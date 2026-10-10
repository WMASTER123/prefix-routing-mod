# Repository instructions

- Read docs/progress.md, docs/architecture.md and relevant contracts before editing.
- This is an external prefix Router MOD, not an engine fork or KV connector.
- Reuse first. Do not implement a second proxy/index before P1 evidence and an ADR.
- Never represent request history, planned placement or dispatched work as observed KV residency.
- Do not load torch, initialize devices, create threads or perform network I/O on package import.
- Incomplete MOD implementations remain import_only; Manager enablement must fail.
- Do not expand host compatibility ranges or claim speedups without recorded tests.
- No credentials, raw user prompts, model weights, large traces or raw private logs in Git.
- Run CPU checks, integration and wheel validation appropriate to the change. Report not-run honestly.
- No global pip installs or driver/host changes. Server work only in this project's authorized path.
- The new server checkout is /root/work/prefix-routing-mod. /root/work/prefix-router is unrelated legacy work; do not edit or reuse without a separate decision.
- Never use the SSH target vllm-hust-wj-21rc-32011-2 from this project.
- Only clean up processes and files created by the current scoped task. No dummy NPU workloads to bypass idle-stop policy.
- Human-reviewed PRs for subsequent changes; use codex/ branches for agent work. Do not auto-merge.
- Keep scope small, update contract/ADR before breaking interfaces, preserve upstream attribution.
