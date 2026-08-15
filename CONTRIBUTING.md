# Contributing

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

Thank you for helping improve Autoresearch Trace Collector.

## Before opening a change

- Use Python 3.10 or newer.
- Use only synthetic test traces; never commit real prompts, credentials, private source, or captured run artifacts.
- Discuss breaking schema or CLI changes in an issue before implementation.
- Keep the implementation dependency-free unless a dependency is clearly justified.

## Development workflow

1. Create a focused branch.
2. Update tests for behavior changes.
3. Update `references/schema.md` and `references/schema.zh-CN.md` together when the data contract changes.
4. Update `README.md` and `README.zh-CN.md` together for user-facing changes.
5. Run:

```text
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

## Pull requests

Explain the problem, the chosen behavior, compatibility impact, and tests performed. Avoid unrelated formatting or refactoring. Security reports should follow [SECURITY.md](SECURITY.md), not a public pull request.
