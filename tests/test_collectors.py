from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import capture_codex  # noqa: E402


class CollectorTests(unittest.TestCase):
    def run_cli(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def make_fake_codex(self, directory: str) -> Path:
        fake = Path(directory) / "fake_codex.py"
        fake.write_text(
            textwrap.dedent(
                """\
                import hashlib
                import json
                import os
                import sys

                expected_argv = ["exec", "--json", "-"]
                if sys.argv[1:] != expected_argv:
                    print(f"unexpected argv: {sys.argv[1:]!r}", file=sys.stderr)
                    raise SystemExit(90)

                prompt = sys.stdin.buffer.read()
                if hashlib.sha256(prompt).hexdigest() != os.environ["EXPECTED_PROMPT_SHA256"]:
                    print("prompt hash mismatch", file=sys.stderr)
                    raise SystemExit(91)

                secret = os.environ["OPENAI_API_KEY"]
                if os.environ.get("FAKE_CODEX_MALFORMED") == "1":
                    sys.stdout.write("{not-json}\\n")
                else:
                    records = [
                        {"type": "thread.started", "thread_id": "thread-fixture"},
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "accessToken": secret,
                                "text": f"Authorization: Bearer {secret}",
                            },
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 7, "output_tokens": 3},
                        },
                    ]
                    for record in records:
                        print(json.dumps(record, separators=(",", ":")), flush=True)

                sys.stderr.write(f"Authorization: Bearer {secret}\\n")
                sys.stderr.write("-----BEGIN PRIVATE KEY-----\\n")
                sys.stderr.write(f"private-material-{secret}\\n")
                sys.stderr.write("-----END PRIVATE KEY-----\\n")
                sys.stderr.flush()
                raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
                """
            ),
            encoding="utf-8",
            newline="\n",
        )

        if os.name == "nt":
            wrapper = Path(directory) / "fake-codex.cmd"
            wrapper.write_text(
                f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n',
                encoding="utf-8",
                newline="",
            )
        else:
            wrapper = Path(directory) / "fake-codex"
            wrapper.write_text(
                "#!/bin/sh\n"
                + f"exec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} \"$@\"\n",
                encoding="utf-8",
                newline="\n",
            )
            wrapper.chmod(0o700)
        return wrapper

    def run_capture(
        self,
        output: Path,
        prompt: Path,
        cwd: Path,
        executable: Path,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "capture_codex.py"),
                "capture",
                "--output-dir",
                str(output),
                "--prompt-file",
                str(prompt),
                "--cwd",
                str(cwd),
                "--codex",
                str(executable),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_semantic_trace_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            commands = [
                (
                    "init",
                    str(trace),
                    "--goal",
                    "lower validation bits per byte",
                    "--metric-name",
                    "val_bpb",
                    "--metric-direction",
                    "minimize",
                    "--metric-unit",
                    "bpb",
                    "--project-root",
                    ".",
                ),
                (
                    "event",
                    str(trace),
                    "hypothesis.proposed",
                    "--experiment-id",
                    "exp-0001",
                    "--iteration",
                    "1",
                    "--message",
                    "baseline; no code change",
                ),
                (
                    "experiment",
                    str(trace),
                    "--experiment-id",
                    "exp-0001",
                    "--iteration",
                    "1",
                    "--attempt",
                    "1",
                    "--status",
                    "keep",
                    "--description",
                    "baseline",
                    "--metric-name",
                    "val_bpb",
                    "--metric-value",
                    "0.9979",
                    "--metric-direction",
                    "minimize",
                    "--metric-unit",
                    "bpb",
                ),
                (
                    "finish",
                    str(trace),
                    "--status",
                    "completed",
                    "--summary",
                    "baseline completed",
                ),
            ]
            for command in commands:
                result = self.run_cli("trace_jsonl.py", *command)
                self.assertEqual(result.returncode, 0, result.stderr)

            validated = self.run_cli("trace_jsonl.py", "validate", str(trace), "--json")
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["valid"])

            summarized = self.run_cli("trace_jsonl.py", "summarize", str(trace))
            self.assertEqual(summarized.returncode, 0, summarized.stderr)
            summary = json.loads(summarized.stdout)
            self.assertEqual(summary["experiment_status_counts"], {"keep": 1})
            self.assertEqual(summary["best"]["experiment_id"], "exp-0001")

    def test_native_verify_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            records = [
                {"type": "thread.started", "thread_id": "thread-test"},
                {"type": "item.completed", "item": {"type": "agent_message"}},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                },
            ]
            events.write_bytes(
                b"".join(
                    json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
                    for record in records
                )
            )

            verified = self.run_cli("capture_codex.py", "verify", str(events))
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["valid"])

            stats = self.run_cli("capture_codex.py", "stats", str(events))
            self.assertEqual(stats.returncode, 0, stats.stderr)
            report = json.loads(stats.stdout)
            self.assertEqual(report["records"], 3)
            self.assertTrue(report["terminal_event_seen"])

    def test_stdout_guard_records_oversized_content(self) -> None:
        chunks = iter([(b"12345\n", False, False), (b"", True, False)])
        state = capture_codex.CaptureState()
        output = io.BytesIO()
        with mock.patch.object(capture_codex, "_read_bounded_line", side_effect=chunks):
            capture_codex._drain_stdout(io.BytesIO(), output, state, [], 4)

        self.assertEqual(output.getvalue(), b"")
        self.assertEqual(len(state.protocol_errors), 1)
        self.assertIn("exceeds 4 bytes", state.protocol_errors[0]["error"])

    def test_capture_end_to_end_redaction_manifest_and_exit_propagation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "program.md"
            prompt_bytes = "run the synthetic experiment 🧪\n".encode("utf-8")
            prompt.write_bytes(prompt_bytes)
            fake_codex = self.make_fake_codex(directory)
            secret = "synthetic-e2e-secret-123456789"
            environment = os.environ.copy()
            environment.update(
                {
                    "OPENAI_API_KEY": secret,
                    "EXPECTED_PROMPT_SHA256": hashlib.sha256(prompt_bytes).hexdigest(),
                }
            )

            success_dir = root / "capture-success"
            result = self.run_capture(
                success_dir, prompt, root, fake_codex, environment
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((success_dir / "manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["collector_status"], "completed")
            self.assertEqual(manifest["codex_exit_code"], 0)
            self.assertEqual(manifest["records"], 3)
            self.assertTrue(manifest["terminal_event_seen"])
            self.assertEqual(
                manifest["prompt"]["sha256"], hashlib.sha256(prompt_bytes).hexdigest()
            )

            for name in ("events.jsonl", "stderr.log", "manifest.json"):
                content = (success_dir / name).read_bytes()
                self.assertNotIn(secret.encode("utf-8"), content)
            self.assertIn(b"[REDACTED:", (success_dir / "events.jsonl").read_bytes())
            self.assertIn(b"[REDACTED:", (success_dir / "stderr.log").read_bytes())
            self.assertIn(
                b"[REDACTED:private-key]",
                (success_dir / "stderr.log").read_bytes(),
            )

            for name in ("events.jsonl", "stderr.log"):
                content = (success_dir / name).read_bytes()
                self.assertEqual(
                    manifest["files"][name]["sha256"], hashlib.sha256(content).hexdigest()
                )
                self.assertEqual(manifest["files"][name]["bytes"], len(content))

            failed_environment = environment | {"FAKE_CODEX_EXIT": "42"}
            failed_dir = root / "capture-failed"
            failed = self.run_capture(
                failed_dir, prompt, root, fake_codex, failed_environment
            )
            self.assertEqual(failed.returncode, 42, failed.stderr)
            failed_manifest = json.loads(
                (failed_dir / "manifest.json").read_text("utf-8")
            )
            self.assertEqual(failed_manifest["collector_status"], "codex_failed")
            self.assertEqual(failed_manifest["codex_exit_code"], 42)
            self.assertTrue((failed_dir / "events.jsonl").is_file())

    def test_capture_malformed_stream_has_collector_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "program.md"
            prompt_bytes = b"synthetic prompt\n"
            prompt.write_bytes(prompt_bytes)
            fake_codex = self.make_fake_codex(directory)
            environment = os.environ.copy()
            environment.update(
                {
                    "OPENAI_API_KEY": "synthetic-malformed-secret",
                    "EXPECTED_PROMPT_SHA256": hashlib.sha256(prompt_bytes).hexdigest(),
                    "FAKE_CODEX_MALFORMED": "1",
                    "FAKE_CODEX_EXIT": "42",
                }
            )
            output = root / "capture-malformed"
            result = self.run_capture(output, prompt, root, fake_codex, environment)
            self.assertEqual(result.returncode, 65, result.stderr)
            manifest = json.loads((output / "manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["collector_status"], "malformed_stream")
            self.assertEqual(manifest["codex_exit_code"], 42)
            self.assertTrue(manifest["protocol_errors"])

    def test_corrupt_tail_is_never_modified_by_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            initialized = self.run_cli(
                "trace_jsonl.py",
                "init",
                str(trace),
                "--goal",
                "synthetic goal",
                "--metric-name",
                "score",
                "--metric-direction",
                "maximize",
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            with trace.open("ab") as handle:
                handle.write(b'{"truncated":')
            before = trace.read_bytes()

            appended = self.run_cli(
                "trace_jsonl.py",
                "event",
                str(trace),
                "hypothesis.proposed",
                "--experiment-id",
                "exp-corrupt",
                "--message",
                "must not be appended",
            )
            self.assertNotEqual(appended.returncode, 0)
            self.assertEqual(trace.read_bytes(), before)

    def test_native_validator_rejects_duplicate_keys_and_nan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            for payload in (
                b'{"type":"one","type":"two"}\n',
                b'{"type":"turn.completed","value":NaN}\n',
            ):
                with self.subTest(payload=payload):
                    events.write_bytes(payload)
                    verified = self.run_cli("capture_codex.py", "verify", str(events))
                    self.assertNotEqual(verified.returncode, 0)
                    self.assertFalse(json.loads(verified.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
