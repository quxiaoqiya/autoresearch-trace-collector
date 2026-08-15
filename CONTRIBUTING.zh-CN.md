# 参与贡献

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

感谢你帮助改进 Autoresearch Trace Collector。

## 提交变更之前

- 使用 Python 3.10 或更高版本。
- 测试只能使用合成 Trace；不要提交真实 prompt、凭据、私有源码或运行捕获产物。
- 对破坏性 Schema 或 CLI 变更，应先通过 Issue 讨论。
- 除非确有充分理由，否则保持实现无第三方依赖。

## 开发流程

1. 创建目标单一的分支。
2. 行为发生变化时同步更新测试。
3. 数据契约变化时同时更新 `references/schema.md` 和 `references/schema.zh-CN.md`。
4. 面向用户的行为变化时同时更新 `README.md` 和 `README.zh-CN.md`。
5. 运行：

```text
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

## Pull Request

请说明问题、选定行为、兼容性影响和已执行测试。避免夹带无关格式化或重构。安全问题应按照 [SECURITY.zh-CN.md](SECURITY.zh-CN.md) 报告，不要通过公开 Pull Request 披露。
