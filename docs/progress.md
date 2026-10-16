# 当前进展与执行顺序

更新：2026-10-16。本页是当前完成情况的唯一入口；[历史进度](progress-history.md)按各次提交保留，旧段落的“当前/下一步”不再作为现行任务。

本轮增量：Linux x86_64 的真实私有 IPC 已覆盖 replay/live、重复/冲突、缺口、超限、断连、停止及实例替换；共享原索引/owner/传输和独立 HUST 身份控制候选共 90 项 release 测试通过。重放发送也纳入总期限；实际 socket 测试加入 CI，不再只检查格式。新分配两张 910B2 的 `r5` 原目录实机复验通过，两端 cached_tokens 均为 0→384；两卡有限稳定性负载另行记录。真实 Rust subscriber 与公开请求链仍待组合，不把 Python 数据面成绩归给它。工作包的过期状态已迁入 [历史页](planning/component-work-packages-history.md)。

## 已通过与交付边界

- 固定 HUST Core ced6857 / Ascend b47795c，Qwen2.5-14B-Instruct、FP16/eager/TP1/DP1。32054新分配的物理0/7均为910B2，各65536MiB；8核CPU配额、64GiB内存。依赖 `pip check` 通过，没有更换引擎或全局设备栈。
- 上阶段功能cc2478b的真实 Router→HUST→同一账本有限闭环，已在新分配两张卡顺序复验通过。最终文档050b7bd已同步，上一阶段新wheel的aarch64干净安装补过。050b7bd的CPU/源码与helper CI五个检查全部success；独立人工审查/合并未完成。
- M7原生事件→常驻原M2索引新增在线接线；原Directory/Pool和91b65ee原索引继续复用。两张卡的首轮在线目录+真实Router终态组合均通过：3个完整前缀块匹配，订阅关闭后unknown。后续审查修改和双副本结果以[本阶段证据](../evidence/online-kv-directory-2026-10-14.md)为准，不能自动继承首轮硬件资格。
- 最终功能24d78d3的双TP1实机`r2`通过：同一原索引owner、单边驻留隔离、两端实际cached_tokens0→384、一侧订阅退出而另一侧继续服务。两卡均已用于真实模型实验且自有进程清理，证据本地/服务器SHA一致。Windows CPU562、aarch64566，integration各3，原生62/进程5组、新wheel两端干净安装通过。b35ffef的5个CI检查全部success；[草稿PR22](https://github.com/WMASTER123/prefix-routing-mod/pull/22)待独立人工审查，后续纯记录head另查。
- 首版请求链0f05ae8已在新分配32064重新通过双TP1在线目录资格；469d06c又在32049的物理5/6两张910B2复验同一结论：固定HUST与Qwen14B的实际cached_tokens分别0→384，隔离/断连和正常清理通过。固定Router宿主typed链fixture探针已通过真实HTTP转发、准入和终态回调；M1候选还在固定Router/HUST之间通过449-token及3个完整块摘要的一致性对照。三项仍分别是数据面、组合接口与特征身份的证据，不相加冒充真实公开Router全链或性能收益。
- 2026-10-16 的候选增量已将同进程M3 owner接到原Router M2索引Pool：固定Router typed公开入口、原索引命中/可信miss、基础选点、准入、真实loopback HTTP转发及可靠终态在同一进程闭环通过。断连撤销信任，同URL新instance必须重新恢复，旧身份不能替换/注销新分区，全池逻辑配额可回收；索引及适配66项和owner 37项CPU回归通过。M1及归一化KV事件在该组合探针中仍是fixture，真实HUST订阅与公开Qwen链尚未合并，因此生产接口仍未冻结，详见[证据](../evidence/runtime-cache-owner-composition-2026-10-16.md)。
- 新分配服务器上的双 TP1 原生目录复验已通过：固定 HUST/Qwen14B 两副本实际缓存复用均为 `0→384` tokens，真实 M7 store 进入共享原索引的独立分区，单边驻留和断连降级均正确，结束后无残留设备进程。首轮 404 被确认是项目隔离环境中同版本旧 wheel 缺 terminal entry points，不是引擎冲突；已改用安装当前审查 wheel 的 rebuild 隔离环境，并新增模型启动前的入口完整性拒绝检查。详见[复验证据](../evidence/two-replica-native-directory-2026-10-16.md)。
- 固定 HUST wire 的 Rust 有界解码与连续性候选已接入原索引 release 回归：精确 `msgspec` fixture、错误帧/字段/身份/预算、重复/缺口/溢出反例共随 74 项测试通过；连续性只保留有界 SHA256 指纹，不保留完整 batch。同一源码在固定 Router 宿主构建并执行最小解码，原 M2/M3/typed HTTP 组合探针无退化。尚未创建生产 ZMQ owner，详见[证据](../evidence/native-kv-wire-rust-2026-10-16.md)。
- Rust 单分区 owner 已把受控冷起点、replay/live 连续性、控制确认和统一失信接到同一个 `InProcessCache`；较早提交随原索引 78 项 release CPU 回归通过，旧 owner 不能撤销同 URL 新实例。显式 IPC 传输初版通过固定宿主 release check；完整 example test 收尾时 SSH 中断，具体原因未确认。实际 socket 行为现由本轮 Linux 回归补齐；最终 aarch64 宿主构建和真实 HUST Rust 接线仍须独立通过，详见[证据](../evidence/native-kv-owner-zmq-2026-10-16.md)。

本阶段只支持受控空起点的已发布事件观测。原生流没有publisher epoch和完整共同水位token快照；本地连接绑定不冒充后端epoch。模型请求历史、计划和预留不能产生驻留事实。

## 各组件的当前状态

| 组件 | 已有实现/验证 | 首版剩余工作 |
|---|---|---|
| M1特征 | 有界completion FeaturePort候选与最终30项Rust检查通过；固定Router/HUST对公共Qwen14B同一输入得到相同449 tokens和3个完整M2块摘要，资产hash与有界身份已验证 | 把已验适配装入公开请求链并测一次分词开销；chat/template、数组/token-id prompt尚未支持，不得静默降级 |
| M2目录 | 原索引+Directory/Pool、连续性/父链/限额/租约；在线订阅实机子集；同进程CachePort和单分区owner已与M3/HTTP分别组合通过，支持精确实例替换、错误/停止失信及全池配额回收 | 完成ZMQ socket故障回归并接真实M7事件；验证长期增长/并发成本，已热/超窗恢复另评估 |
| M3状态账本 | 预留、在线传输、真实核心终态、幂等和有限持久恢复；同进程owner已实现配置/实例围栏、发出后保债、可靠终态一次释放、有限墓碑与显式checkpoint/新进程恢复 | 选择checkpoint持久介质和提交时机，接真实入口终态，验证Router崩溃后的负债恢复 |
| M4选点 | 固定Router内候选组合层已实现正命中优先及无正命中的最低在途回退 | 接真实M1/M2/M3双副本快照并测策略/查询开销；优化方法后置 |
| M5执行 | 原Router完整构建、真实普通/流式/取消链路通过；候选组合层装入固定源码后aarch64完整check通过；公开typed链fixture探针已通过真实HTTP转发 | 公开HTTP入口安装真实端口，排空/关闭/背压及默认关闭启动验收 |
| M6评测 | CPU/integration/源码/制品/手动NPU入口与原始证据 | 两副本强基线、Host配对开销、到达负载/多阈值容量 |
| M7适配/MOD | 固定版本能力映射、真实入口与终态可选包装、原生KV解码、单分区信任owner及同机IPC传输候选 | socket行为/真实HUST联调、完整启动/停止/恢复和实际Manager激活；新版本逐项认证 |
| M8后台亲和 | 责任与软计划契约 | 尚未实现，基础性能证据前不加入 |

MOD保持import_only，生产ABI尚未冻结，仍无端到端容量提升结论。原生事件的逻辑驻留不等于即时设备强一致状态或省去同量计算；后端cached_tokens和性能需独立对照。

## 近期顺序

1. 在线目录数据面资格和 Linux x86_64 实际 IPC 回归已通过；独立人工审查仍待完成。下一步复验最终 aarch64 固定宿主构建并接固定 HUST 身份控制与真实事件，CPU fixture 不能替代这项门槛。
2. 最小公开HTTP请求链：候选本地组合层、原M2索引和M3 owner已在固定宿主同进程闭环通过。用通过 socket/真实 HUST 验证的 Rust subscriber 替换组合探针的fixture事件后，再安装真实Qwen M1与HUST终态；缓存无正命中仍回基础负载策略，M2失效降级unknown，M3失效拒绝发送。Python/stdin资格接线不冻结为生产热路径。
3. 为M3 checkpoint选择可测量的持久介质/提交边界，补公开HTTP正常、取消、断连、配置竞争、目录缺口及进程重启测试；通过后冻结代码目录、配置、错误语义、线程模型与替身，才能正式分工。当前显式checkpoint语义已形成，但生产持久化与启动恢复尚未闭环。
4. 完成上述真实接线的首个正确性子集后立即做同底座强基线与配对开销：直连、原Router基础策略、同宿主仅加接线，再比较策略；低复用、热点变化和混合长度必测。

用户确认暂无固定业务SLO。先统一校准并预冻结多组TTFT/生成停顿阈值和输入/预设输出预算分组，报告达标容量、完成/拒绝/超时及分组P95/P99；失败保留在到达分母。chunk间隔不能冒称逐token间隔。具体数值尚未选择，性能测试尚未完成。

## 团队与审查

可独立准备：版本/协议fixture、M1一致性、评测负载/统计和测试工具。固定Router内三个本地端口与原M2适配已形成候选并完成CPU组合，但真实M7/M1/终态同时装配、M3持久恢复及公开HTTP故障测试未过，暂不把核心范围宣布为冻结。共享契约由集成负责人维护，生产分工以[工作包](planning/component-work-packages.md)的冻结清单为门槛。

每项交付需区分自审、自动CI、实机子集及独立人工审查。草稿PR不自动合并；不把测试数量当组件完成比例。当前服务器只用于本项目目录；真实模型实验结束后清理自有进程，不运行保活负载。

2026-10-16 早先分配入口 `32063` 失联的记录保留在历史页；后续新分配资源已经完成上述双 TP1 原生 M7 复验，不能再把“未新增 NPU 成绩”当作当前状态。
