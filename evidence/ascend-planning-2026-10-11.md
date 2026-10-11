# Ascend 规划修订验证记录

日期：2026-10-11。范围：文档、ADR 和文档校验；不修改运行功能、依赖锁、manifest 或 config schema。

基于提交 `ee445f92dafc0da6f86f94f7ff0a91a769c3b3ba`，在 `codex/ascend-first-plan` 分支修订。工作区三份规划与 docs/planning 正文同步，仅转换资料链接；docs/reference 保留原始快照。

## 本地执行

Windows / Python 3.12.13，现有隔离 venv：

| 命令 | 结果 |
|---|---|
| python tools/check.py --suite cpu | 通过：ruff 检查/格式，29 项测试；含当前文档相对链接、绝对路径拒绝与代码围栏检查 |
| python tools/check.py --suite integration | 通过：3 项合成 loopback 后端测试；不是 Router 集成 |
| python -m build | sdist/wheel 构建成功 |
| python tools/verify_wheel.py | 源码目录外干净安装、descriptor 和包内 manifest 验证通过 |

本地原始报告在 artifacts/checks/ 和 artifacts/*-junit.xml，制品在 dist/；不把这些临时结果当作远端 CI 或 NPU 报告。新增的是一项代码围栏测试及已有链接校验强化，不是两类档案的运行合同测试。

## 没有执行或没有宣称

- 未连接服务器、运行知识库脚本、改系统或同步服务器 checkout。
- 未执行 NPU 推理、APC/图功能、原生事件、Router 性能或收益实验。
- 未实现 BackendCapability/ExecutionProfile、阶段估计器、生产 Router 或 observer。
- 未把文档兼容目标、知识库公式或已有论文结果当成本项目实测。

后续先按 [当前计划](../docs/planning/前缀路由MOD_整体实现计划与工程验收.md) 完成 P0/P1 与 N0；具体方法和性能承诺继续由目标平台证据决定。
