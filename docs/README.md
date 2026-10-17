# 文档导航与权威顺序

2026-10-14 最新实机状态：新分配 32068 的 HUST ced6857/b47795c 已完成真实 HTTP→完整 family→核心逻辑终态→原账本的可选有限闭环。4 个 family/8 个 child 全部关联，stop string/断连核心 abort、重放、账本重开和自有包装恢复通过；不是原生 Router 已联通，也不是在线 KV 目录或性能验收。默认关闭、固定守卫、MOD import_only 不变。当前限制和下一步以[实机接线说明](planning/cedb477-live-terminal.md)及[证据](../evidence/cedb477-live-terminal-2026-10-14.md)为准，下方按日期保留历史。

当前入口是本目录的活文档，planning 保存 2026-10-11 修订的详细规划。reference 保留建仓时原始快照，不将其中“尚未建仓”等历史状态当成当前事实。冲突通过 ADR 和实际验证记录明确，不静默改方向；规划不是已实现能力。

| 文档 | 用途 |
|---|---|
| [进度](progress.md) | 实际完成状态、证据和接下来可执行任务 |
| [下一阶段执行计划](planning/next-execution-plan.md) | HUST 验证后的 W1–W4、交付物、退出条件、用卡与审查 |
| [运行门槛](planning/runtime-event-gates.md) | G1–G4 准入及真实事件恢复的判定条件 |
| [架构](architecture.md) | 产品范围、八项职责、复用边界和链路 |
| [Ascend 设计约束](ascend-design.md) | NPU 知识应用、三道复用门槛、档案与 N0–N6 验收 |
| [接口契约](contracts.md) | component-v0.1 可执行参考、所有权、代次及未冻结部分 |
| [组件联调与三人工作包](planning/component-work-packages.md) | 首个闭环、独立任务、宿主准入、用卡与合并条件 |
| [KV 事件回归合同](kv-event-contract.md) | 固定 HUST framing/replay、快照边界及真实回归子集 |
| [MOD 合规](mod-compliance.md) | 模板/Manager 版本、发现/启用边界、发布门槛 |
| [测试](testing.md) | CPU、模拟、硬件及制品测试的真实覆盖 |
| [服务器操作](operations.md) | 持久化、环境隔离、独占测试、恢复和安全 |
| [兼容矩阵](compatibility.md) | 哪些只是候选，哪些已验证 |
| [决策记录](decisions/README.md) | 技术选择与未决项 |
| [证据规范](../evidence/README.md) | 结果类型、原始记录和发布声明 |
| [基准说明](../benchmarks/README.md) | 负载矩阵、指标与消融 |
| [配置说明](../configs/README.md) | 静态示例及当前校验范围 |
| [原始资料](reference/README.md) | 详细规划与调研快照 |

当前详细基线：[总体架构](planning/前缀路由综合套件_总体架构与组件规划.md)、[复用评估](planning/组件复用与自研边界评估_2026-10-10.md)、[实施与验收](planning/前缀路由MOD_整体实现计划与工程验收.md)。修订理由见 [Ascend 优先 ADR](decisions/2026-10-11-ascend-first.md)。仓库版本保留正文，仅将工作区专用链接换为仓库内对应资料。
