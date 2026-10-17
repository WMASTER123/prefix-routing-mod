# 接口契约：component-v0.1 研发参考与未冻结部分

现行完成情况统一以[当前进度](progress.md)为准，近期接线见[在线目录计划](planning/online-kv-directory.md)。本页下方按日期追加的完成/未完成描述保留为历史；旧版publisher能力不外推到新版HUST，参考接口不等于生产ABI。新增双副本目录子集不表示公开HTTP全链路、长期恢复或容量验收通过。

2026-10-14 后续：原typed Router库的异步准入/自有载体/转发/生命周期已和真实HUST终态组合到同一实验账本，2 family/3 child及丢许可保债通过。原三文件宿主patch与HUST包装不变；新接收器映射显式实例，新增完整终态批次校验。当前仍不是生产HTTP入口、在线KV或性能验收，component-v0.1/生产ABI和MOD import_only不变。范围与下一步见[组合计划](planning/router-hust-terminal-chain.md)和[证据](../evidence/router-hust-terminal-chain-2026-10-14.md)，下面同日较早记录仅对应当时阶段。

2026-10-14 最新实机状态：新分配 32068 的 HUST ced6857/b47795c 已完成真实 HTTP→完整 family→核心逻辑终态→原账本的可选有限闭环。4 个 family/8 个 child 全部关联，stop string/断连核心 abort、重放、账本重开和自有包装恢复通过；不是原生 Router 已联通，也不是在线 KV 目录或性能验收。默认关闭、固定守卫、MOD import_only 不变。当前限制和下一步以[实机接线说明](planning/cedb477-live-terminal.md)及[证据](../evidence/cedb477-live-terminal-2026-10-14.md)为准，下方按日期保留历史。

2026-10-14：新版完整family、原核心清理、journal/ACK与同一账本已CPU组合；部分提交、后端准入拒绝和核心清理失败不得结清，通用Ended与业务结果仍分别提供。方法审计中的业务结果/健康/时钟/IPC为替身，公开schema未变，实际跨进程观测协议未冻结。候选安装接缝见[现场计划](planning/cedb477-terminal-boundaries.md)。

2026-10-13 当前迁移覆盖：用户选定 Core ced6857 / Ascend b47795c；新版本 profile、原生 KV 规范化和独立制品恢复见[版本适配入口](planning/hust-version-adapters.md)。以下固定旧源码、旧 publisher/终态 hook 和历史验收仅属于原提交；不能据此在新版本启用。新版可靠终态、共同水位恢复与在线目录接线仍须逐项验收。

2026-10-13 进程恢复补充：工具账本的排他打开覆盖创建/重放/事务/关闭，锁文件不删除；规范化本地路径限定单 owner，不声明多主机或别名隔离。CPU supervisor 另行验证旧 Router 进程组结束后才开启下一次交接；账本锁本身不提供发送 fencing。新 clock/请求命名空间不复用，旧尝试保留原身份和负债；旧观察可对账但不能授予新许可。真实服务状态尚由 fixture 提供，component-v0.1 及生产 ABI 不变。

2026-10-13 入口/恢复增量：一个 HTTP attempt 关联全部 rendered prompt × sampling child；实际 external/internal ID 从调用处捕获，全部组成功提交才封口。参考账本保留每 attempt 的原时钟；旧传输观察仍按旧时钟验证，可靠 ENDED 的新接收确认可使用当前 runtime 时钟，不跨时钟比较数值。新 runtime 必须是从未用过的时钟、同实例的新鲜观测和进位控制版本，计数必须保留旧负债；旧 dispatcher 停止证明目前是 CPU 调用方前置条件，不是自动完成的生产 fencing。异步 reserve 仅提交成功返回新许可，重复申请不重新许可；普通拒绝不关闭旧工作的观察通道。见[ADR](decisions/2026-10-13-entry-recovery.md)。基础公开 schema 未变，这些仍为 tools/experiments 接缝。

2026-10-13 实验通道 transport-observer-cpu-v1：源回调序号与账本生命周期序号分开；只接收 SEND_STARTED/NOT_SENT/OUTCOME_UNKNOWN，拒绝从该通道注入 backend ENDED。每个源观察的完整载荷和账本转换在同一事务持久化，重复冲突拒绝；终态先结清后的迟到关闭只记录观察。接收历史发送不等于许可新发送，重启后的 admission_ready 不自动打开。此协议是有界 loopback CPU 实验，未冻结生产 ABI；见[ADR](decisions/2026-10-13-online-transport.md)。

2026-10-12 候选接线补充：关联 begin 预留全部 child 额度，bind 使用实际内部 ID，全部成功提交后 seal，abandon 保留未决；已知 child 全部结束不替代 seal。事务接收实验提交成功才返回 applied，单一 owner 分配生命周期序号，重复终态使用原已提交载荷。重建历史时钟只用于重放校验，新派发暂停，未实现生产时钟交接。KV pool 的恢复结果/增量/控制确认都携带当前 ticket；lease 从控制请求发送时点计算，实时帧不续期。M7 必须在解码前限额并提供完整必需元数据，不能将缺失 parent/LoRA 当 None。新增对象均为实验端口，component-v0.1 未变；见[范围](planning/capture-recovery-wire-boundaries.md)。

2026-10-12 终态原型使用独立 `Binding(worker, instance, observer)` 与显式完整 family，所有子请求结束才产生一条正证据；不从缺失记录推断未接受，不从逻辑结束推断 KV/HBM free。M5 仍须统一生命周期序号和业务结果，observer 不能直接把通用 ended 伪装为 COMPLETED。进程内 journal/ACK/快照实验不改变生产协议，整进程恢复与 GC 未实现；见[候选边界](planning/terminal-observer-scope.md)。

2026-10-12 审查收尾：`experiments/router-extension/lifecycle_bridge.rs` 将真实 Lease 回调转换成 component-v0.1 的有限传输事件。每尝试最多两个事件，使用同一 Router 单调时钟；状态码不认定后端接受，EOF/error/drop 不认定结束。`tools/lifecycle_trace_probe.py` 在实验结束后将记录重放到现有账本 oracle；可靠在线投递、正常/取消终态、进程重启恢复仍未实现。终态来源与接线门槛见[固定 HUST 审计](planning/backend-terminal-evidence.md)。生产 ID 分配和内部请求关联尚未冻结，CPU 记录只支持有界 ASCII ID 子集。

2026-10-12 增量实验见[目录/流式适配 ADR](decisions/2026-10-12-bounded-router-adapters.md)。Rust 目录实验使用 `Binding`、不可复用的恢复 `Ticket`、规范化 `Block/Batch` 与排他快照/重放水位；这些是实验输入，不替代 component-v0.1，也不是已冻结的 HUST wire 或生产 ABI。一个分区只支持 worker 实例/publisher/完整身份/block_size、group 0/device/DP 0。`lookup=None` 表示未可信或身份不符，`Some(0)` 才是已观测未命中。

目录事件在 owner 写锁内处理，查询仅持读锁，不暴露索引 Arc；失败撤销整个精确视图，不能开放半批结果。普通增量不复制整个目录；资源到限额时完整重建必要父链。父链缺失、同位置哈希别名、序号缺口及超预算均退出可信模式。M7 仍负责解码前 payload 上限、完整身份核验、控制面新鲜度及连接代次；没有这些证据，不能把本实验接口直接接生产。

可选流式实验的本地 EOF/error/drop 继续属于传输证据，不能产生 backend ENDED。按消费进度拉取及释放 HTTP stream 不等于推理已取消；M3 保留发送后未知负债。流式参数必须显式选择，实验数值不作为生产默认。标准 `LifecycleEvent` 桥、可靠对账、进程重启恢复及 MOD 启停仍待实现。

基础在线对象现已有[标准库参考类型和端口](../src/prefix_routing_mod/contracts.py)、[版本化行为样例](../tests/fixtures/component-v0.1.json)及 CPU 组合测试，详见下方 v0.1。生产语言/ABI、事件索引绑定及 MOD 激活仍待宿主准入和人工评审。下表是最低语义，不代表所有数据源已存在。未知使用 null / unknown，不用 0 伪造测量；token 数、block 数、字节数不能混用。进程内耗时用单调时钟；跨主机时间不直接相减。

| 对象 / 唯一写入者 | 必需语义 | 消费者必须遵守 |
|---|---|---|
| RequestFeatures / M1 | request_id、compatibility_domain、model/template/tokenizer/config 指纹、input_tokens 可未知、prefix 特征、precision | 身份包含会影响 KV 的配置；不把字符串相似度当命中 |
| PrefixMatches / M2 | worker_id、epoch、连续匹配 tokens/blocks、source、observed_at、coverage、quality | observed / estimated / unknown 分离；事件缺失不等于 miss |
| WorkerState / M3 | worker_id、epoch、health、draining、load 分量、各分量时间/来源/覆盖范围 | TP ranks 不作为独立副本；后端 running 与本地 outstanding 不直接相加 |
| RouteDecision / M4 | decision_id、request_id、attempt_id、worker_id/epoch、policy/config_version、reason | 只是选择，不代表已经发送；M5 提交前复核合法性 |
| RequestOutcome / M5 | request_id/attempt_id、send_state、terminal_kind、时间、error_class、backend_confirmation | 同一终态幂等；取消客户端不等于后端完成；迟到结果可触发对账 |
| PlacementPlan / M8 | domain、prefix_scope、候选 worker/epoch、plan_version、valid_until、撤销原因 | 软偏好；失效/过期/拥塞时可绕过，不修改引擎驻留 |
| CapabilityReport / M7 | 接口来源、版本/配置指纹、verified/limited/unsupported、原因、证据引用 | 分开记录 discoverable、activatable、executed、performance_validated |

## 请求生命周期和责任

```text
received -> selected -> reserved -> sending -> accepted/acceptance_unknown
                                               -> streaming -> terminal
```

- M5 产生生命周期事件，M3 是账本唯一写入者。reserved 是本地预记账，不是引擎已预留 HBM。
- 发送前失败可明确撤销本次预记账；发送后超时可能已执行，不可盲目重发。
- terminal 包括 completed、failed、cancel_confirmed、outcome_unknown；“未知”不能当成功，也不能永久不对账。
- 以 attempt_id / event_id 幂等；旧代次通知不得污染新代次。服务 `instance_epoch` 与发布流 `publisher_epoch` 分离，发布器失效不清空仍运行实例的在途账本。具体持久化/超时对账方法 P2/P3 再选。
- 重启、模型或模板身份变化使旧索引、旧计划失效；订阅缺口需要恢复或明确降级。
- 精确模式缺失身份/事件证据时退出精确模式，基础策略仍须执行健康和隔离筛选。

## 配置契约（已代码化）

configs/example.json 是 schema_version=1 的静态单池配置。只接受 model、compatibility_domain、workers；每个 worker 只接受 worker_id 和 HTTP(S) origin。ID 与归一化 origin 唯一，禁止 URL 凭据、路径、查询、fragment。所有未知配置字段拒绝，避免拼写错误静默生效。

compatibility_domain 当前是操作员声明，不是自动验证的身份指纹；doctor 返回 config_valid 不意味着两个副本可共享缓存身份。模型目录、tokenizer/template 指纹、APC 与真实 block/hash 配置由 P1b 验证后扩展，不能靠写相同字符串提升能力等级。

## Ascend 描述与校准合同（新增规划，未代码化）

不修改当前 config schema 来伪装已支持。以下两类档案通过已有七类对象引用，最终字段在 P0/P1 结合真实接口冻结：

| 产物 / 所有者 | 最低语义 | 消费边界 |
|---|---|---|
| BackendCapability / M7 | 硬件与完整软件版本、端点/物理设备/逻辑号/rank 映射、语义与缓存布局身份、APC/图/事件等逐项支持和证据 | CapabilityReport 引用；包存在不代表能力支持，observed/estimated/unknown 分开 |
| ExecutionProfile / M6 | profile_id/version、执行指纹、输入/并发适用范围、冷暖状态、结果单位、样本/误差、证据与校准时间 | M7 验证配置适用性，M3 引用，M4/M8 只读；不远程逐请求校准或索取真实未来输出 |

三类身份分别为：影响复用合法性的语义域；block/head/dtype/事件 hash 等缓存布局与协议；图、调度预算、并行、软件/硬件和 Host 配额等执行性能配置。配置可能同时影响多类身份，必须显式记录依赖；不能仅凭名称推断影响范围。CPU 配额改变一般需要重新校准，不自动清除仍合法的前缀目录；worker 重启同时使对应驻留/计划代次失效。

WorkerState 可包含带时间/来源/覆盖范围的阶段摘要、实际 KV 压力和校准档案引用；图桶、padding 等默认离线诊断，不强制进入在线协议。实际引擎 batch 未知时保持未知，不能把 Router 当前请求集合当实际 batch。全局 NPU 利用率不能替代缺失的 Prefill/Decode 工作量。

PrefixMatches 的匹配 tokens 与实际省去计算 tokens 分开；allocated/reclaimable blocks 与 HBM 字节不同，保留单位及来源。Host 已提交的 KV 写入不自动认定为可复用；M7 须验证后端事件时点及其安全保证，不能由 Router 自己推测设备完成。

档案缺失、过期、指纹不符或超出校准范围时，禁止无标识套用旧成本，降级到有证据的基础策略；精确身份/事件缺失则另行退出精确模式。未来测试至少覆盖这些分支、异步事件、block/rank 映射及失效范围。

ExecutionProfile、PlacementPlan、CapabilityReport 完整 schema 随真实消费者和接入能力引入；不是本轮所有规划对象都已代码化。不能将本页视为生产接口冻结已完成，也不能将参考类型当后端真实 API。

## component-v0.1：可独立实现的语义接缝

| 参考类型 | 关键字段与限制 |
|---|---|
| WorkerRef | worker_id + instance_epoch；URL/逻辑卡号不代替实例身份，M7 从受控启动/可靠来源确认 |
| CacheRef | WorkerRef + publisher_epoch；对应 M2 的一个发布流，不是 M3 的账本代次 |
| SemanticIdentity | domain、model、tokenizer、template、kv_config 指纹；M7/M1 验证来源，类型存在不代表已验证 |
| RequestFeatures | request_id/domain、可空 identity/input_tokens、只读 token_ids、block_size、precision；exact 要求完整 token 序列与匹配计数，非 exact 禁止携带伪精确块 |
| ReadContext / Observation | Router 单调 clock_id/now_ns/max_age_ns；值可未知，来源/覆盖范围/接收时点独立。后端时钟不能直接带入 Router 时钟 |
| PrefixMatch | CacheRef、完整身份、block_size、连续完整 matched_tokens、trust、来源与时间；未知/恢复中为 null，可信 0 才是已观测 miss |
| WorkerState | ref/domain、控制 version、health/draining/观测时间；backend_running 与 local_outstanding 分开；不将两个覆盖重叠的计数相加 |
| DispatchContext | M5 分配的 request_id/attempt_id + 捕获的 config_version；每个真实发送尝试有唯一 ID |
| RouteDecision | 对应请求/尝试、目标实例、控制 worker_version、policy/config_version、有限 reason_code；不意味着已预留或发送 |
| LifecycleEvent | event/request/attempt/worker、sequence、kind、confirmation、Router 时点、有限 error_class；不是逐 token 的事件流 |

核心类型不可变。参考上限为 ID 128 UTF-8 bytes、262144 token IDs、u32 token ID 和非负 i64 计数/时间；用于阻止无界 fixture/跨语言溢出，不是 NPU 性能调参或实际服务上下文限制。部署应按模型、payload 和资源配置更低上限；超出精确特征预算可以显式退出精确模式，不能悄悄截断 token 后仍称完整匹配。不记录 token 原文/序列到日志。未知值不通过填 0 进入策略。

PrefixMatch 只接受事件或一致恢复协议来源，不接收请求历史作为 observed。M2 须验证完整覆盖、序号/epoch 及拓扑后才能标 trusted；参考类型校验无法替代这个过程。M4 必须同时比对 WorkerRef、语义身份、block_size 和新鲜度，不能拿旧实例的可信记录给新实例使用。

### 端口与并发合同

- `FeaturePort.extract(request_id, parsed_request)`：M1，使用本地已验证 tokenizer/template，输出已知或明确降级特征；不发逐请求远程查询。
- `CachePort.lookup(features, ReadContext)`：M2，返回不可变匹配元组；只读当前安装视图，不在请求链等待网络恢复。M7→M2 的 wire framing/恢复边界以[事件合同](kv-event-contract.md)为准，生产规范化 event ABI/索引接线仍在 R1 复核。
- `StatePort.snapshot()`：M3，当前实例状态快照；计数有各自来源/时点，健康与负载新鲜度不互相替代。
- `PolicyPort.select(features, matches, workers, DispatchContext, ReadContext)`：M4，只读函数，返回决定或无目的地。空目标应明确拒绝/按配置有界排队，不能绕过健康/隔离约束。
- `LedgerPort.reserve(features, decision, ReadContext)`：M3 原子检查当前实例、控制版本、配置版本、健康/排空、新鲜度、请求关联、重复与准入限额，再预记账；返回成功才允许这次发送。失败抛 `ReservationRejected`，无新增负债，M5 有界重读/选点或返回拒绝，不忙等。
- `LedgerPort.apply(LifecycleEvent)`：M3，返回 applied/duplicate/stale 或抛 `LifecycleRejected`；不接受未知尝试、不吞事件缺口/冲突。M5 对异常暂停相关派发并对账，保留负债，不能将异常当成功释放。

worker.version 是实例/健康/排空/隔离等**控制合法性版本**，不在每次负载计数变化时使所有并发决定失效。准入额度在 reserve 内原子检查，策略的负载视图可以短暂陈旧，但不能越过额度；生产实现需避免全局锁、重复分词/跨语言复制带来的 Host 放大，CPU oracle 不提供性能实现保证。

配置发布与 reserve 要有明确线性化顺序；新 config_version 拒绝尚未预记账的旧决定，已有工作继续完成/排空，不因配置切换抹掉负债。配置版本不得复用。reserve→发送期间的排空/重启桥接是 M5 宿主适配的强制验收项，不在这里假定分布式网络发送可以原子完成。

### 生命周期与错误行为

```text
reserved -> sending -> accepted -> streaming -> released
    |          |          |           |
    |          +----------+-----------+-> uncertain -> 对账确认 -> released
    +-> not_sent（明确未接受）-> released
```

- sending 允许直接 streaming 或已确认终态；一次发送/首次接受/首次流式各最多一个转换。COMPLETED/CANCEL_CONFIRMED 要有 backend ENDED；FAILED 无后端终止证据仍 uncertain。HTTP 5xx、超时、关闭响应体不自动提供该证据。
- 每次尝试 sequence 从 1 连续递增；同 event_id 同完整载荷为 duplicate，冲突或非法转换拒绝。晚到的可靠确认可解决 uncertain；已有 accepted/streaming 证据不能被后续“未接受”说法覆盖。
- M5 仅给实际状态转换分配 sequence；已 uncertain 后再次报告 FAILED/NONE 不构成转换，必须在事件产生处收敛。参考账本拒绝此类通知且不消耗序号/留存，可靠确认使用下一连续序号；错误诊断另走有界日志。不能把每个超时回调或 token 当生命周期事件。
- 发送前明确失败释放，允许新的 attempt_id；发送后 uncertain 保留本地负债，禁止同一请求自动重发。旧实例事件返回 stale，不碰新实例计数；旧负债需要 M7 的实际终止证据或可靠后端对账，不能凭 publisher 重启/超时删除。
- Router 进程重启和 tombstone GC/持久化尚未实现；生产留存必须有上限、明确去重窗口/ID 不复用与满额拒绝行为。测试账本只保留有限尝试、每尝试最多 8 个转换，满额拒绝，不声称已解决长时间运行。

六组版本化正例、反例和并发模拟见 `tests/test_component_contracts.py`；跨 M1/M2/M3/M4/模拟 M5 的调用见 `tests/test_component_flow.py`。它们不含生产代理、tokenizer、缓存索引、真实取消、图模式或性能验证。三人任务边界和待决生产接口见[工作包](planning/component-work-packages.md)。
