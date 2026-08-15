# Security policy

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

## Supported versions

This project is not yet released. Security fixes currently target the default branch; no released-version support commitment is in effect.

## Reporting a vulnerability

Use GitHub private vulnerability reporting when it is enabled for this repository. Do not publish credentials, private trace contents, unredacted prompts, or exploit details in a public issue. If private reporting is unavailable, open a public issue containing no sensitive details and ask the maintainer for a private contact channel.

Include the affected version, platform, minimal reproduction using synthetic data, expected behavior, and impact. Remove all real secrets before attaching files.

## Trace-data handling

- Treat `trace.jsonl`, `events.jsonl`, `stderr.log`, and `manifest.json` as sensitive by default.
- Redaction is best effort and may miss unknown encodings, formats, or ordinary-text secrets.
- Do not collect authentication files, environment dumps, cookies, private keys, raw credentials, private datasets, or full prompts.
- If a credential appears in any artifact, revoke or rotate it before doing anything else; deleting the file alone is not sufficient.
- Review every artifact before sharing it or attaching it to an issue.

## Scope

Useful reports include credential-redaction bypasses, unsafe path handling, file-integrity failures, lock or append corruption, command-injection paths, and cases where malformed input is accepted as valid.

The collector does not claim to provide a confidentiality boundary, tamper-proof storage, sandboxing, or scientific-result verification.
