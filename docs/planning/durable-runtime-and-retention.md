# M3 持久提交、观测回收与启动恢复

更新：2026-10-17。状态：Rust 候选已完成 x86_64/aarch64 CPU 正确性、取消安全与本地盘开销验证；增量布局已显式装入固定私有宿主做真实 commit→ACK 资格，未完成长期回收，不作为生产接口冻结。前置真实公开 HTTP 有限链已复验通过，见[接线计划](native-public-chain.md)；历史快照结果见[旧证据](../../evidence/durable-runtime-candidate-2026-10-16.md)，当前结果见[本轮证据](../../evidence/durable-public-chain-2026-10-17.md)。

## 实际剩余问题

Rust RuntimeOwner 外已有候选持久包装：发送许可晚于 commit，存储结果不确定或等待提交的任务被取消时 fail closed；终态 cursor 绑定完整 source/worker/instance/observer 身份，整批先校验后结算。恢复默认暂停，相同实例和旧派发者停止声明通过后才 CAS takeover。固定宿主候选已接入，但 `previous_dispatcher_stopped` 仍是调用方证明，WorkerControl 也没有持续控制新鲜度到期机制，不能视为生产启动恢复完成。

后端 CoreEnds 已改为只在未确认窗口保留记录和重复保护，Frontend ACK 同一序号后精确回收；固定源码审查和 CPU 反例回归已通过。FrontendEnds 的实际 family/opaque child 仍未接可持续回收：TerminalJournal.retire 只接受已证明归属的 managed ID，不能把仅有 `prm-` 前缀的真实 HUST 入口直接标成 managed。受信 Router 归属的方案与取舍见[终态源回收 ADR](../decisions/2026-10-16-terminal-source-retention.md)。20 分钟后端稳定性没有启用该 hook，因此不能替代 Frontend 长期留存验收。

## 必须保持的提交顺序

1. 准入预留持久提交完成后才返回发送许可。提交失败/不确定或许可丢失不得派发；已提交记录保留，不能借超时清除。
2. 传输 EOF/取消/5xx 只更新传输证据，不释放已发送负债；可靠终态完整批次必须先核对实例、顺序和全部 family，再提交结算。
3. 同一事务提交 M3 更新、终态消费者 binding/cursor 和去重位置。提交确认后才向源 ACK；源端回收再确认这一已持久边界。丢 ACK 可重复投递，不重复释放。
4. Router 重启先取得同一存储的排他所有权，确认旧派发者已停止，分配新请求命名空间并恢复为暂停状态。核对新鲜实际服务身份后再开放准入；旧未决负债保留，不能从缺记录/旧 healthy/新进程假定后端空闲。
5. 原生 KV 无完整已热快照时，Router 重启可将 M2 保持 unknown 并走合法基础负载策略。M2 的恢复失败不清 M3，也不能阻塞 HTTP 请求等待建树。

## 回收协议的设计约束

固定 HUST 源码已确认：外部 ID 被保存，internal ID 默认增加 8 位随机片段，并行 child 使用 `index_internal-id`；清理 observer 位于删除请求的固定路径并受完整源码 hash 守卫。该证据足以让 Core ACK 后回收未确认窗口，但外部 `X-Request-Id` 可被客户端设置，`prm-` 前缀本身不是 Router 归属证明。下一步仍须明确受信 Router 会话、已持久消费水位、源端退役边界和迟到重复的拒绝规则。

已完成且已持久消费的记录可以回收；未决 family、未确认提交和旧实例负债不能按 TTL 删除。低于源退役边界的消费者不能从 0 重放或清空账本，必须恢复持久游标或进入可诊断的暂停状态。core seen、frontend children/statuses、发送队列、墓碑、存储和重建暂态都要有总预算。

需要改动的位置集中在 `runtime_owner.rs` 的准入/结算/恢复、独立存储适配、`terminal_transport.py` / `terminal_journal.py` 的自身元数据留存和窄幅控制接口；不改 HUST 调度、KV 分配/淘汰或设备执行。新控制行为形成版本化 fixture/ADR 后再实施，保留默认关闭和源码守卫。

## 本地存储评估

Rust helper 与私有宿主候选锁定 `rusqlite 0.40.2` 的最小 bundled feature：WAL/FULL、owner/generation CAS、有界状态、64 等待槽和 5 秒提交期限；磁盘任务不在同步 Lease 回调或 M3 状态锁内执行。旧 Python SQLite RPC 未进入生产路径。历史快照成绩保留原负载边界；本轮规范化行布局在相同持久性、每次修改一条 active 的两轮 aarch64 对照中，512 active 的 P50 为 2.577–2.598ms，对照完整快照 4.239–4.665ms，0 active 尾延迟无优势。行布局仍有 O(n) checkpoint/比较/头部成本，不将存储结果推导为服务收益。

固定 Router 原依赖保留，显式候选 feature 只在私有复制中新增 SQLite 所需三个包，审计确保原有版本/来源/checksum 不变。先评估该行布局的完整接线成本，再决定是否需要 group commit；它会增加排队/取消复杂度，不预先同时实现第二套机制。若后续采用分组提交，每个请求仍只能在包含自身预留的事务提交后取得许可，统计包含等批时间。生产默认、长期恢复和正式依赖仍待验收；不手写 WAL，不依赖仅在正常退出时保存的 checkpoint。

## 冻结前验收

- CPU：提交前/后异常、丢许可、完整批尾损坏、重复/冲突、磁盘错误、双 owner、配置竞争、强制退出与重启。已发请求在任意截断点不得无负债恢复。
- 真实接线：超过早期 64 family/256 child 上限的连续服务；建议至少 512 family、1024 child，检查稳态资源趋势、ACK/退役与重复窗口。未知故障单独保留，不混入容量成绩。
- 先补真实控制失联、健康过期和暂停恢复，再比较直连/原 Router/仅加接线的配对 Host 开销；分组提交不能以遗漏持久性换低延迟。
- 上述通过后固定生产目录、类型/错误码、线程/时钟、配置、启动/排空/关闭和替身，交叉审查后正式 A/B/C 分工。最终容量曲线、M8 和高级策略不作为这一冻结前置。

存储/协议阶段主要用 Linux CPU；真实持续服务与最终恢复回归使用两个 TP1 NPU 副本。当前完成范围和 CI 以 [progress](../progress.md) 为准。
