#!/usr/bin/env python3
"""Append, validate, and summarize semantic autoresearch traces."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid
from collections import Counter
from typing import Any, Iterator, Mapping, Sequence


SCHEMA = "autoresearch.trace/v1"
COLLECTOR_VERSION = "1.0.0"
DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
EVENT_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,255}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

SENSITIVE_KEYS = {
    "access_key",
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "auth",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "session_cookie",
    "token",
}
SECRET_ENV_NAMES = (
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY",
)
SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-access-key]"),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(
            r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "[REDACTED:private-key]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|password|passwd|secret)"
            r"\s*([:=])\s*([^\s,;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
)
QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>[\"'](?:api[_-]?key|apikey|access[_-]?token|accesstoken|"
    r"refresh[_-]?token|refreshtoken|client[_-]?secret|clientsecret|"
    r"aws[_-]?secret[_-]?access[_-]?key|authorization|password|passwd|"
    r"private[_-]?key|token|secret|cookie)[\"']\s*:\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)


class TraceError(Exception):
    """A stable, user-facing trace protocol error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes, *, label: str = "JSON") -> Any:
    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raise TraceError(f"{label}: UTF-8 BOM is not allowed")
        if b"\x00" in raw:
            raise TraceError(f"{label}: NUL byte is not allowed")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TraceError(f"{label}: invalid UTF-8 at byte {exc.start}") from exc
    else:
        text = raw
        if text.startswith("\ufeff"):
            raise TraceError(f"{label}: UTF-8 BOM is not allowed")
        if "\x00" in text:
            raise TraceError(f"{label}: NUL character is not allowed")
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise TraceError(f"{label}: invalid JSON: {exc}") from exc


def load_known_secrets() -> tuple[str, ...]:
    values = []
    for name in SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            values.append(value)
    return tuple(sorted(set(values), key=len, reverse=True))


def _redact_string(value: str, secrets: Sequence[str]) -> tuple[str, int]:
    count = 0
    redacted = value
    for secret in secrets:
        occurrences = redacted.count(secret)
        if occurrences:
            redacted = redacted.replace(secret, "[REDACTED:known-secret]")
            count += occurrences
    for pattern, replacement in SECRET_PATTERNS:
        redacted, matches = pattern.subn(replacement, redacted)
        count += matches
    redacted, matches = QUOTED_SECRET_ASSIGNMENT_RE.subn(
        lambda match: (
            match.group("prefix")
            + match.group("quote")
            + "[REDACTED]"
            + match.group("quote")
        ),
        redacted,
    )
    count += matches
    return redacted, count


def redact_value(value: Any, secrets: Sequence[str] | None = None) -> tuple[Any, int]:
    """Recursively redact common secret keys and string patterns."""
    known = load_known_secrets() if secrets is None else tuple(secrets)

    def normalize_key(key: str) -> str:
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
        return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")

    def walk(item: Any, key_hint: str | None = None) -> tuple[Any, int]:
        normalized = normalize_key(key_hint or "")
        if normalized in SENSITIVE_KEYS or any(
            normalized.endswith("_" + sensitive) for sensitive in SENSITIVE_KEYS
        ):
            return "[REDACTED:sensitive-field]", 1
        if isinstance(item, str):
            cleaned_string, string_count = _redact_string(item, known)
            stripped = cleaned_string.strip()
            if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
                try:
                    embedded = strict_json_loads(stripped, label="embedded JSON")
                except TraceError:
                    return cleaned_string, string_count
                if isinstance(embedded, (dict, list)):
                    cleaned_embedded, embedded_count = walk(embedded)
                    if embedded_count:
                        return (
                            json.dumps(
                                cleaned_embedded,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ),
                            string_count + embedded_count,
                        )
            return cleaned_string, string_count
        if isinstance(item, list):
            output = []
            total = 0
            for child in item:
                cleaned, count = walk(child)
                output.append(cleaned)
                total += count
            return output, total
        if isinstance(item, dict):
            output_dict: dict[str, Any] = {}
            total = 0
            for key, child in item.items():
                cleaned, count = walk(child, str(key))
                output_dict[str(key)] = cleaned
                total += count
            return output_dict, total
        return item, 0

    return walk(value)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TraceError(f"event is not JSON serializable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


@contextlib.contextmanager
def trace_lock(trace_path: Path) -> Iterator[None]:
    """Cooperative one-byte lock for local filesystems."""
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = trace_path.with_name(f".{trace_path.name}.lock")
    with open(lock_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _last_record(trace_path: Path, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> dict[str, Any] | None:
    if not trace_path.exists() or trace_path.stat().st_size == 0:
        return None
    with open(trace_path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        handle.seek(end - 1)
        if handle.read(1) != b"\n":
            raise TraceError("trace has an incomplete final line; refusing to append")

        cursor = end - 1
        start = 0
        while cursor > 0:
            block_size = min(8192, cursor)
            cursor -= block_size
            handle.seek(cursor)
            block = handle.read(block_size)
            newline = block.rfind(b"\n")
            if newline >= 0:
                start = cursor + newline + 1
                break
            if end - 1 - cursor > max_line_bytes:
                raise TraceError("final trace line exceeds the configured size limit")

        line_size = end - 1 - start
        if line_size <= 0:
            raise TraceError("trace ends with a blank line; refusing to append")
        if line_size > max_line_bytes:
            raise TraceError("final trace line exceeds the configured size limit")
        handle.seek(start)
        raw = handle.read(line_size)
    value = strict_json_loads(raw, label="final trace line")
    if not isinstance(value, dict):
        raise TraceError("final trace line is not a JSON object")
    return value


def append_event(
    trace_path: Path,
    event: str,
    data: Mapping[str, Any],
    *,
    run_id: str | None = None,
    source: str = "semantic",
    experiment_id: str | None = None,
    iteration: int | None = None,
    attempt: int | None = None,
    worker_id: str | None = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> dict[str, Any]:
    if not EVENT_RE.fullmatch(event):
        raise TraceError("event must be a lowercase dotted name")
    _validate_id(source, "source")
    for value, label in (
        (run_id, "run_id"),
        (experiment_id, "experiment_id"),
        (worker_id, "worker_id"),
    ):
        if value is not None:
            _validate_id(value, label)
    if iteration is not None and iteration < 0:
        raise TraceError("iteration must be non-negative")
    if attempt is not None and attempt < 1:
        raise TraceError("attempt must be at least 1")

    cleaned_data, redaction_count = redact_value(dict(data))
    with trace_lock(trace_path):
        previous = _last_record(trace_path, max_line_bytes)
        expected_metric: Mapping[str, Any] | None = None
        if previous is None:
            if event != "run.started":
                raise TraceError("the first event must be run.started; run init first")
            resolved_run_id = run_id or new_run_id()
            sequence = 1
            if isinstance(cleaned_data.get("metric"), dict):
                expected_metric = cleaned_data["metric"]
        else:
            validation = validate_trace(trace_path, max_line_bytes)
            if not validation["valid"]:
                first_error = validation["errors"][0]
                raise TraceError(
                    f"existing trace is invalid; refusing to append: {first_error}"
                )
            if event == "run.started":
                raise TraceError("run.started already exists")
            if previous.get("schema") != SCHEMA:
                raise TraceError("final record uses an unsupported schema")
            previous_run_id = previous.get("run_id")
            if not isinstance(previous_run_id, str):
                raise TraceError("final record has no valid run_id")
            if run_id is not None and run_id != previous_run_id:
                raise TraceError("run_id does not match the existing trace")
            resolved_run_id = previous_run_id
            previous_seq = previous.get("seq")
            if not isinstance(previous_seq, int) or isinstance(previous_seq, bool):
                raise TraceError("final record has no valid seq")
            sequence = previous_seq + 1
            if previous.get("event") == "run.finished":
                raise TraceError("run.finished is terminal; refusing to append")
            first_record = next(iter_records(trace_path, max_line_bytes))[1]
            first_data = first_record.get("data")
            if isinstance(first_data, dict) and isinstance(first_data.get("metric"), dict):
                expected_metric = first_data["metric"]
            if event == "experiment.completed" and experiment_id:
                for _, existing in iter_records(trace_path, max_line_bytes):
                    if (
                        existing.get("event") == "experiment.completed"
                        and existing.get("experiment_id") == experiment_id
                    ):
                        raise TraceError(
                            f"experiment {experiment_id} already has a completed event"
                        )
            if event == "hypothesis.proposed" and experiment_id:
                for _, existing in iter_records(trace_path, max_line_bytes):
                    if (
                        existing.get("event") == "experiment.completed"
                        and existing.get("experiment_id") == experiment_id
                    ):
                        raise TraceError(
                            f"experiment {experiment_id} is already completed; "
                            "refusing a late hypothesis"
                        )

        candidate_errors: list[str] = []
        _validate_candidate_event(
            event,
            cleaned_data,
            experiment_id=experiment_id,
            iteration=iteration,
            expected_metric=expected_metric,
            errors=candidate_errors,
        )
        if candidate_errors:
            raise TraceError("invalid new event: " + "; ".join(candidate_errors))

        record: dict[str, Any] = {
            "schema": SCHEMA,
            "event_id": str(uuid.uuid4()),
            "event": event,
            "ts": utc_now(),
            "run_id": resolved_run_id,
            "seq": sequence,
            "source": source,
            "data": cleaned_data,
        }
        if experiment_id is not None:
            record["experiment_id"] = experiment_id
        if iteration is not None:
            record["iteration"] = iteration
        if attempt is not None:
            record["attempt"] = attempt
        if worker_id is not None:
            record["worker_id"] = worker_id
        if redaction_count:
            record["redactions"] = redaction_count

        encoded = canonical_json_bytes(record)
        if len(encoded) > max_line_bytes:
            raise TraceError(
                f"event is {len(encoded)} bytes; maximum is {max_line_bytes} bytes"
            )
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(trace_path, flags, 0o600)
        original_size = os.lseek(descriptor, 0, os.SEEK_END)
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("short write while appending the event")
                offset += written
            os.fsync(descriptor)
        except Exception as exc:
            try:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
            except OSError as rollback_error:
                raise TraceError(
                    "append failed and rollback also failed; preserve and validate the trace"
                ) from rollback_error
            raise TraceError(f"append failed and was rolled back: {exc}") from exc
        finally:
            os.close(descriptor)
    return record


def _validate_id(value: str, label: str) -> None:
    if not ID_RE.fullmatch(value):
        raise TraceError(
            f"{label} must be 1-256 characters using letters, digits, and ._:@/+~-"
        )


def new_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ar-{stamp}-{uuid.uuid4().hex[:8]}"


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _load_object_argument(raw: str | None, file_path: str | None, label: str) -> dict[str, Any]:
    if raw is None and file_path is None:
        return {}
    if raw is not None and file_path is not None:
        raise TraceError(f"use only one of --{label}-json and --{label}-file")
    if file_path is not None:
        try:
            payload = Path(file_path).read_bytes()
        except OSError as exc:
            raise TraceError(f"cannot read {label} file: {exc}") from exc
    else:
        payload = raw or "{}"
    value = strict_json_loads(payload, label=label)
    if not isinstance(value, dict):
        raise TraceError(f"{label} must be a JSON object")
    return value


def iter_records(
    trace_path: Path, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        handle = open(trace_path, "rb")
    except OSError as exc:
        raise TraceError(f"cannot open trace: {exc}") from exc
    with handle:
        first = True
        while True:
            raw = handle.readline(max_line_bytes + 2)
            if not raw:
                break
            line_number = 1 if first else line_number + 1
            first = False
            if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
                raise TraceError(f"line {line_number}: exceeds {max_line_bytes} bytes")
            if not raw.endswith(b"\n"):
                raise TraceError(f"line {line_number}: missing final newline")
            content = raw[:-1]
            if content.endswith(b"\r"):
                content = content[:-1]
            if len(content) > max_line_bytes:
                raise TraceError(f"line {line_number}: exceeds {max_line_bytes} bytes")
            if not content:
                raise TraceError(f"line {line_number}: blank lines are not allowed")
            value = strict_json_loads(content, label=f"line {line_number}")
            if not isinstance(value, dict):
                raise TraceError(f"line {line_number}: top-level value must be an object")
            yield line_number, value
        if first:
            raise TraceError("trace is empty")


def validate_trace(
    trace_path: Path, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    try:
        records = [record for _, record in iter_records(trace_path, max_line_bytes)]
    except TraceError as exc:
        return {"valid": False, "errors": [str(exc)], "warnings": [], "records": 0}

    run_id: str | None = None
    expected_metric: Mapping[str, Any] | None = None
    first_data = records[0].get("data") if records else None
    if isinstance(first_data, dict) and isinstance(first_data.get("metric"), dict):
        expected_metric = first_data["metric"]
    event_ids: set[str] = set()
    completed_experiments: set[str] = set()
    started_experiments: set[str] = set()
    hypothesis_experiments: set[str] = set()
    first_hypothesis_seq: dict[str, int] = {}
    completion_seq: dict[str, int] = {}
    finish_count = 0

    for index, record in enumerate(records, 1):
        prefix = f"line {index}"
        if record.get("schema") != SCHEMA:
            errors.append(f"{prefix}: schema must be {SCHEMA}")
        event_id = record.get("event_id")
        if not isinstance(event_id, str):
            errors.append(f"{prefix}: event_id must be a string")
        elif event_id in event_ids:
            errors.append(f"{prefix}: duplicate event_id {event_id}")
        else:
            event_ids.add(event_id)
            try:
                uuid.UUID(event_id)
            except ValueError:
                errors.append(f"{prefix}: event_id must be a UUID")

        event = record.get("event")
        if not isinstance(event, str) or not EVENT_RE.fullmatch(event):
            errors.append(f"{prefix}: event must be a lowercase dotted name")
            event = ""
        timestamp = record.get("ts")
        if not isinstance(timestamp, str) or not _valid_timestamp(timestamp):
            errors.append(f"{prefix}: ts must be an RFC3339 UTC timestamp")
        current_run_id = record.get("run_id")
        if not isinstance(current_run_id, str):
            errors.append(f"{prefix}: run_id must be a string")
        elif run_id is None:
            run_id = current_run_id
        elif current_run_id != run_id:
            errors.append(f"{prefix}: run_id differs from the first record")
        seq = record.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != index:
            errors.append(f"{prefix}: seq must equal {index}")
        source = record.get("source")
        if not isinstance(source, str) or not source:
            errors.append(f"{prefix}: source must be a non-empty string")
        data = record.get("data")
        if not isinstance(data, dict):
            errors.append(f"{prefix}: data must be an object")
            data = {}
        if "redactions" in record and (
            not isinstance(record["redactions"], int)
            or isinstance(record["redactions"], bool)
            or record["redactions"] < 1
        ):
            errors.append(f"{prefix}: redactions must be a positive integer")

        experiment_id = record.get("experiment_id")
        if experiment_id is not None and not isinstance(experiment_id, str):
            errors.append(f"{prefix}: experiment_id must be a string")
            experiment_id = None
        elif isinstance(experiment_id, str) and not ID_RE.fullmatch(experiment_id):
            errors.append(f"{prefix}: experiment_id has invalid characters")
        iteration = record.get("iteration")
        if iteration is not None and (
            not isinstance(iteration, int)
            or isinstance(iteration, bool)
            or iteration < 0
        ):
            errors.append(f"{prefix}: iteration must be a non-negative integer")
        attempt = record.get("attempt")
        if attempt is not None and (
            not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
        ):
            errors.append(f"{prefix}: attempt must be a positive integer")
        worker_id = record.get("worker_id")
        if worker_id is not None and (
            not isinstance(worker_id, str) or not ID_RE.fullmatch(worker_id)
        ):
            errors.append(f"{prefix}: worker_id is invalid")

        if event == "run.started":
            if index != 1:
                errors.append(f"{prefix}: run.started must be the first record")
            _validate_run_started(data, prefix, errors)
        elif index == 1:
            errors.append(f"{prefix}: first event must be run.started")

        if event == "run.finished":
            finish_count += 1
            if index != len(records):
                errors.append(f"{prefix}: run.finished must be the final record")
            if data.get("status") not in {"completed", "stopped", "failed"}:
                errors.append(f"{prefix}: invalid run finish status")

        if event in {
            "hypothesis.proposed",
            "experiment.started",
            "experiment.prepared",
            "experiment.completed",
            "evaluation.started",
            "evaluation.completed",
            "decision.selected",
            "decision.applied",
        } and not experiment_id:
            errors.append(f"{prefix}: {event} requires experiment_id")

        if event == "experiment.started" and experiment_id:
            started_experiments.add(experiment_id)
        if event == "hypothesis.proposed" and experiment_id:
            hypothesis_experiments.add(experiment_id)
            first_hypothesis_seq.setdefault(experiment_id, index)
            message = data.get("message")
            if message is not None and not isinstance(message, str):
                errors.append(f"{prefix}: hypothesis message must be a string or null")
        if event == "experiment.completed" and experiment_id:
            if experiment_id in completed_experiments:
                errors.append(f"{prefix}: duplicate completed experiment {experiment_id}")
            completed_experiments.add(experiment_id)
            completion_seq[experiment_id] = index
            if iteration is None:
                errors.append(f"{prefix}: experiment.completed requires iteration")
            _validate_experiment(data, prefix, errors, expected_metric)

    if finish_count > 1:
        errors.append("run.finished appears more than once")
    incomplete = sorted((started_experiments | hypothesis_experiments) - completed_experiments)
    if incomplete:
        warnings.append("incomplete experiments: " + ", ".join(incomplete))
    if finish_count == 0:
        warnings.append("run has no run.finished event")
    missing_hypotheses = sorted(completed_experiments - hypothesis_experiments)
    if missing_hypotheses:
        warnings.append(
            "completed experiments without a recorded prior hypothesis: "
            + ", ".join(missing_hypotheses)
        )
    late_hypotheses = sorted(
        experiment_id
        for experiment_id in completed_experiments & hypothesis_experiments
        if first_hypothesis_seq[experiment_id] > completion_seq[experiment_id]
    )
    if late_hypotheses:
        errors.append(
            "hypothesis was recorded after completion for: " + ", ".join(late_hypotheses)
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "records": len(records),
        "run_id": run_id,
        "incomplete_experiments": incomplete,
    }


def _valid_timestamp(value: str) -> bool:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value
    ):
        return False
    try:
        dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _validate_run_started(data: Mapping[str, Any], prefix: str, errors: list[str]) -> None:
    if not isinstance(data.get("collector_version"), str) or not data.get(
        "collector_version"
    ):
        errors.append(f"{prefix}: collector_version must be non-empty")
    if not isinstance(data.get("goal"), str) or not data.get("goal"):
        errors.append(f"{prefix}: run.started requires a non-empty goal")
    metric = data.get("metric")
    if not isinstance(metric, dict):
        errors.append(f"{prefix}: run.started requires metric object")
        return
    if not isinstance(metric.get("name"), str) or not metric.get("name"):
        errors.append(f"{prefix}: metric name must be non-empty")
    if metric.get("direction") not in {"minimize", "maximize"}:
        errors.append(f"{prefix}: metric direction must be minimize or maximize")
    if not isinstance(data.get("metadata"), dict):
        errors.append(f"{prefix}: metadata must be an object")
    if not isinstance(data.get("project_root"), str) or not data.get("project_root"):
        errors.append(f"{prefix}: project_root must be a non-empty string")


def _validate_experiment(
    data: Mapping[str, Any],
    prefix: str,
    errors: list[str],
    expected_metric: Mapping[str, Any] | None = None,
) -> None:
    status = data.get("status")
    if status not in {"keep", "discard", "crash"}:
        errors.append(f"{prefix}: experiment status must be keep, discard, or crash")
    if not isinstance(data.get("description"), str) or not data.get("description"):
        errors.append(f"{prefix}: experiment description must be non-empty")
    if "commit" in data and (
        not isinstance(data["commit"], str) or not data["commit"]
    ):
        errors.append(f"{prefix}: commit must be a non-empty string")
    metric = data.get("primary_metric")
    if status in {"keep", "discard"} and not isinstance(metric, dict):
        errors.append(f"{prefix}: successful experiment requires primary_metric")
    if isinstance(metric, dict):
        if not isinstance(metric.get("name"), str) or not metric.get("name"):
            errors.append(f"{prefix}: primary metric name must be non-empty")
        value = metric.get("value")
        if not _finite_number(value):
            errors.append(f"{prefix}: primary metric value must be finite")
        if metric.get("direction") not in {"minimize", "maximize"}:
            errors.append(f"{prefix}: primary metric direction is invalid")
        if metric.get("trust") not in {"reported", "verified"}:
            errors.append(f"{prefix}: primary metric trust must be reported or verified")
        if not isinstance(metric.get("source"), str) or not metric.get("source"):
            errors.append(f"{prefix}: primary metric source must be non-empty")
        if expected_metric is not None:
            for key in ("name", "direction", "unit"):
                if metric.get(key) != expected_metric.get(key):
                    errors.append(
                        f"{prefix}: primary metric {key} does not match run.started"
                    )
    if "metrics" in data and not isinstance(data["metrics"], dict):
        errors.append(f"{prefix}: metrics must be an object")
    if "artifacts" in data and not isinstance(data["artifacts"], dict):
        errors.append(f"{prefix}: artifacts must be an object")
    for key in ("memory_gb", "duration_seconds"):
        if key in data and not _finite_number(data[key], nonnegative=True):
            errors.append(f"{prefix}: {key} must be a finite non-negative number")


def _finite_number(value: Any, *, nonnegative: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    if not math.isfinite(numeric):
        return False
    return not nonnegative or numeric >= 0


def _validate_candidate_event(
    event: str,
    data: Mapping[str, Any],
    *,
    experiment_id: str | None,
    iteration: int | None,
    expected_metric: Mapping[str, Any] | None,
    errors: list[str],
) -> None:
    if event == "run.started":
        _validate_run_started(data, "run.started", errors)
    if event == "run.finished" and data.get("status") not in {
        "completed",
        "stopped",
        "failed",
    }:
        errors.append("run.finished status is invalid")
    experiment_events = {
        "hypothesis.proposed",
        "experiment.started",
        "experiment.prepared",
        "experiment.completed",
        "evaluation.started",
        "evaluation.completed",
        "decision.selected",
        "decision.applied",
    }
    if event in experiment_events and not experiment_id:
        errors.append(f"{event} requires experiment_id")
    if event == "hypothesis.proposed":
        message = data.get("message")
        if message is not None and not isinstance(message, str):
            errors.append("hypothesis message must be a string or null")
    if event == "experiment.completed":
        if iteration is None:
            errors.append("experiment.completed requires iteration")
        _validate_experiment(data, "experiment.completed", errors, expected_metric)


def summarize_trace(trace_path: Path) -> dict[str, Any]:
    validation = validate_trace(trace_path)
    if not validation["valid"]:
        return {"valid": False, "validation": validation}
    records = [record for _, record in iter_records(trace_path)]
    counts = Counter(record["event"] for record in records)
    statuses = Counter()
    experiments = []
    started = records[0]["data"]
    run_metric = started.get("metric", {})
    target_name = run_metric.get("name")
    direction = run_metric.get("direction")
    best: dict[str, Any] | None = None
    for record in records:
        if record["event"] != "experiment.completed":
            continue
        data = record["data"]
        statuses[data.get("status", "unknown")] += 1
        metric = data.get("primary_metric")
        summary = {
            "experiment_id": record.get("experiment_id"),
            "iteration": record.get("iteration"),
            "status": data.get("status"),
            "description": data.get("description"),
            "commit": data.get("commit"),
            "primary_metric": metric,
        }
        experiments.append(summary)
        if (
            data.get("status") == "keep"
            and isinstance(metric, dict)
            and metric.get("name") == target_name
            and _finite_number(metric.get("value"))
        ):
            if best is None:
                best = summary
            else:
                candidate = float(metric["value"])
                incumbent = float(best["primary_metric"]["value"])
                if (direction == "minimize" and candidate < incumbent) or (
                    direction == "maximize" and candidate > incumbent
                ):
                    best = summary
    return {
        "valid": True,
        "run_id": records[0]["run_id"],
        "closed": records[-1]["event"] == "run.finished",
        "records": len(records),
        "event_counts": dict(sorted(counts.items())),
        "experiment_status_counts": dict(sorted(statuses.items())),
        "best": best,
        "incomplete_experiments": validation["incomplete_experiments"],
        "warnings": validation["warnings"],
    }


def _common_event_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("trace", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--source", default="semantic")
    parser.add_argument("--experiment-id")
    parser.add_argument("--iteration", type=int)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--worker-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect append-only JSONL events for an autoresearch run."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a new semantic trace")
    init.add_argument("trace", type=Path)
    init.add_argument("--goal", required=True)
    init.add_argument("--metric-name", required=True)
    init.add_argument(
        "--metric-direction", choices=("minimize", "maximize"), required=True
    )
    init.add_argument("--metric-unit")
    init.add_argument("--project-root", default=".")
    init.add_argument("--run-id")
    init.add_argument("--metadata-json")
    init.add_argument("--metadata-file")

    event = subparsers.add_parser("event", help="append a semantic event")
    _common_event_args(event)
    event.add_argument("event")
    event.add_argument("--message")
    event.add_argument("--data-json")
    event.add_argument("--data-file")

    experiment = subparsers.add_parser(
        "experiment", help="append one self-contained experiment result"
    )
    _common_event_args(experiment)
    experiment.add_argument(
        "--status", choices=("keep", "discard", "crash"), required=True
    )
    experiment.add_argument("--description", required=True)
    experiment.add_argument("--commit")
    experiment.add_argument("--metric-name")
    experiment.add_argument("--metric-value", type=float)
    experiment.add_argument(
        "--metric-direction", choices=("minimize", "maximize")
    )
    experiment.add_argument("--metric-unit")
    experiment.add_argument("--metric-source", default="evaluation_output")
    experiment.add_argument(
        "--metric-trust", choices=("reported", "verified"), default="reported"
    )
    experiment.add_argument("--memory-gb", type=float)
    experiment.add_argument("--duration-seconds", type=float)
    experiment.add_argument("--metrics-json")
    experiment.add_argument("--metrics-file")
    experiment.add_argument("--artifacts-json")
    experiment.add_argument("--artifacts-file")
    experiment.add_argument("--error-kind")
    experiment.add_argument("--error-message")

    finish = subparsers.add_parser("finish", help="append the terminal run event")
    finish.add_argument("trace", type=Path)
    finish.add_argument("--run-id")
    finish.add_argument(
        "--status", choices=("completed", "stopped", "failed"), required=True
    )
    finish.add_argument("--summary")
    finish.add_argument("--data-json")
    finish.add_argument("--data-file")

    validate = subparsers.add_parser("validate", help="validate a semantic trace")
    validate.add_argument("trace", type=Path)
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)

    summarize = subparsers.add_parser("summarize", help="summarize a semantic trace")
    summarize.add_argument("trace", type=Path)

    return parser


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    if args.trace.exists() and args.trace.stat().st_size > 0:
        raise TraceError("trace already exists and is non-empty; refusing to overwrite")
    if not args.goal.strip():
        raise TraceError("--goal must be non-empty")
    if not args.metric_name.strip():
        raise TraceError("--metric-name must be non-empty")
    metadata = _load_object_argument(
        args.metadata_json, args.metadata_file, "metadata"
    )
    metric: dict[str, Any] = {
        "name": args.metric_name,
        "direction": args.metric_direction,
    }
    if args.metric_unit:
        metric["unit"] = args.metric_unit
    data = {
        "collector_version": COLLECTOR_VERSION,
        "goal": args.goal,
        "metric": metric,
        "project_root": args.project_root,
        "metadata": metadata,
    }
    return append_event(
        args.trace,
        "run.started",
        data,
        run_id=args.run_id or new_run_id(),
        source="collector",
    )


def command_event(args: argparse.Namespace) -> dict[str, Any]:
    if args.event in {"run.started", "run.finished", "experiment.completed"}:
        raise TraceError(
            f"use the dedicated command instead of event for {args.event}"
        )
    experiment_events = {
        "hypothesis.proposed",
        "experiment.started",
        "experiment.prepared",
        "evaluation.started",
        "evaluation.completed",
        "decision.selected",
        "decision.applied",
    }
    if args.event in experiment_events and not args.experiment_id:
        raise TraceError(f"{args.event} requires --experiment-id")
    data = _load_object_argument(args.data_json, args.data_file, "data")
    if args.message is not None:
        if "message" in data:
            raise TraceError("message appears in both --message and data")
        data["message"] = args.message
    return append_event(
        args.trace,
        args.event,
        data,
        run_id=args.run_id,
        source=args.source,
        experiment_id=args.experiment_id,
        iteration=args.iteration,
        attempt=args.attempt,
        worker_id=args.worker_id,
    )


def command_experiment(args: argparse.Namespace) -> dict[str, Any]:
    if not args.experiment_id:
        raise TraceError("--experiment-id is required")
    if args.iteration is None:
        raise TraceError("--iteration is required")
    if not args.description.strip():
        raise TraceError("--description must be non-empty")
    if args.status in {"keep", "discard"}:
        missing = [
            name
            for name, value in (
                ("--metric-name", args.metric_name),
                ("--metric-value", args.metric_value),
                ("--metric-direction", args.metric_direction),
            )
            if value is None
        ]
        if missing:
            raise TraceError("successful experiment requires " + ", ".join(missing))
    if args.metric_value is not None and not math.isfinite(args.metric_value):
        raise TraceError("--metric-value must be finite")
    if args.metric_value is not None and (
        not args.metric_name
        or not args.metric_name.strip()
        or not args.metric_direction
    ):
        raise TraceError(
            "--metric-value requires --metric-name and --metric-direction"
        )
    if args.metric_value is not None and not args.metric_source.strip():
        raise TraceError("--metric-source must be non-empty")
    if args.memory_gb is not None and (
        not math.isfinite(args.memory_gb) or args.memory_gb < 0
    ):
        raise TraceError("--memory-gb must be finite and non-negative")
    if args.duration_seconds is not None and (
        not math.isfinite(args.duration_seconds) or args.duration_seconds < 0
    ):
        raise TraceError("--duration-seconds must be finite and non-negative")

    data: dict[str, Any] = {
        "status": args.status,
        "description": args.description,
    }
    if args.commit:
        data["commit"] = args.commit
    if args.metric_value is not None:
        metric = {
            "name": args.metric_name,
            "value": args.metric_value,
            "direction": args.metric_direction,
            "source": args.metric_source,
            "trust": args.metric_trust,
        }
        if args.metric_unit:
            metric["unit"] = args.metric_unit
        data["primary_metric"] = metric
    metrics = _load_object_argument(args.metrics_json, args.metrics_file, "metrics")
    if metrics:
        data["metrics"] = metrics
    artifacts = _load_object_argument(
        args.artifacts_json, args.artifacts_file, "artifacts"
    )
    if artifacts:
        data["artifacts"] = artifacts
    if args.memory_gb is not None:
        data["memory_gb"] = args.memory_gb
    if args.duration_seconds is not None:
        data["duration_seconds"] = args.duration_seconds
    if args.error_kind or args.error_message:
        data["error"] = {
            "kind": args.error_kind or "unknown",
            "message": args.error_message or "",
        }
    return append_event(
        args.trace,
        "experiment.completed",
        data,
        run_id=args.run_id,
        source=args.source,
        experiment_id=args.experiment_id,
        iteration=args.iteration,
        attempt=args.attempt,
        worker_id=args.worker_id,
    )


def command_finish(args: argparse.Namespace) -> dict[str, Any]:
    data = _load_object_argument(args.data_json, args.data_file, "data")
    data["status"] = args.status
    if args.summary is not None:
        data["summary"] = args.summary
    return append_event(
        args.trace,
        "run.finished",
        data,
        run_id=args.run_id,
        source="collector",
    )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = command_init(args)
            print(json.dumps({"trace": str(args.trace), "run_id": result["run_id"]}))
            return 0
        if args.command == "event":
            result = command_event(args)
            print(json.dumps({"event_id": result["event_id"], "seq": result["seq"]}))
            return 0
        if args.command == "experiment":
            result = command_experiment(args)
            print(json.dumps({"event_id": result["event_id"], "seq": result["seq"]}))
            return 0
        if args.command == "finish":
            result = command_finish(args)
            print(json.dumps({"event_id": result["event_id"], "seq": result["seq"]}))
            return 0
        if args.command == "validate":
            result = validate_trace(args.trace, args.max_line_bytes)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif result["valid"]:
                print(
                    f"VALID: {result['records']} records; run_id={result.get('run_id')}"
                )
                for warning in result["warnings"]:
                    print(f"WARNING: {warning}")
            else:
                for error in result["errors"]:
                    print(f"ERROR: {error}", file=sys.stderr)
            return 0 if result["valid"] else 1
        if args.command == "summarize":
            result = summarize_trace(args.trace)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        parser.error(f"unknown command: {args.command}")
    except (TraceError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
