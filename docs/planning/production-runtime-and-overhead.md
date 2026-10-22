# 生产接线、Manager 与开销实施边界

2026-10-22，状态：后续工作包的具体合同草案，尚未实现生产服务。现行成绩以 [progress](../progress.md) 为准。

本日后续：每来源持续终态驱动的 CPU 候选已实现，见[驱动 ADR](../decisions/2026-10-22-terminal-driver.md)。下一步复用它接入真实、限时、整批校验的运输适配，而不是再写另一个循环。现有有限工具的 panic/逐批重复资格断言不直接迁入生产；尤其 ACK 回应可能包含更新的终态，不得因非空而误判来源失效。生产请求与持续驱动装配、信号排空和 Manager 均仍待验收。

## 运行形态与目录

保留一个 Rust Router 在线进程：原 HTTP/SSE + M1/M2/M3/M4/M5；每后端的 KV 与控制工作在阻塞线程，可靠终态由独立有界异步循环持续对账。Python 仅负责静态配置、doctor、版本映射、后端可选 hook 和启动适配。当前 `hust_runtime_probe` 是有限实验，不是用户服务入口；其请求之间显式 reconcile 的调度不能直接当常驻服务使用。

生产代码目标为 `runtime/router/`：一个 crate 的 `src/` 包含 features、cache、control、admission、store、policy、backend、service；文件按职责归属，不建立第二代理、第二索引或第二账本。宿主 patch/staging 与有限 probe 留在 `experiments/router-extension/`，Python 控制代码留在 `src/prefix_routing_mod/`。迁移共享实现时先更新唯一源码位置和所有 staging/CI 路径，逐项比较字节/行为，禁止复制出两套实现。目标目录及迁移本身仍待验收，不因本文存在而宣称已交付。

## 启停与 Manager 复用

已读固定 `extension-manager@00d1b940` 安装代码：CLI `run -- <command>` 最终调用 `process_supervisor.supervise`，支持监管显式命令、转发信号和清理自有进程树；provider 负责描述/检查/配置计划，不提供隐式 apply/delete。复用这一监管层，不另写后台 daemon/PID 文件服务。

Router service 必须自己完成以下状态转换，再将真实入口加入 MOD manifest：

1. starting：校验制品/profile/配置，独占打开账本；恢复默认 paused，不建立新空账本覆盖旧状态。
2. paused → serving：完成实际身份、控制新鲜度、来源游标与旧 dispatcher 停止核验。M2 暖恢复维持 unknown。
3. serving → draining：收到自身停止信号时，先关闭所有控制门和新准入，撤销 readiness；继续已有 HTTP/SSE 和可靠终态对账。
4. draining → stopped：债务和自身 HTTP 工作均清空后关闭监听/订阅/存储；超过显式 drain 期限，保留持久未决状态并返回可诊断退出码，不能按超时结清。
5. Manager 的 shutdown grace 必须覆盖 Router drain 期限及清理余量。信号二次强退/Manager 超时仍由持久恢复合同保债，不能把 kill 当排空通过。

只管理自己的 Router，接入已有 HUST 服务，不部署或停止用户的后端。Manager 无法证明 Runtime 有效时继续保持 descriptor-only；`run` 能监管命令不等于本 MOD 已可 enable。生产制品、终态后台循环、信号排空和 Manager 真正启用仍为未完成项。

## 公开配置与错误

现有 `configs/example.json` schema 1 只用于静态池校验，不添加实验字段。生产配置采用单独版本化 schema，并由 Python 校验/生成 Rust 可读的同一合同 fixture；Rust 再次拒绝未知字段和非法值。必须先实现并测消费者，才冻结字段。

最小配置包含：固定 backend profile、模型与 tokenizer 文件指纹、Router 监听地址、worker ID/origin、控制凭据的环境变量引用、IPC 地址、账本路径、准入/目录/传输总预算、控制刷新/TTL 和 drain 期限。凭据值不入公开 JSON、日志、doctor 或 Git；实际来源 epoch 由运行观测得到，不能从配置凭空生成。拒绝把有限 probe 私有 JSON 当公开 schema。对外监听需显式选择；测试默认回环。

拟采用下列稳定错误类别；当前宿主仍以原 400/503 为主，**以下映射尚未接入**：

| 类别 | 拟定 HTTP | 语义 |
|---|---|---|
| invalid_request / unsupported_input | 400 | 发送前拒绝，无新增已发负债 |
| admission_capacity | 429 | 发送前额度不足；不进行隐式无限排队 |
| control_unavailable / runtime_paused / draining | 503 | 无合法目的地，不绕过准入 |
| source_identity_changed | 503 | 对应实例隔离；诊断与旧债保留 |
| durable_store_uncertain | 503 | 停止新派发，提交结果待恢复核验 |
| upstream_outcome_unknown | 502/504 | 可能已经执行；禁止按 HTTP 状态自动重发 |

错误正文只返回有限 code、request_id 和安全提示。只有已证明未发送的拒绝才能声明可重试；不能对所有 503 统一标记 retryable。M3 控制围栏与当前宿主 registry revision 分开，由集成负责人统一定义。

## 尽早进行的同底座开销对照

先使用同一设备/模型/APC/调度参数，交错运行并保留全部失败；明确每条链的功能差异：

| 链 | 作用 |
|---|---|
| 后端直连 | Engine 与压测端下界；不冒称有相同准入/持久性 |
| 原 Router，原生基础策略 | 代理/序列化/转发成本 |
| 扩展宿主默认关闭 | 宿主接缝本身成本 |
| MOD 基础选点 + 特征/目录/控制/终态 | 完整观测和 CPU 接线成本 |
| 上述链 + 相同 WAL/FULL 持久性 | 持久提交、排队、ACK/retire 的增量成本 |

保留模型首次加载/图冷/缓存冷暖条件，测重复分词、请求与目录锁、Host CPU/配额、RSS、持久提交尾延迟和压测端饱和。不得用 1024 上下文、1 GiB KV、每副本 2 准入的正确性参数直接得出生产容量；先记录并合理校准正式上下文/KV/并发预算。没有收益时先根据测量修正接线，不能叠加新策略掩盖成本。

随后在同样后端调优机会下比较 RoundRobin、最低在途、固定 Router 实际支持的 cache-aware 强基线和本 MOD；分别报告有/无额外准入保护的差异。公开/可复现负载覆盖高复用、低复用、热点漂移、工作集压力和输入/输出混合长度，策略开发前固定负载生成种子、阈值和失败口径。没有业务 SLO 时报告多阈值容量曲线、P95/P99、完成/拒绝/超时率，保留失败分母；SSE chunk 间隔不冒称逐 token 停顿。

## 任务卡和验收顺序

- A：控制身份与目录、升级 fixture、资源测试；交付保债无关的控制门/目录端口。
- B：常驻 HTTP 装配、一次分词、原转发/SSE、输入/错误、信号排空；不得自己复制账本。
- C：唯一持久账本、终态后台循环、Manager 监管适配、制品与开销/容量工具。

共享类型/配置/错误和迁移由一名集成负责人统一修改；A 审控制来源，B 审发送/取消，C 审持久性，成员对各自测试负责。当前先完成控制门的 CPU→固定宿主→双 NPU 故障资格，再迁移常驻 service；随后 Manager 真启停与配对开销并行，最后做容量曲线。当前无需先设计新算法。
