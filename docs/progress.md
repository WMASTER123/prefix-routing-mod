# 当前进展与执行顺序

更新：2026-10-22。本页是现行完成情况入口；[接手指南](项目接手指南_2026-10-17.md)集中记录框架、端口、三人所有权和未决边界。[上一阶段](progress-native-owner-history-20261016.md)和 [更早历史](progress-history.md)保留原证据边界。组件数与测试数不用于计算“完成百分比”。

## 本阶段已完成

1. **原生传输回归**：Linux x86_64 和 aarch64 的共享原索引/owner/真实 IPC/身份控制各 90 项 release 测试通过；固定完整 Router example test 36 项通过，原 mock HTTP 组合回归通过。重放发送纳入总期限，真实 socket 测试已加入 CI。
2. **真实公开请求链有限闭环**：原 HTTP 入口、真实 Qwen M1、Rust M7→同进程原 M2、基础 M4、M3 准入、原 M5 转发与真实 HUST 终态已同时联通。A 冷/A 热/B 热实际目标 A/A/B，后端 cached_tokens 为 0/384/384；真实流式取消取得核心 ABORTED 后才结清，重复终态不重复释放，最终 active=0。详见 [公开链证据](../evidence/native-public-chain-2026-10-16.md)。
3. **新分配两卡复验与稳定性**：两张 910B2、Qwen2.5-14B TP1/FP16/eager，原目录隔离/断连复验通过；20 分钟后端稳定性完成 177/171 请求、12272/11856 生成 token，正常退出，无强制清理或残留设备进程。该稳定性未启用终态观测，不能替代其长期留存验收，见 [证据](../evidence/npu-stability-2026-10-16.md)。
4. **M3 持久提交候选**：新增 Rust 单写提交包装与 SQLite WAL/FULL 候选，发送许可晚于持久提交，终态/M3/cursor 同一事务，恢复默认暂停并用 owner/generation CAS 围栏。审查补齐异步取消失败关闭、完整终态 source binding、歧义快照拒绝和整批预校验；x86_64/aarch64 release 各 48 项通过，本地 clippy 通过。aarch64 完整快照 commit 在 0/64/512 active 下 P50 为 0.673/0.809/2.320 ms，因此不选作默认生产热路径，见[证据](../evidence/durable-runtime-candidate-2026-10-16.md)。
5. **Core/Frontend 终态窗口回收**：按固定 HUST 源码与 hash 守卫确认 internal ID 和一次性清理边界，Core 记录及重复保护只保留到唯一 Frontend ACK；CPU 回归覆盖 ACK 前重复、ACK 后复用和外来流量。Frontend family 退役采用专属受管池，现已装入双副本 Rust 持久公开链；不把前缀当凭据，见[ADR](decisions/2026-10-16-terminal-source-retention.md)。
6. **增量存储与交接**：同一 SQLite WAL/FULL 适配器新增变化行布局，整批终态先验证再修改；helper 已覆盖真实子进程未提交退出与已提交 active 保债。最小 feature 只新增三个包、未升级原宿主依赖。两轮相同持久性对照中，512 active 行布局 P50 2.577–2.598ms，对照快照 4.239–4.665ms；0 active 尾延迟不优，不能外推服务收益。真实 commit→ACK 资格见[既有证据](../evidence/durable-public-chain-2026-10-17.md)。
7. **受信终态退役与累计资源**：首版部署边界为受管副本池仅接受 Router 业务入口；默认 `observe_only`，只有显式 `exclusive_router_v1` 才允许在 M3 持久提交、ACK 后按同一水位退役完整 family。双 NPU 已注入两个真正“服务端提交成功但 Router 未收到回复”的故障，重试均未重复释放；520 个 churn family/1040 child 后 Frontend/Core 历史计数均为 0、Router 墓碑≤16、store bytes 保持 321104。详见[恢复与回收资格](../evidence/recovery-retirement-qualification-2026-10-18.md)。
8. **Router 强退与同实例恢复**：真实请求在持久 sent、终态尚未消费时强制退出；新 Router 默认暂停，保留 1 条 uncertain 债务，拒绝暂停期请求；核对相同终态来源身份并确认旧派发者停止后 CAS takeover，旧债和恢复后新请求均结清。由于原生 replay END 没有权威当前水位，重启后的 M2 明确保持 unknown，不把可能不完整的暖回放当命中。已回收水位以下 cursor 被拒绝；真实重启一个 HUST 后端后，旧持久绑定因 instance epoch 变化而失败关闭。基于该证据只冻结 `recovery-retirement-v1` 语义合同，整个 MOD 的公开 ABI/Manager/性能仍未冻结。

9. **控制新鲜度到准入的有限闭环**：新增默认关闭、单调期限与来源身份绑定的 WorkerGate，M3 快照/预留/发送前均校验；失联/过期关对应准入但保留旧债，同源新验证可恢复，冲突来源隔离。双 NPU 真实身份 HTTP 503、停止刷新后的真实期限到期、注入 observer 冲突、另一副本继续服务、保债和 drain 拒绝均通过，共结清 9 family。aarch64 helper 68、索引/IPC/控制 93、完整宿主 39 项通过；这是有限链，不是常驻 service 或 Manager 信号排空。细节及自审修复见[本轮证据](../evidence/control-admission-2026-10-22.md)。

10. **终态持续驱动 CPU 候选**：同一 DurableRuntime 外新增每来源唯一的 `terminal_driver.rs`，持续轮询不依赖 HTTP 触发；active=0 也补 ACK/retire，同水位失联重试不重复结算，observe_only 不回收。停止等操作完成，任务丢失/取消失败关闭。aarch64 helper 共 80 项（新增 12 项）及 Clippy 通过；运输目前是 CPU 替身，尚未替代有限 HUST reconcile，也未完成信号/Manager。新服务器两卡复验的是第 9 项固定二进制，不是新驱动，见[范围与记录](../evidence/terminal-driver-2026-10-22.md)。

11. **固定 HUST HTTP 运输的 CPU 接线**：新增唯一 `hust_terminal_transport.rs`，调用现有可选终态端点，绑定完整 cursor/PID/mode，限时限字节、完整批与 family 校验；ACK 允许携带更新记录，后续 poll 才提交。Rust helper 86 项通过；真实 socket 和 Rust↔现有 FrontendEnds 的跨进程 HTTP 八场景通过，Core utility 是替身。有限公开链源码新增显式 `--use-terminal-driver` 路径，与旧 reconcile 互斥，完整宿主因本机依赖缓存不足尚未构建通过，需补构建及独立实机资格；旧两卡成绩不能继承。Windows CPU 596/integration 3、包构建/安装通过。旧服务器连接已关闭，尚未运行本轮 NPU。测试、审查和未决项见[运输记录](../evidence/hust-terminal-transport-2026-10-22.md)。

固定基线为 HUST Core ced6857 / Ascend b47795c，Router91b65ee；未更换引擎、CANN 或全局 Python。服务使用项目隔离环境及窄 PYTHONPATH，本 MOD wheel 构建、干净安装和导入验证通过。当前平台镜像的服务 venv 全量 `pip check` 仍报告若干预装工具缺失可选依赖，不能再写成整环境依赖审计通过；这些项未由本 MOD 引入，需在冻结运行镜像时单列处理。

## 组件状态

| 组件 | 已实际完成 | 冻结/交付前剩余 |
|---|---|---|
| M1 | 真实本地 Qwen tokenizer、完整块摘要，已装入公开字符串 completion 链 | 输入/输出预算和一次分词开销；chat/template、数组/token-id 不在当前支持范围 |
| M2 | 复用原索引/Directory/Pool；真实 Rust 订阅写入同进程目录，连续性/父链/总配额/失信/实例围栏；Router 重启后安全降级为 unknown 已通过 | 大工作集/并发成本；当前不支持暖目录恢复，需权威快照水位的新 profile 后另行验收 |
| M3 | 条件准入、发出后保债、真实终态/增量存储/commit→ACK→retire；回复丢失、累计回收、强退恢复与控制失联/过期/同源恢复有限资格通过；持续驱动及 HUST HTTP 运输 CPU 联调通过 | 新宿主构建/实机资格、生产常驻装配/readiness/排空和 Manager；跨主机所有权不在首版范围 |
| M4 | 基础可信正命中优先/最低在途回退，真实目标和缓存结果符合预期 | 配对开销与强基线；优化策略后置 |
| M5 | 复用原 Router HTTP/SSE，公开入口、真实转发及生成后取消通过 | 生产启停/排空、并发配置竞争/故障和可发布二进制 |
| M6 | CPU/源码/原生 CI、可重复装配、手动 NPU 公开链/稳定性及原始报告 | 同底座开销、持续服务、强基线和多阈值容量曲线 |
| M7/MOD | 固定版本映射、默认关闭的真实入口/终态适配、独立身份接口和原生 IPC | 生命周期、长期观测回收、正式 Manager 激活与升级逐项认证 |
| M8 | 边界和软计划契约 | 尚未实现；基础性能证据之前不加入 |

当前冻结的只有 `recovery-retirement-v1` 语义合同；生产目录、公开配置/错误码、Manager 生命周期和整个 MOD ABI 尚未冻结，MOD 仍 `import_only`。没有容量提升或优于强基线的结论；原生流无 publisher epoch/完整共同水位快照，不能把本地连接代次称为后端 epoch。

阶段历史：最早的单卡 `live_terminal_chain` 是 Python 持久参考账本，早期双卡 `hust_runtime_probe` 只做 Rust commit→ACK；随后才接入显式受管 retire。不能把这些旧结果当作响应丢失或崩溃恢复证据。本页第 7、8 项及[最新资格记录](../evidence/recovery-retirement-qualification-2026-10-18.md)已补齐对应真实故障。

## 接下来集中完成什么

1. 按[控制准入 ADR](decisions/2026-10-22-control-admission.md)保留已通过的控制门资格；接着推进[生产接线与开销工作包](planning/production-runtime-and-overhead.md)中的常驻终态循环、信号排空、Manager 监管、唯一源码迁移与制品。不得改动保债、来源绑定或 commit→ACK→retire 顺序。
2. 开展有界直连/原 Router/同底座仅加接线开销对照，明确启用的观测与持久性；随后做双副本强基线、多阈值容量曲线和输入/输出长度分组。
3. 正式按 A(M7/M2)、B(M1/M4/M5)、C(M3/M6/交付)分工。共享语义已冻结，生产目录迁移、公开配置/错误码、Manager 和测试替身仍由集成负责人统一收敛并交叉审查。见[工作包](planning/component-work-packages.md)和[接手指南](项目接手指南_2026-10-17.md)。

用户暂无固定 SLO：容量评测仍按预冻结的多阈值 TTFT/生成停顿与输入/输出预算分组，保留失败分母；chunk 间隔不冒称逐 token 间隔。尚未开展正式容量实验。

## 测试与审查

2026-10-18 代码 `49b3aea` 的成绩：本地 CPU 588 通过/7 跳过/3 deselected、integration 3 通过，Ruff 检查与格式通过；aarch64 固定完整宿主 example 38 项、独立 checks 58 项通过，限定排除原 Router 四项既存 lint 和组合 dead-code/type-complexity 后 `clippy -D warnings` 通过。删除的 1 项是无法由原生 END 证明完整的暖 M2 恢复测试。2026-10-22 已重新查询：该提交的 6 项 GitHub 检查全部成功，仓库保留的 6 次历史失败都有后续成功修复，不能再将此提交写为 CI 待完成。新增控制门的成绩单独记录，不继承旧 CI。当前服务 venv 的全量 `pip check` 仍有平台预装工具依赖缺口，不能计为本轮通过。

PR23 已合并到阶段分支 `codex/online-kv-directory`；后续为[草稿 PR24](https://github.com/WMASTER123/prefix-routing-mod/pull/24)，控制与生命周期候选在其上叠加[草稿 PR25](https://github.com/WMASTER123/prefix-routing-mod/pull/25)。`7110d16`（含 a53cba0 持续驱动）的 6 项 GitHub 检查已逐项成功；本轮 HTTP 运输实现提交 `4b867d9` 的 6 项检查也已独立成功，包括新增跨进程 HTTP 场景，不能将该 CI 当完整宿主/NPU 验收。`main` 尚不包含全部研发进展。`3264050`/`artifacts/t9` 是较早持久链输入；32066 旧证据和 32081 新 Rust 双副本退役结果、二进制/归档 hash 见[退役证据](../evidence/terminal-retirement-2026-10-17.md)。本轮没有自动合并或正式发布。

已完成自审、自动回归和上述实机子集；独立人工审查/合并未完成，不自动合并。Linux CPU 可推进持久化/协议；再次真实持续服务与恢复回归需要两个 TP1 NPU。测试完成清理自有进程，不运行空载保活负载。
