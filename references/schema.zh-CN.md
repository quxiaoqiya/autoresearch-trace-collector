# Autoresearch Trace Schema

当其他生产者或消费者需要与本 Skill 集成时，请阅读本参考。内置校验器始终是可执行规范。

## 语义 JSONL 信封

UTF-8 文件的每一行都是一个 JSON 对象。文件以 `\n` 结尾，并且只能追加。

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

必填字段为 `schema`、`event_id`、`event`、`ts`、`run_id`、`seq`、`source` 和对象类型的 `data`。实验范围内的记录还带有 `experiment_id`；`iteration`、`attempt` 和 `worker_id` 可选。物理行顺序和 `seq` 是权威顺序，时间戳仅用于描述。

`event` 是小写点分名称。标准事件包括：

- `run.started`、`run.resumed`、`run.finished`
- `hypothesis.proposed`
- `experiment.started`、`experiment.prepared`、`experiment.completed`
- `evaluation.started`、`evaluation.completed`
- `decision.selected`、`decision.applied`
- `error.observed`
- `codex.capture.started`、`codex.capture.completed`

为保持向前兼容，允许未知的点分事件名，但仍必须包含公共信封字段。

## 运行数据

`run.started` 的序号必须为 1，内容包括：

```json
{
  "collector_version": "1.0.0",
  "goal": "lower validation bits per byte",
  "metric": {"name": "val_bpb", "direction": "minimize", "unit": "bpb"},
  "project_root": ".",
  "metadata": {}
}
```

指标方向只能使用 `minimize` 或 `maximize`。只有在明确提供时才把环境或硬件事实放入 `metadata`；不要转储进程环境变量。

## 实验结果数据

`experiment.completed` 是自包含记录，消费者无需连接此前事件：

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

`status` 可取 `keep`、`discard` 或 `crash`。`keep` 和 `discard` 必须包含有限值主指标。若崩溃时没有可信值，应省略 `primary_metric`。可变候选代码生成的指标使用 `reported`；只有受保护的外部 evaluator 才应标为 `verified`。

为了兼容 Karpathy autoresearch，这些字段可按如下方式映射到 `results.tsv`：`commit` 使用七字符前缀，主 `val_bpb` 格式化为六位小数，`memory_gb` 格式化为一位小数，`status` 和 `description` 直接映射。上游为崩溃使用零哨兵；JSONL 中应保留 `null`/缺失，仅在必要的 TSV 投影中写零。

## Codex 原生捕获

`capture_codex.py` 保留 Codex stdout 事件的原生顶层结构。它校验每一行都是带字符串 `type` 的 UTF-8 JSON 对象，递归执行尽力脱敏，然后重新序列化为单行。它不会向 `events.jsonl` 添加语义信封字段。

`manifest.json` 会记录捕获 ID、prompt 哈希与字节数（不记录 prompt 原文）、已清理 argv、开始/结束时间、Codex 返回码、采集器状态、事件与 item 计数、是否出现终止事件，以及最终产物的 SHA-256 和大小。`events.jsonl` 与 `stderr.log` 之间不保证跨流顺序。

## 校验不变量

- 拒绝 BOM、NUL、无效 UTF-8、空行、重复 JSON 键、NaN/Infinity、非对象行、超长行和缺失末尾换行。
- 要求语义序号从 1 开始连续、单一运行 ID、唯一事件 ID、恰好一个 `run.started`、最多一个 `run.finished`，且每个实验 ID 最多一个 `experiment.completed`。
- 每个非崩溃主指标的名称、方向和单位必须与 `run.started` 声明的目标一致。
- 中断后允许存在未完成的运行或实验，并在摘要中报告。
- 当前文件尾部不完整或损坏时拒绝追加，绝不隐式修复。
- 写入前脱敏已知敏感键名和常见凭据模式；不要把脱敏理解为保密保证。
