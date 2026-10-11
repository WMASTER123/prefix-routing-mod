# 接口契约 v0（设计基线，尚未全部代码化）

契约字段在 P1 接线前复核。下表是最低语义，不代表所有数据源已存在。未知使用 null / unknown，不用 0 伪造测量；token 数、block 数、字节数不能混用。进程内耗时用单调时钟；跨主机时间不直接相减。

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
- 以 attempt_id / event_id 幂等；旧代次通知不得污染新代次。具体持久化/超时对账方法 P2/P3 再选。
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

下一步为七对象及档案补版本化样例和自动契约测试，再交叉评审。当前不能将本页视为 P0 接口冻结已完成，也不能将规划合同当真实 API。
