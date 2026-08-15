#!/usr/bin/env python3
"""Capture `codex exec --json` without mixing stdout events and stderr."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import Counter
from typing import Any, BinaryIO, Mapping, Sequence

from trace_jsonl import (
    TraceError,
    append_event,
    canonical_json_bytes,
    load_known_secrets,
    redact_value,
    strict_json_loads,
    utc_now,
)


DEFAULT_MAX_LINE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_PROMPT_BYTES = 16 * 1024 * 1024
COLLECTOR_VERSION = "1.0.0"
PRIVATE_KEY_BEGIN_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE
)
PRIVATE_KEY_END_RE = re.compile(
    r"-----END [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE
)


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _new_private_binary(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def _fsync(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".partial")
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    with _new_private_binary(temporary) as handle:
        handle.write(encoded)
        _fsync(handle)
    os.replace(temporary, path)


def _safe_relative(path: Path, cwd: Path) -> str:
    try:
        return os.path.relpath(path.resolve(), cwd.resolve())
    except (OSError, ValueError):
        return path.name


def _read_prompt(args: argparse.Namespace) -> bytes:
    if args.prompt_file is not None:
        try:
            raw = args.prompt_file.read_bytes()
        except OSError as exc:
            raise TraceError(f"cannot read prompt file: {exc}") from exc
    else:
        raw = sys.stdin.buffer.read(args.max_prompt_bytes + 1)
    if len(raw) > args.max_prompt_bytes:
        raise TraceError(f"prompt exceeds {args.max_prompt_bytes} bytes")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise TraceError("prompt must not start with a UTF-8 BOM")
    if b"\x00" in raw:
        raise TraceError("prompt contains a NUL byte")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TraceError(f"prompt is not valid UTF-8 at byte {exc.start}") from exc
    if not raw.strip():
        raise TraceError("prompt is empty")
    return raw


def _resolve_executable(value: str) -> str:
    candidate = Path(value)
    if candidate.parent != Path(".") or candidate.is_absolute():
        if not candidate.exists():
            raise FileNotFoundError(value)
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if resolved is None:
        raise FileNotFoundError(value)
    return resolved


class CaptureState:
    def __init__(self) -> None:
        self.event_counts: Counter[str] = Counter()
        self.item_counts: Counter[str] = Counter()
        self.thread_ids: set[str] = set()
        self.protocol_errors: list[dict[str, Any]] = []
        self.stderr_errors: list[str] = []
        self.redactions = 0
        self.records = 0
        self.terminal_event_seen = False

    def protocol_error(self, line: int, raw: bytes, message: str) -> None:
        self.protocol_errors.append(
            {
                "line": line,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "error": message,
            }
        )


def _read_bounded_line(
    pipe: BinaryIO, max_line_bytes: int
) -> tuple[bytes, bool, bool]:
    """Return bytes, EOF flag, and oversize flag while draining one physical line."""
    first = pipe.readline(max_line_bytes + 2)
    if not first:
        return b"", True, False
    if first.endswith(b"\n"):
        content_size = len(first) - 1
        if first.endswith(b"\r\n"):
            content_size -= 1
        if content_size <= max_line_bytes:
            return first, False, False
    if not first.endswith(b"\n") and len(first) <= max_line_bytes:
        return first, False, False

    digest_source = bytearray(first[: min(len(first), 1024 * 1024)])
    total = len(first)
    current = first
    while current and not current.endswith(b"\n"):
        current = pipe.readline(max_line_bytes + 2)
        if not current:
            break
        total += len(current)
        remaining = 1024 * 1024 - len(digest_source)
        if remaining > 0:
            digest_source.extend(current[:remaining])
    marker = (
        b"[OVERSIZED-LINE bytes="
        + str(total).encode("ascii")
        + b" sample_sha256="
        + hashlib.sha256(bytes(digest_source)).hexdigest().encode("ascii")
        + b"]\n"
    )
    return marker, False, True


def _drain_stdout(
    pipe: BinaryIO,
    output: BinaryIO,
    state: CaptureState,
    secrets: Sequence[str],
    max_line_bytes: int,
) -> None:
    line_number = 0
    try:
        while True:
            raw, eof, oversized = _read_bounded_line(pipe, max_line_bytes)
            if eof:
                break
            line_number += 1
            if oversized:
                state.protocol_error(line_number, raw, "event line exceeds size limit")
                continue
            if not raw.endswith(b"\n"):
                state.protocol_error(line_number, raw, "event line is missing final newline")
                break
            content = raw[:-1]
            if content.endswith(b"\r"):
                content = content[:-1]
            if len(content) > max_line_bytes:
                state.protocol_error(
                    line_number,
                    raw,
                    f"event line exceeds {max_line_bytes} bytes",
                )
                continue
            if not content:
                state.protocol_error(line_number, raw, "blank event line")
                continue
            try:
                event = strict_json_loads(content, label=f"event line {line_number}")
                if not isinstance(event, dict):
                    raise TraceError("top-level value is not an object")
                event_type = event.get("type")
                if not isinstance(event_type, str) or not event_type:
                    raise TraceError("event has no non-empty string type")
                cleaned, redactions = redact_value(event, secrets)
                encoded = canonical_json_bytes(cleaned)
                if len(encoded) > max_line_bytes:
                    raise TraceError("redacted event exceeds size limit")
            except TraceError as exc:
                state.protocol_error(line_number, raw, str(exc))
                continue

            output.write(encoded)
            output.flush()
            state.redactions += redactions
            state.records += 1
            state.event_counts[event_type] += 1
            thread_id = cleaned.get("thread_id")
            if isinstance(thread_id, str):
                state.thread_ids.add(thread_id)
            item = cleaned.get("item")
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                state.item_counts[item["type"]] += 1
            if event_type in {"turn.completed", "turn.failed", "error"}:
                state.terminal_event_seen = True
    except Exception as exc:  # thread boundary: preserve the failure in the manifest
        state.protocol_errors.append(
            {"line": line_number, "bytes": 0, "sha256": None, "error": repr(exc)}
        )


def _redact_text(value: str, secrets: Sequence[str]) -> tuple[str, int]:
    cleaned, count = redact_value(value, secrets)
    assert isinstance(cleaned, str)
    return cleaned, count


def _drain_stderr(
    pipe: BinaryIO,
    output: BinaryIO,
    state: CaptureState,
    secrets: Sequence[str],
    max_line_bytes: int,
    tee: bool,
) -> None:
    inside_private_key = False
    try:
        while True:
            raw, eof, oversized = _read_bounded_line(pipe, max_line_bytes)
            if eof:
                break
            if oversized:
                text = raw.decode("ascii", errors="replace")
                state.stderr_errors.append("oversized stderr line was replaced")
                redactions = 0
            else:
                text = raw.decode("utf-8", errors="replace")
                if inside_private_key:
                    if PRIVATE_KEY_END_RE.search(text):
                        inside_private_key = False
                    continue
                if PRIVATE_KEY_BEGIN_RE.search(text):
                    inside_private_key = not bool(PRIVATE_KEY_END_RE.search(text))
                    text = "[REDACTED:private-key]\n"
                    redactions = 1
                else:
                    text, redactions = _redact_text(text, secrets)
            encoded = text.encode("utf-8")
            output.write(encoded)
            output.flush()
            state.redactions += redactions
            if tee:
                sys.stderr.write(text)
                sys.stderr.flush()
        if inside_private_key:
            state.stderr_errors.append("unterminated private key block was redacted")
    except Exception as exc:  # thread boundary
        state.stderr_errors.append(repr(exc))


def _write_prompt(pipe: BinaryIO, prompt: bytes, errors: list[str]) -> None:
    try:
        pipe.write(prompt)
        if not prompt.endswith(b"\n"):
            pipe.write(b"\n")
        pipe.flush()
    except BrokenPipeError:
        errors.append("Codex closed stdin before the prompt was fully written")
    except Exception as exc:  # thread boundary
        errors.append(repr(exc))
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def verify_events(path: Path, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    event_counts: Counter[str] = Counter()
    item_counts: Counter[str] = Counter()
    thread_ids: set[str] = set()
    usage = Counter()
    terminal = False
    records = 0
    try:
        handle = open(path, "rb")
    except OSError as exc:
        return {"valid": False, "errors": [f"cannot open events file: {exc}"]}
    with handle:
        line_number = 0
        while True:
            raw = handle.readline(max_line_bytes + 2)
            if not raw:
                break
            line_number += 1
            if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
                errors.append(f"line {line_number}: exceeds {max_line_bytes} bytes")
                break
            if not raw.endswith(b"\n"):
                errors.append(f"line {line_number}: missing final newline")
                break
            content = raw[:-1]
            if content.endswith(b"\r"):
                content = content[:-1]
            if len(content) > max_line_bytes:
                errors.append(f"line {line_number}: exceeds {max_line_bytes} bytes")
                continue
            if not content:
                errors.append(f"line {line_number}: blank line")
                continue
            try:
                event = strict_json_loads(content, label=f"line {line_number}")
            except TraceError as exc:
                errors.append(str(exc))
                continue
            if not isinstance(event, dict):
                errors.append(f"line {line_number}: top-level value is not an object")
                continue
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                errors.append(f"line {line_number}: missing string type")
                continue
            records += 1
            event_counts[event_type] += 1
            item = event.get("item")
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                item_counts[item["type"]] += 1
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                thread_ids.add(thread_id)
            if event_type in {"turn.completed", "turn.failed", "error"}:
                terminal = True
            if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
                for key, value in event["usage"].items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage[key] += value
    if records == 0:
        errors.append("events file contains no valid records")
    if not terminal:
        warnings.append("no terminal turn.completed, turn.failed, or error event")
    return {
        "valid": not errors,
        "records": records,
        "errors": errors,
        "warnings": warnings,
        "event_counts": dict(sorted(event_counts.items())),
        "item_counts": dict(sorted(item_counts.items())),
        "thread_ids": sorted(thread_ids),
        "terminal_event_seen": terminal,
        "usage": dict(sorted(usage.items())),
    }


def _child_exit_code(return_code: int) -> int:
    if return_code < 0:
        return min(255, 128 + abs(return_code))
    return return_code


def command_capture(args: argparse.Namespace) -> int:
    prompt = _read_prompt(args)
    cwd = args.cwd.resolve()
    if not cwd.is_dir():
        raise TraceError(f"cwd is not a directory: {args.cwd}")
    extra_args = list(args.codex_args)
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    if "--json" in extra_args:
        raise TraceError("do not pass --json; the collector adds it")
    executable = _resolve_executable(args.codex)
    command = [executable, "exec", "--json", *extra_args, "-"]
    sanitized_command, command_redactions = redact_value(
        [Path(executable).name, "exec", "--json", *extra_args, "-"],
        load_known_secrets(),
    )

    output_dir = args.output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.mkdir()
    except FileExistsError as exc:
        raise TraceError("output directory already exists; refusing to overwrite") from exc

    capture_id = f"capture-{uuid.uuid4()}"
    started_at = utc_now()
    prompt_sha256 = hashlib.sha256(prompt).hexdigest()

    events_partial = output_dir / "events.jsonl.partial"
    stderr_partial = output_dir / "stderr.log.partial"
    events_final = output_dir / "events.jsonl"
    stderr_final = output_dir / "stderr.log"
    manifest_path = output_dir / "manifest.json"
    state = CaptureState()
    state.redactions += command_redactions
    stdin_errors: list[str] = []
    semantic_errors: list[str] = []
    interrupted = False
    return_code: int | None = None
    spawn_error: str | None = None

    semantic_context = {
        "capture_id": capture_id,
        "capture_dir": _safe_relative(output_dir, cwd),
        "prompt_sha256": prompt_sha256,
        "prompt_bytes": len(prompt),
        "argv": sanitized_command,
    }
    if args.trace is not None:
        try:
            append_event(
                args.trace,
                "codex.capture.started",
                semantic_context,
                source="codex.capture",
                experiment_id=args.experiment_id,
                iteration=args.iteration,
                attempt=args.attempt,
            )
        except (TraceError, OSError) as exc:
            raise TraceError(f"cannot append codex.capture.started: {exc}") from exc

    with _new_private_binary(events_partial) as events_handle, _new_private_binary(
        stderr_partial
    ) as stderr_handle:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (OSError, ValueError) as exc:
            spawn_error = str(exc)
            process = None

        if process is not None:
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            secrets = load_known_secrets()
            stdout_thread = threading.Thread(
                target=_drain_stdout,
                args=(
                    process.stdout,
                    events_handle,
                    state,
                    secrets,
                    args.max_line_bytes,
                ),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(
                    process.stderr,
                    stderr_handle,
                    state,
                    secrets,
                    args.max_line_bytes,
                    args.tee_stderr,
                ),
                daemon=True,
            )
            stdin_thread = threading.Thread(
                target=_write_prompt,
                args=(process.stdin, prompt, stdin_errors),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            stdin_thread.start()
            try:
                return_code = process.wait()
            except KeyboardInterrupt:
                interrupted = True
                process.terminate()
                try:
                    return_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    return_code = process.wait()
            stdin_thread.join(timeout=5)
            stdout_thread.join()
            stderr_thread.join()
        _fsync(events_handle)
        _fsync(stderr_handle)

    os.replace(events_partial, events_final)
    os.replace(stderr_partial, stderr_final)
    events_sha, events_bytes = _sha256_file(events_final)
    stderr_sha, stderr_bytes = _sha256_file(stderr_final)
    verification = verify_events(events_final, args.max_line_bytes)

    if spawn_error:
        collector_status = "spawn_failed"
    elif state.protocol_errors or not verification.get("valid", False):
        collector_status = "malformed_stream"
    elif state.stderr_errors or stdin_errors:
        collector_status = "io_warning"
    elif interrupted:
        collector_status = "interrupted"
    elif return_code:
        collector_status = "codex_failed"
    else:
        collector_status = "completed"

    completed_at = utc_now()
    completion_data = {
        **semantic_context,
        "collector_status": collector_status,
        "codex_exit_code": return_code,
        "records": state.records,
        "events_sha256": events_sha,
        "events_bytes": events_bytes,
        "terminal_event_seen": state.terminal_event_seen,
    }
    if args.trace is not None:
        try:
            append_event(
                args.trace,
                "codex.capture.completed",
                completion_data,
                source="codex.capture",
                experiment_id=args.experiment_id,
                iteration=args.iteration,
                attempt=args.attempt,
            )
        except (TraceError, OSError) as exc:
            semantic_errors.append(str(exc))
            collector_status = "semantic_trace_failed"

    manifest = {
        "schema": "codex.capture/v1",
        "collector_version": COLLECTOR_VERSION,
        "capture_id": capture_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "cwd": ".",
        "prompt": {"sha256": prompt_sha256, "bytes": len(prompt)},
        "argv": sanitized_command,
        "collector_status": collector_status,
        "codex_exit_code": return_code,
        "interrupted": interrupted,
        "spawn_error": spawn_error,
        "records": state.records,
        "event_counts": dict(sorted(state.event_counts.items())),
        "item_counts": dict(sorted(state.item_counts.items())),
        "thread_ids": sorted(state.thread_ids),
        "terminal_event_seen": state.terminal_event_seen,
        "redactions": state.redactions,
        "protocol_errors": state.protocol_errors,
        "stderr_warnings": state.stderr_errors,
        "stdin_warnings": stdin_errors,
        "semantic_trace_errors": semantic_errors,
        "verification": verification,
        "files": {
            "events.jsonl": {"sha256": events_sha, "bytes": events_bytes},
            "stderr.log": {"sha256": stderr_sha, "bytes": stderr_bytes},
        },
    }
    _write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "capture_id": capture_id,
                "collector_status": collector_status,
                "codex_exit_code": return_code,
                "records": state.records,
            },
            ensure_ascii=False,
        )
    )

    if semantic_errors or state.stderr_errors or stdin_errors:
        return 74
    if spawn_error:
        return 69
    if state.protocol_errors or not verification.get("valid", False):
        return 65
    if interrupted:
        return 130
    return _child_exit_code(return_code or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture the native JSONL stream from codex exec --json."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="launch and capture codex exec")
    capture.add_argument("--output-dir", type=Path, required=True)
    prompt_group = capture.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt-file", type=Path)
    prompt_group.add_argument("--stdin", action="store_true")
    capture.add_argument("--cwd", type=Path, default=Path("."))
    capture.add_argument("--codex", default="codex")
    capture.add_argument("--trace", type=Path)
    capture.add_argument("--experiment-id")
    capture.add_argument("--iteration", type=int)
    capture.add_argument("--attempt", type=int)
    capture.add_argument("--tee-stderr", action="store_true")
    capture.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)
    capture.add_argument("--max-prompt-bytes", type=int, default=DEFAULT_MAX_PROMPT_BYTES)
    capture.add_argument("codex_args", nargs=argparse.REMAINDER)

    verify = subparsers.add_parser("verify", help="verify a native Codex events file")
    verify.add_argument("events", type=Path)
    verify.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)

    stats = subparsers.add_parser("stats", help="summarize a native Codex events file")
    stats.add_argument("events", type=Path)
    stats.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            if args.max_line_bytes < 1024:
                raise TraceError("--max-line-bytes must be at least 1024")
            if args.max_prompt_bytes < 1:
                raise TraceError("--max-prompt-bytes must be positive")
            return command_capture(args)
        result = verify_events(args.events, args.max_line_bytes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("valid") else 1
    except FileNotFoundError as exc:
        print(f"ERROR: Codex executable not found: {exc}", file=sys.stderr)
        return 69
    except (TraceError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
