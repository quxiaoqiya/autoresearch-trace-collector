# Autoresearch trace schema

Read this reference when integrating another producer or consumer with the skill. The bundled validators remain the executable specification.

## Semantic JSONL envelope

Each UTF-8 line is one JSON object. Files end with `\n` and are append-only.

```json
{
  "schema": "autoresearch.trace/v1",
  "event_id": "d52f6c46-262e-4ef5-87ae-bfb5d9b01801",
  "event": "experiment.completed",
  "ts": "2026-08-15T01:02:03.456000Z",
  "run_id": "ar-20260815T010200Z-72f9c94a",
  "seq": 8,
  "source": "semantic",
  "experiment_id": "exp-0001",
  "iteration": 1,
  "attempt": 1,
  "data": {}
}
```

Required fields are `schema`, `event_id`, `event`, `ts`, `run_id`, `seq`, `source`, and object-valued `data`. Experiment-scoped records also carry `experiment_id`; `iteration`, `attempt`, and `worker_id` are optional. Physical line order and `seq` are authoritative; timestamps are descriptive.

`event` is a lowercase dotted name. Canonical events are:

- `run.started`, `run.resumed`, `run.finished`
- `hypothesis.proposed`
- `experiment.started`, `experiment.prepared`, `experiment.completed`
- `evaluation.started`, `evaluation.completed`
- `decision.selected`, `decision.applied`
- `error.observed`
- `codex.capture.started`, `codex.capture.completed`

Unknown dotted events are allowed for forward compatibility but still require the common envelope.

## Run data

`run.started` is sequence 1 and includes:

```json
{
  "collector_version": "1.0.0",
  "goal": "lower validation bits per byte",
  "metric": {"name": "val_bpb", "direction": "minimize", "unit": "bpb"},
  "project_root": ".",
  "metadata": {}
}
```

Use only `minimize` or `maximize` for direction. Store environment or hardware facts in `metadata` only when deliberately provided; never dump the process environment.

## Experiment result data

`experiment.completed` is self-contained so a consumer need not join earlier events:

```json
{
  "status": "keep",
  "description": "baseline",
  "commit": "40-character-commit-sha",
  "primary_metric": {
    "name": "val_bpb",
    "value": 0.9979,
    "direction": "minimize",
    "unit": "bpb",
    "source": "train_stdout",
    "trust": "reported"
  },
  "metrics": {"peak_vram_mb": 45060.2, "mfu_percent": 39.8},
  "memory_gb": 44.0,
  "duration_seconds": 325.9,
  "artifacts": {
    "run_log": {"path": ".autoresearch/artifacts/exp-0001.log", "sha256": "...", "bytes": 12345}
  }
}
```

`status` is `keep`, `discard`, or `crash`. `keep` and `discard` require a finite primary metric. A crash omits `primary_metric` when no trustworthy value exists. `trust` is `reported` for metrics produced by mutable candidate code and `verified` only for a protected external evaluator.

For compatibility with Karpathy autoresearch, the fields map to `results.tsv` as follows: `commit` becomes the seven-character commit prefix, the primary `val_bpb` value is formatted to six decimals, `memory_gb` to one decimal, and `status`/`description` map directly. Upstream uses zero sentinels for crashes; retain `null`/absence in JSONL and write zero only in the required TSV projection.

## Native Codex capture

`capture_codex.py` keeps Codex stdout events in their native top-level shape. It validates that every line is a UTF-8 JSON object with a string `type`, applies recursive best-effort redaction, then reserializes it as one line. It does not add semantic envelope fields to `events.jsonl`.

`manifest.json` records the capture ID, prompt hash and byte length (not prompt text), sanitized argv, start/end timestamps, Codex return code, collector status, event and item counts, terminal-event presence, and SHA-256/size of finalized artifacts. Cross-stream ordering between `events.jsonl` and `stderr.log` is not guaranteed.

## Validation invariants

- Reject BOM, NUL, invalid UTF-8, blank lines, duplicate JSON keys, NaN/Infinity, non-object lines, oversized lines, and a missing final newline.
- Require contiguous semantic sequence numbers starting at 1, one run ID, unique event IDs, one `run.started`, at most one `run.finished`, and at most one `experiment.completed` per experiment ID.
- Require each non-crash primary metric's name, direction, and unit to match the objective declared by `run.started`.
- Allow an unfinished run or experiment after interruption; report it in the summary.
- Refuse to append when the current tail is incomplete or malformed. Never repair implicitly.
- Redact known sensitive key names and common credential patterns before writing. Do not interpret redaction as a confidentiality guarantee.
