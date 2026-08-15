# Autoresearch Trace Collector

[English](README.md) | [简体中文](README.zh-CN.md)

Autoresearch Trace Collector is an independent Codex skill and dependency-free Python toolkit for writing validated, append-only JSONL experiment traces and capturing the native event stream emitted by separate `codex exec --json` runs.

It is designed for loops that repeatedly form a hypothesis, change code, evaluate a primary metric, and keep, discard, or mark the experiment as crashed. It is not an official OpenAI project and does not include code from Karpathy's autoresearch project.

## Collection modes

| Mode | Purpose | Output |
| --- | --- | --- |
| Semantic | Record hypotheses, experiment outcomes, metrics, and decisions in the current task | `trace.jsonl` |
| Capture | Launch and capture a separate `codex exec` process | `events.jsonl`, `stderr.log`, `manifest.json` |
| Combined | Link a native capture to a semantic experiment with `--trace` | Both sets |

## Key features

- `autoresearch.trace/v1` semantic event envelope with contiguous sequence numbers, UUID event IDs, one run ID, and RFC 3339 UTC timestamps.
- Self-contained `keep`, `discard`, and `crash` experiment results.
- Metric consistency checks against the objective declared by `run.started`.
- Append-only writes with a cooperative local lock, validation before append, flush, and `fsync`.
- Strict UTF-8 and JSONL validation, including duplicate-key, non-finite-number, blank-line, truncated-tail, and line-size checks.
- Separate storage for native Codex stdout events, stderr diagnostics, and capture metadata.
- Recursive best-effort redaction of common credential keys, tokens, and private-key blocks before persistence.
- Windows, macOS, and Linux test matrix using only the Python standard library.

## Requirements

- Python 3.10 or newer.
- Codex CLI available on `PATH` only when using capture mode.
- A local filesystem for the writer's cooperative lock. Network filesystem locking is not guaranteed.

## Install as a Codex skill

Clone the repository so that `SKILL.md` is directly inside the skill directory:

```text
git clone git@github.com:quxiaoqiya/autoresearch-trace-collector.git "$HOME/.agents/skills/autoresearch-trace-collector"
```

Restart Codex if the skill is not detected immediately, then invoke it explicitly:

```text
$autoresearch-trace-collector Record this autoresearch run; the primary metric is val_bpb and lower is better.
```

The scripts can also be run directly without installing the skill.

## Quick start: semantic trace

Set `<skill-dir>` to this repository's absolute path.

Initialize a trace:

```text
python "<skill-dir>/scripts/trace_jsonl.py" init ".autoresearch/traces/demo/trace.jsonl" --goal "lower validation bits per byte" --metric-name val_bpb --metric-direction minimize --metric-unit bpb --project-root .
```

Record the hypothesis before changing code:

```text
python "<skill-dir>/scripts/trace_jsonl.py" event ".autoresearch/traces/demo/trace.jsonl" hypothesis.proposed --experiment-id exp-0001 --iteration 1 --message "baseline; no code change"
```

Append one self-contained result after evaluation and after verifying the keep/discard action:

```text
python "<skill-dir>/scripts/trace_jsonl.py" experiment ".autoresearch/traces/demo/trace.jsonl" --experiment-id exp-0001 --iteration 1 --attempt 1 --status keep --description "baseline" --commit "<full-commit-sha>" --metric-name val_bpb --metric-value 0.997900 --metric-direction minimize --metric-unit bpb --metric-source train_stdout --metric-trust reported --memory-gb 44.0 --duration-seconds 325.9
```

For a crash without a trustworthy metric, omit `--metric-value` instead of recording zero:

```text
python "<skill-dir>/scripts/trace_jsonl.py" event ".autoresearch/traces/demo/trace.jsonl" hypothesis.proposed --experiment-id exp-0002 --iteration 2 --message "test whether the larger batch completes within the evaluation limit"
python "<skill-dir>/scripts/trace_jsonl.py" experiment ".autoresearch/traces/demo/trace.jsonl" --experiment-id exp-0002 --iteration 2 --attempt 1 --status crash --description "training timed out" --error-kind timeout --error-message "evaluation exceeded the time limit"
```

Validate the open trace before starting a separate capture:

```text
python "<skill-dir>/scripts/trace_jsonl.py" validate ".autoresearch/traces/demo/trace.jsonl"
python "<skill-dir>/scripts/trace_jsonl.py" summarize ".autoresearch/traces/demo/trace.jsonl"
```

## Capture a separate Codex run

Use a new, non-existing output directory and pass the prompt through a file or stdin:

```text
python "<skill-dir>/scripts/capture_codex.py" capture --output-dir ".autoresearch/traces/demo/codex/exp-0001" --prompt-file "program.md" --cwd . --trace ".autoresearch/traces/demo/trace.jsonl" --experiment-id exp-0001 --iteration 1 --attempt 1 -- --full-auto
```

The collector adds `exec --json` automatically. Arguments after `--` are forwarded to Codex. Options such as `--full-auto` can allow the child process to modify the target workspace; use them only where that behavior is intended.

Verify or summarize the captured event stream:

```text
python "<skill-dir>/scripts/capture_codex.py" verify ".autoresearch/traces/demo/codex/exp-0001/events.jsonl"
python "<skill-dir>/scripts/capture_codex.py" stats ".autoresearch/traces/demo/codex/exp-0001/events.jsonl"
```

After all linked captures and experiments are complete, close the semantic trace:

```text
python "<skill-dir>/scripts/trace_jsonl.py" finish ".autoresearch/traces/demo/trace.jsonl" --status completed --summary "best val_bpb: 0.997900"
python "<skill-dir>/scripts/trace_jsonl.py" validate ".autoresearch/traces/demo/trace.jsonl"
```

### Capture output

- `events.jsonl`: validated and redacted native Codex stdout events.
- `stderr.log`: redacted progress and diagnostics, never mixed into JSONL.
- `manifest.json`: capture ID, timestamps, sanitized argv, prompt hash and byte length, exit status, event counts, warnings, and artifact hashes.

Normal Codex exit codes are propagated unless collection itself fails. The wrapper returns 65 for a malformed stream, 69 when Codex cannot be launched, 74 for collector I/O, prompt, or semantic-link failures, and 130 when interrupted. These numbers can overlap with a child process exit code; use `manifest.json` to distinguish the source and as the authoritative capture record.

## Event model

Canonical semantic events include `run.started`, `hypothesis.proposed`, `experiment.started`, `evaluation.completed`, `decision.selected`, `experiment.completed`, and `run.finished`. Unknown lowercase dotted events remain valid for forward compatibility.

See the [schema reference](references/schema.md) or its [Chinese translation](references/schema.zh-CN.md). The bundled validators are the executable specification.

## Autoresearch compatibility

The experiment result fields can be projected to the `results.tsv` convention used by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch). This project does not provide a TSV exporter and does not replace the upstream evaluator or control loop; JSONL is an additional audit record.

## Security and privacy

Trace data may contain source code, tool output, paths, thread IDs, or other sensitive material. Redaction is defense in depth, not proof that no secret can leak. Do not collect authentication files, environment dumps, raw credentials, private datasets, or full prompts. Review artifacts before sharing them and see [SECURITY.md](SECURITY.md).

## Limitations

- Does not capture hidden chain-of-thought, system/developer prompts, events emitted before activation, or the native stream of the current Codex task.
- Does not run experiments, judge scientific validity, manage Git commits, calculate semantic artifact hashes, or repair corrupted traces.
- `metric-trust=verified` is a caller assertion; the collector cannot prove that an evaluator is protected.
- Native stdout and stderr are drained concurrently, so ordering across their separate files is not guaranteed.
- Every semantic append validates the existing trace; this favors integrity over high-throughput telemetry.
- Artifact hashes prove byte identity, not the scientific validity of a result.

## Development

```text
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

Contributions should update both language versions of user-facing documentation. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Related documentation

- [Codex skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex non-interactive mode and JSONL output](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Karpathy autoresearch](https://github.com/karpathy/autoresearch)

## License

Released under the [MIT License](LICENSE).
