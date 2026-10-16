# 测试与 CI

2026-10-16 后续：`validate-index` 已加入 `--features hust-control`，直接编译共享传输/源控制并运行真实 Linux 私有 IPC 与 loopback HTTP 反例，当前本地 90 项 release 通过。此项替换“ZMQ 仅格式检查”的覆盖缺口；完整宿主和真实 HUST 仍独立验收。`tools/npu_stability_probe.py` 提供 1..1800 秒、两 TP1 的有限后端稳定性负载，真实生成 16/64/128 tokens，默认不安装终态 hook，只输出有界计数；不作为 Router 容量或终态长期回收证明。

2026-10-16 较早回归直接编译 `native_kv_wire.rs` / `native_kv_owner.rs`，固定 HUST msgspec fixture 和 78 项 release 测试由 CI 通过。该组没有 socket；后来 SSH 中断时完整宿主测试未取得结果，原因未确认。本轮实际 IPC 增量见上段，真实 HUST 订阅仍独立验收，见[单owner证据](../evidence/native-kv-owner-zmq-2026-10-16.md)。

现行完成范围以[progress](progress.md)和[在线目录证据](../evidence/online-kv-directory-2026-10-14.md)为准，下方日期段落保留历史。原索引CI新增常驻owner双分区/缺口/租约/身份/全池预算进程检查；NPU手动工具为`tools.hust_native_probe --index-binary ... --terminal-observer`和`tools.two_replica_kv_probe --execute ...`。前者可组合既有Router终态，后者验证两个TP1真实副本/共享索引/隔离；均不代替公开HTTP组件装配、容量或完整恢复。

2026-10-14 用户确认：暂无固定业务SLO，双副本评测先报告多阈值容量曲线与输入/输出长度分组。阈值/分组在基础校准后、策略对比前统一冻结；同时报告各组P95/P99、完成/拒绝/超时率，不事后挑阈值或删失败样本。当前未新增性能结果，具体口径见[当前计划](planning/router-hust-terminal-chain.md)。

2026-10-14 后续：原typed Router库的异步准入/自有载体/转发/生命周期已和真实HUST终态组合到同一实验账本，2 family/3 child及丢许可保债通过。原三文件宿主patch与HUST包装不变；新接收器映射显式实例，新增完整终态批次校验。当前仍不是生产HTTP入口、在线KV或性能验收，component-v0.1/生产ABI和MOD import_only不变。范围与下一步见[组合计划](planning/router-hust-terminal-chain.md)和[证据](../evidence/router-hust-terminal-chain-2026-10-14.md)，下面同日较早记录仅对应当时阶段。

2026-10-14 最新实机状态：新分配 32068 的 HUST ced6857/b47795c 已完成真实 HTTP→完整 family→核心逻辑终态→原账本的可选有限闭环。4 个 family/8 个 child 全部关联，stop string/断连核心 abort、重放、账本重开和自有包装恢复通过；不是原生 Router 已联通，也不是在线 KV 目录或性能验收。默认关闭、固定守卫、MOD import_only 不变。当前限制和下一步以[实机接线说明](planning/cedb477-live-terminal.md)及[证据](../evidence/cedb477-live-terminal-2026-10-14.md)为准，下方按日期保留历史。

2026-10-14：新增`hust-source-methods`独立job（Python3.11/3.12），显式下载HUST ced6857的十个固定SHA文件，运行新版终态审计和入口→原账本组合；另执行缺seal失败、意外异常传播等3项测试。`tools.fetch_hust_audit_sources`只写全新项目artifacts目录；TLS/网络或SHA失败会使job失败。普通CPU测试的3项源码执行在未设HUST_AUDIT_SOURCE_DIR时skip，专用job必须显式设置有效目录。没有HUST安装或NPU/真实HTTP资格；复现命令和边界见[现场计划](planning/cedb477-terminal-boundaries.md)。下方“源码未纳入CI”只描述此前日期的状态。

2026-10-13 入口/恢复新增：自动 CPU 测试覆盖总组数/提前结束、普通额度拒绝后的继续对账、旧许可 fencing、时钟不复用、交接提交前/后独立进程退出；原 helper CI 直接编译 admission_bridge.rs，覆盖精确回执、丢回执、取消等待及八个 RPC 槽位上限。固定 HUST 入口方法审计手动执行 `python -m tools.audit_http_entry --source-directory <原七文件目录> --entry-directory <三个 serving 文件目录> --output <新报告>`，须通过全部 hash 守卫，renderer/IPC/core 仍是替身。完整 Router 新增 `--online --dynamic` 验证零预填预留、提交早于后端到达和第二请求额度拒绝；旧 `--online` 及默认关闭路径保留。不是 NPU/真实 hook 或性能验收。

2026-10-13 新增真实 socket/独立接收进程回归和原生控制线程测试。原 helper CI 直接包含 online_bridge.rs，覆盖断连后原载荷重发、错误/超大确认、期限、留存上限和关闭派发；Python CPU CI 包含进程终止与重建。完整 Router 通过 tools.router_extension_probe 的显式 --online 手动验收，仍用合成 HTTP 后端与预先持久化的有限 fixture，不代表真实 HUST 终态或动态准入通过。结果见[证据](../evidence/online-transport-2026-10-13.md)。

2026-10-12 新增独立子进程在 SQLite commit 前/后强制退出的回归、原生 family fanout 的方法审计，以及 wire 解码限额/必需字段测试。原生索引 CI 新增规范化 fixture→原索引的断言；fixture 来自固定 HUST codec 合成数据，CI 不启动 HUST。方法审计在目标 CPU 手动运行；msgspec 0.22.0 仅从既有隔离服务 venv 显式调用，不加入 Router 的运行依赖。逐条与有界批量提交都覆盖原子性/幂等/崩溃；没有生产 IPC、真实 NPU、持久化时钟交接或 GC 验收。

2026-10-12 新增 `test_backend_terminal_audit.py` 的工具/hash/证据保护测试，以及 `test_terminal_observer_prototype.py` 的进程内在线账本与故障测试，自动进入原 CPU CI。固定源码方法审计另手动执行 `python -m tools.audit_backend_terminal --source-directory <四个固定源码文件所在目录> --output <新的报告路径>`；四文件先完整 hash 校验，不导入引擎。真实源码未纳入仓库/CI，不能将工具单测说成 CI 执行了 HUST。consumer 重建测试保留参考账本，未覆盖 kill -9/持久化、真实网络或 NPU。

2026-10-12 审查收尾：新增 `Native HTTP extension helper checks`，锁定小型 checks crate 并直接编译宿主共用的 `prefix_extension.rs` / `lifecycle_bridge.rs`；包含空块让出/超时、慢消费者、响应限制、关闭恰一次及有限标准事件。该 CI 不编译完整 Router 或测试真实后端终态。完整 aarch64 example 人工测试另输出原生传输记录，由 `tools.lifecycle_trace_probe` 重放到现有账本 oracle；实验结束后的重放不算可靠在线投递。Windows 大响应 fixture 先消费请求体再关闭连接，避免 TCP reset 隐藏大小限制断言。

2026-10-12 原生增量：新增 `Native index adapter checks`，固定 Rust 1.99.0 和两个原索引源文件 hash，执行 Rust 格式、原生语义与短更替 smoke。它不构建完整 Router 或占用 NPU。完整 aarch64 Router 的 release 与 `tools.router_extension_probe` / `tools.router_stream_probe` 仍由项目私有构建环境手动运行；[本轮记录](../evidence/bounded-router-adapters-2026-10-12.md)单独区分这些成绩。Python CPU 的 probe 守卫测试不能替代真正执行 Rust。

当前真机工作遵循[四道顺序门槛](planning/runtime-event-gates.md)。只读 `tools/audit_runtime.py --output artifacts/runtime-audit-new.json` 检查实际调用解释器的包元数据和 `pip check`；未含必需核心包也失败。退出 0 仅表示 Python 依赖审计通过，不是 NPU/ABI/环境锁通过；详见[运行环境](runtime.md)。CPU 回归覆盖缺包、失败、超时、保留旧证据、来源 URL 脱敏和“依赖通过不冒充环境验收”。

在 uv sync --frozen --extra test 后运行：

```bash
uv run --frozen python tools/check.py --suite cpu
uv run --frozen python tools/check.py --suite integration
uv run --frozen python -m build
uv run --frozen python tools/verify_wheel.py
```

CPU 包含格式、静态检查、配置/CLI、真实 Manager 拒绝启用和未实现套件结果检查。integration 目前只测试合成 loopback 后端的健康、非流式响应、SSE 帧和 503；**尚未经过真实 Router，不能证明流式低延迟、取消传播或背压已正确**。

component-v0.1 增加类型/精度/完整块、新鲜度、服务/publisher 代次隔离、配置切换、原子准入、幂等/冲突通知、发送前撤销/发送后不确定/迟到对账和上限拒绝测试；六组语言可移植的行为样例保存在 `tests/fixtures/component-v0.1.json`。跨组件测试只是静态目录和有界账本 oracle 的进程内组合，不启动生产 Router，不替代真实转发取消或性能评测。生产绑定必须保留这些语义，具体线程模型/ABI 和长期 GC 另外验收。

verify_wheel 在源码目录之外新建临时 venv，安装无运行依赖的 wheel，加载 entry point 与包内 manifest、运行 doctor；Manager schema/启用拒绝由 CPU 测试负责。发布前还需完整安装链、目标宿主及真实启停验收。

干净 wheel 同时导入新合同并构造实例身份，确认契约被打包且不加载 torch/vllm；不把接口制品成功称为 MOD 可启用。

可选 publisher 的 CPU 测试另覆盖默认不导入引擎、版本/文件守卫、幂等、共同水位、发送/编码失败、元数据及重放 payload 上限、祖先拓扑和清空代次。干净 wheel 检查实际加载 general plugin 并调用默认关闭入口，确认 torch/vllm 未导入；不能由此证明 opt-in 的真机接线成功。

`tools/regress_kv_identity.py --publisher-hook` 在隔离 HUST 服务环境中做真实纯文本身份与恢复子集：已热快照、超窗快照、快照加增量、真实 clear、同地址重启、新旧实时 epoch。测试目录有界、仅暂存回归状态；不是生产索引。新代次须重建 SUB，完整报告保留失败和重试。压力/自然淘汰的 hook 重验、恢复竞态、资源成本和图模式仍是后续独立项目，不由这组串行请求替代。

## 统一报告

tools/check.py 生成 artifacts/checks/<suite>.json，CPU / integration 同时生成 JUnit。退出码 0=通过，1=执行失败，2=参数错误，3=未实现/未运行。当前 npu-smoke、npu-routing、performance、release 是保留入口，返回 not-run 和 3，绝不静默成功。

在目标实现阶段补齐 pass/fail/skipped/not-run/inconclusive 的完整结果 schema；必须有真实测试才允许该套件通过。

`tools/smoke_backend.py` 是显式 `--execute` 的局部诊断入口，仅 Linux、指定本地模型/可执行文件、单卡 TP=1 eager、loopback、独立进程组、有限超时；可采样原生 KV 事件。使用方式见[运维](operations.md)。它还不包含图/取消/恢复/Router，不能替代完整 `npu-smoke`，保留入口仍返回 not-run。CPU 测试覆盖启动参数、SSE 完整性、事件脱敏和关闭边界，不启动设备。探测不加速包安装、不更改 MOD 的 import_only 状态。

新增 tools/restore_runtime.py 的同机环境恢复入口、tools/verify_triton_overlay.py 的实际文件审计、tools/inspect_runtime.py 的显式导入/加载库清单。恢复与四卡检查见 [W1 证据](../evidence/w1-runtime-restore-2026-10-11.md)。tools/regress_kv_events.py 是有界真实生命周期子集，tools/audit_kv_recovery.py 为合成队列的真实源码方法审计；[G2 证据](../evidence/g2-native-events-subset-2026-10-11.md)分别记录。不得把子集 pass 或确定性源码观察汇总为完整 npu-smoke/恢复通过。

## CI 策略

恢复故障入口 tools/regress_kv_recovery.py 需在已审计的 HUST 服务 venv 中显式运行；CPU 连续性/故障测试沿用统一入口。与生命周期入口使用不同输出目录和 HTTP 端口，不覆盖旧报告。真实三批重放、缓冲重复帧、SUB 断连、窗口溢出、控制面超时和同地址重启分别报告；旧连接注入标记 synthetic。完整快照恢复、hash/group 身份、网络逐帧 epoch 和性能保持独立未验证标志。命令见[运维](operations.md)，边界见[ADR](decisions/2026-10-11-g2-recovery-regression.md)。

smoke 的可选 --pycache-root 覆盖显式私有缓存、越界拒绝和其他缓存隔离；真实逻辑卡 3 复测见[本轮证据](../evidence/g2-recovery-regression-2026-10-11.md)。该参数不改变完整 NPU 套件状态，环境准备、冷启动和共享 bytecode 启动分别留证。

GitHub Actions 在 push main、PR 和手动触发时运行 Python 3.11/3.12、锁文件依赖、同一测试入口和干净 wheel 检查，保存证据 14 天。仅 contents:read，不发布、不部署、不占 NPU。锁定第三方 action 的不可变提交是后续供应链加固项，当前使用主版本标签，依赖版本由 uv.lock 固定。

NPU 先手动执行，后接受控 runner。不得将未审查 PR 放到有服务器凭据的 runner。真机测试必须预约/设备锁，清理只针对本次创建的资源。

## 待补覆盖（不能省略）

P1/P2：真实 Router 协议透传、即时 SSE、慢客户端、取消、发送前/后失败、排空与无可用副本。P3：身份隔离、事件乱序/缺口/恢复、代次失效、账本幂等和元数据上限。P4/P5：强基线、负对照、独立消融与过渡期成本。P6/P7：持续运行、跨节点、目标架构、版本拒绝、关闭回退和第二成员复现。

## Ascend 增量验收（计划，尚未实现）

CPU 契约新增能力/档案 schema、单位/unknown、指纹失配/过期/范围外回退、语义与性能失效范围、block/rank 映射。mock 只能验证合同逻辑，不能证明 NPU 图或事件能力。

硬件阶段按 [N0–N6](ascend-design.md) 实施：npu-smoke 验证整栈与单副本目标组合；npu-routing 验证两个 TP=1 副本的 APC/图/事件与生命周期；performance 校准前缀实际节时、混合阶段和 Host 开销；release 验证目标 aarch64 制品、MOD 真实加载/停用与降级。先固定主配置再做风险驱动组合覆盖，未测试组合不进入支持矩阵。

异步微基准在必要边界正确同步，服务测试不逐请求同步设备。图与 KV 冷热分别控制；采样诊断不用于正式成绩，observer 开关单独对照。完整版本/模型/图/内存/Host 指纹和独占条件随报告保存。手动测试仍用统一入口，新增 NPU CI 前遵守设备预约、凭据和 PR 审查约束。
