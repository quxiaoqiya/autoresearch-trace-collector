# Autoresearch Trace Collector

[English](README.md) | [简体中文](README.zh-CN.md)

Autoresearch Trace Collector 是一个独立开发的 Codex Skill 和无第三方依赖的 Python 工具集，用于生成可校验、仅追加的 JSONL 实验记录，并采集独立 `codex exec --json` 运行公开输出的原生事件流。

它面向“提出假设、修改代码、评测主指标、保留/丢弃/标记崩溃”的循环。本项目不是 OpenAI 官方项目，也不包含 Karpathy autoresearch 项目的源码。

## 两种采集模式

| 模式 | 用途 | 输出 |
| --- | --- | --- |
| 语义模式 | 在当前任务中记录假设、实验结果、指标和决策 | `trace.jsonl` |
| 捕获模式 | 启动并采集一个独立的 `codex exec` 进程 | `events.jsonl`、`stderr.log`、`manifest.json` |
| 组合模式 | 使用 `--trace` 将原生捕获关联到语义实验 | 两组文件 |

## 主要功能

- `autoresearch.trace/v1` 语义事件信封，包含连续序号、UUID 事件 ID、单一运行 ID 和 RFC 3339 UTC 时间。
- 自包含的 `keep`、`discard` 和 `crash` 实验结果。
- 根据 `run.started` 声明的目标检查主指标名称、方向和单位。
- 仅追加写入、协作式本地文件锁、写入前校验、flush 和 `fsync`。
- 严格校验 UTF-8 和 JSONL，包括重复键、非有限数、空行、截断尾部和超长行。
- 分开保存 Codex 原生 stdout 事件、stderr 诊断和捕获元数据。
- 持久化之前递归、尽力脱敏常见凭据字段、令牌和私钥块。
- 仅使用 Python 标准库，并在 Windows、macOS 和 Linux 测试矩阵中运行。

## 环境要求

- Python 3.10 或更高版本。
- 仅捕获模式要求 Codex CLI 位于 `PATH` 中。
- 写入器的协作式锁要求本地文件系统；不保证网络文件系统上的锁语义。

## 安装为 Codex Skill

克隆仓库，确保 `SKILL.md` 直接位于 Skill 目录中：

```text
git clone git@github.com:quxiaoqiya/autoresearch-trace-collector.git "$HOME/.agents/skills/autoresearch-trace-collector"
```

如果 Codex 没有立即发现 Skill，请重启 Codex，然后显式调用：

```text
$autoresearch-trace-collector 记录本次 autoresearch；主指标为 val_bpb，越低越好。
```

不安装 Skill 也可以直接运行其中的脚本。

## 快速开始：语义化 Trace

将 `<skill-dir>` 替换为本仓库的绝对路径。

初始化 Trace：

```text
python "<skill-dir>/scripts/trace_jsonl.py" init ".autoresearch/traces/demo/trace.jsonl" --goal "lower validation bits per byte" --metric-name val_bpb --metric-direction minimize --metric-unit bpb --project-root .
```

在修改代码之前记录假设：

```text
python "<skill-dir>/scripts/trace_jsonl.py" event ".autoresearch/traces/demo/trace.jsonl" hypothesis.proposed --experiment-id exp-0001 --iteration 1 --message "baseline; no code change"
```

评测完成并确认保留/丢弃操作之后，追加一个自包含结果：

```text
python "<skill-dir>/scripts/trace_jsonl.py" experiment ".autoresearch/traces/demo/trace.jsonl" --experiment-id exp-0001 --iteration 1 --attempt 1 --status keep --description "baseline" --commit "<full-commit-sha>" --metric-name val_bpb --metric-value 0.997900 --metric-direction minimize --metric-unit bpb --metric-source train_stdout --metric-trust reported --memory-gb 44.0 --duration-seconds 325.9
```

发生崩溃且没有可信指标时，应省略 `--metric-value`，不要写成零：

```text
python "<skill-dir>/scripts/trace_jsonl.py" event ".autoresearch/traces/demo/trace.jsonl" hypothesis.proposed --experiment-id exp-0002 --iteration 2 --message "test whether the larger batch completes within the evaluation limit"
python "<skill-dir>/scripts/trace_jsonl.py" experiment ".autoresearch/traces/demo/trace.jsonl" --experiment-id exp-0002 --iteration 2 --attempt 1 --status crash --description "training timed out" --error-kind timeout --error-message "evaluation exceeded the time limit"
```

启动独立捕获之前，先校验尚未关闭的 Trace：

```text
python "<skill-dir>/scripts/trace_jsonl.py" validate ".autoresearch/traces/demo/trace.jsonl"
python "<skill-dir>/scripts/trace_jsonl.py" summarize ".autoresearch/traces/demo/trace.jsonl"
```

## 捕获独立 Codex 运行

使用一个尚不存在的新输出目录，并通过文件或 stdin 传入 prompt：

```text
python "<skill-dir>/scripts/capture_codex.py" capture --output-dir ".autoresearch/traces/demo/codex/exp-0001" --prompt-file "program.md" --cwd . --trace ".autoresearch/traces/demo/trace.jsonl" --experiment-id exp-0001 --iteration 1 --attempt 1 -- --full-auto
```

采集器会自动添加 `exec --json`。`--` 后的参数会转发给 Codex。`--full-auto` 等选项可能允许子进程修改目标工作区，只应在确实需要时使用。

校验或汇总采集到的事件流：

```text
python "<skill-dir>/scripts/capture_codex.py" verify ".autoresearch/traces/demo/codex/exp-0001/events.jsonl"
python "<skill-dir>/scripts/capture_codex.py" stats ".autoresearch/traces/demo/codex/exp-0001/events.jsonl"
```

所有关联捕获和实验结束后，再关闭语义 Trace：

```text
python "<skill-dir>/scripts/trace_jsonl.py" finish ".autoresearch/traces/demo/trace.jsonl" --status completed --summary "best val_bpb: 0.997900"
python "<skill-dir>/scripts/trace_jsonl.py" validate ".autoresearch/traces/demo/trace.jsonl"
```

### 捕获输出

- `events.jsonl`：经过校验和脱敏的 Codex 原生 stdout 事件。
- `stderr.log`：经过脱敏的进度与诊断，永不混入 JSONL。
- `manifest.json`：捕获 ID、时间、已清理的 argv、prompt 哈希与字节数、退出状态、事件计数、警告和产物哈希。

除非采集器自身失败，否则会传播 Codex 的正常退出码。包装器在事件流损坏时返回 65、无法启动 Codex 时返回 69、采集器 I/O、prompt 或语义关联失败时返回 74，被中断时返回 130。这些数字可能与子进程自身退出码重合；应通过 `manifest.json` 区分来源，并将其作为捕获记录的权威依据。

## 事件模型

标准语义事件包括 `run.started`、`hypothesis.proposed`、`experiment.started`、`evaluation.completed`、`decision.selected`、`experiment.completed` 和 `run.finished`。为保持向前兼容，也允许未知的小写点分事件名。

请参阅[英文 Schema](references/schema.md)或[中文 Schema](references/schema.zh-CN.md)。内置校验器是可执行规范。

## 与 Autoresearch 的兼容关系

实验结果字段可以映射到 [Karpathy autoresearch](https://github.com/karpathy/autoresearch) 使用的 `results.tsv` 约定。本项目不提供 TSV 导出器，也不会替代上游 evaluator 或控制循环；JSONL 是额外的审计记录。

## 安全与隐私

Trace 可能包含源码、工具输出、路径、线程 ID 或其他敏感材料。脱敏只是纵深防御，不能证明绝无秘密泄露。不要采集身份认证文件、环境变量转储、原始凭据、私有数据集或完整 prompt。共享前应人工检查产物，并阅读 [SECURITY.zh-CN.md](SECURITY.zh-CN.md)。

## 功能边界

- 不采集隐藏思维过程、system/developer prompt、启用前事件或当前 Codex 任务的原生事件流。
- 不自动运行实验、判断科学结论、管理 Git commit、计算语义产物哈希或修复损坏 Trace。
- `metric-trust=verified` 只是调用方声明；采集器无法证明 evaluator 确实受到保护。
- 原生 stdout 和 stderr 会并发读取，因此两个文件之间没有可靠的全局顺序。
- 每次语义追加都会校验现有 Trace；它优先保证完整性，不适合高吞吐遥测。
- 产物哈希只能证明字节一致，不能证明实验结论科学有效。

## 开发

```text
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

面向用户的文档变更应同时更新两种语言。详情参阅 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。

## 相关资料

- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex 非交互模式与 JSONL 输出](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Karpathy autoresearch](https://github.com/karpathy/autoresearch)

## 许可证

本项目使用 [MIT License](LICENSE) 发布。
