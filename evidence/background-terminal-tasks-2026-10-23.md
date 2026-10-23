# 后台终态任务监督与双副本资格

日期：2026-10-23。续接[上一轮有限 step 接线](hust-terminal-driver-npu-2026-10-22.md)，本轮独立验证后台 run，不继承旧路径的故障或性能成绩。

## 实现与审查

同一 TerminalDriver 新增拥有式 TerminalTask，分别监督每个来源；采用有界 watch 快照，不增加账本、历史队列或重复消费者。首次完整确认才就绪，重试撤销就绪依据，停止立即撤销就绪；进度中的零负债或 idle 不替代最终 pending 检查。全部来源先发停止，再逐个 join；停止与取消 join 均不 abort 在途提交/确认。详见[ADR](../docs/decisions/2026-10-23-terminal-task-supervision.md)。

自审修复：先成功后发生 RPC 失联时，必须清除旧的就绪成功依据，防止下一次 Working 重用旧时间提前恢复。新增反例保留此保护。HUST 运输只在本地批次持久提交后累计七种固定核心状态，ACK/retire 重试不重复记数；饱和诊断计数不参与结算或恢复。新旧实验模式共用同一来源构造器和生成后取消解析，未复制账本、运输或 SSE 代理。

本地 Linux x86_64 helper 95 项、严格 Clippy 与格式通过；跨进程真实 FrontendEnds HTTP 场景由八项扩展为有限 step/后台 run 各八项，共 16 项，Core utility 仍是替身。覆盖正常、并发 ACK 新记录、真实断开回复、短暂不可用、observe_only、错误身份与未知请求。新增监督回归覆盖启动、零负债待确认、停止、句柄丢失、取消 join、panic、成功依据过期与重试失效。Windows CPU 606 passed / 7 skipped / 3 deselected、integration 3 passed，Ruff 通过。

## 现场与对照

用户核验新分配的 ED25519 指纹后连接，只操作授权项目目录。aarch64、8 CPU 时间配额、64GiB 内存，两张 910B2 本次物理映射为 3/5；公共 Qwen2.5-14B-Instruct 只读。固定 Core ced6857 / Ascend b47795c、FP16/eager、两个 TP1，服务环境、CANN 和全局依赖未改。

先对上一轮实际二进制 `37e255f35c6dc203881e0357988c7b7a2b762868f1be3a432c0fed088c38af05` 做同机复验：A/A/B、cached_tokens 0/384/384、核心 FINISHED_ABORTED、4 family、最终 active=0，两个后端自然退出，无强制清理。原始报告 `artifacts/c23-server32049-baseline/report.json`，本地忽略副本 `artifacts/c23-baseline-report.json`。这是功能回归对照，不是延迟开销 A/B。

实现提交 `03ed0d2` 后，自审修正在 `75da63d`。后者六项 GitHub CI 全部成功。服务器已在固定宿主完成最终 aarch64 helper 95 项/严格 Clippy、HTTP 16 场景、完整宿主 example 39 项与正常 release 二进制构建；未改变 Cargo.toml/lock 和宿主依赖。原宿主 deprecated/dead-code 告警保留，不宣称整个宿主严格 Clippy 全清。

最终 NPU 输入二进制 SHA256 `5366e46ed7b6c6def07876951ac3a3f6b625d5dda0fdcaa6d63cbd6bcba10b5e`。共享来源摘要：

| 文件 | SHA256 |
|---|---|
| terminal_driver.rs | `71f459ccfd95297d150e90f492eec182fdbfa76c0b9ead5d4a95d35961b72c3b` |
| hust_terminal_transport.rs | `e3ef8186d77b9c9e9bb4e64e8298db4bedc2d82cec358c8993eb48fbe6213604` |
| hust_runtime_probe.rs | `459b638e756881ca2a6f70ba0bac2710d6e95294b7efd5640a91bd65c8a6a7ef` |

构建及 CPU 原始结果在 `artifacts/c23-server32049-reviewed/`。中间制品 `d2e78c8...` 不用于本轮 NPU 验收。

## 最终双卡结果

使用上述最终二进制，运行 `tools.hust_runtime_chain --execute --model /data/shared_models/Qwen2.5-14B-Instruct --binary <上述二进制> --output artifacts/c23-server32049-background --pycache-root artifacts/g2-recovery-20261011/new-server/pyc --durable-store --managed-terminal-retirement --use-terminal-driver --background-terminal-driver`。沿用项目隔离服务 Python、已审计缓存与启动工具，未改引擎。

结果通过：实际终态来源水位对应 A/A/B，cached_tokens 0/384/384；关闭控制准入后新请求返回 503，生成后取消取得核心 FINISHED_ABORTED=1，另外三个正常请求为 FINISHED_LENGTH_CAPPED。4 family 完成持久提交、ACK、retire，最终 active=0；两个后台任务均返回 Requested 且没有 pending confirmation。已结清账本重开仍 paused，未做带旧债强退接管。两个后端 parent_returncode=0、forced_group_cleanup=false。

报告中 background_terminal_driver_verified/startup_confirmation_verified/admission_drain_rejected/background_tasks_joined 均为 true；durable_recovery_verified/production_lifecycle_verified/performance_verified 均为 false。retire_response_losses=0，retire_response_loss_recovered=true 只是符合零注入模式，不能解读成实机丢回复通过。核心状态诊断现在是后台运行期间已提交批次的累计，不是只保留取消那一批。

原始报告 `artifacts/c23-server32049-background/report.json`，本地忽略副本 `artifacts/c23-background-report.json`；SHA256 `0f9586d5f5209f2415a5ef54a32fa0b093e1aa3f8a9e1a9b0a8512b1608e8fe0`。两次有限实机运行都真实使用两张 NPU；完成后不保留空载保活负载。

## 仍未覆盖

`--background-terminal-driver` 是有限请求脚本中持续运行的真实后台任务；还没有交付公开常驻服务入口、整体 readiness HTTP 端点、SIGTERM/慢客户端工作计数与期限排空、生产启动恢复、Manager 激活或目录迁移。MOD 保持 import_only。CPU 回复丢失场景不能替代本轮 NPU 新路径的故障注入；无新增崩溃、churn、图模式或容量结论。平台原 profiling 依赖审计缺口仍未解决，不能写成整环境依赖通过。

本轮自审和自动回归不替代独立人工评审，保持草稿 PR，不自动合并。按用户最新要求暂停人员分工讨论，继续完成服务交付及同底座开销验证。
