# M3 持久提交、观测回收与启动恢复

更新：2026-10-18。状态：Rust 候选已完成 x86_64/aarch64 CPU 正确性、双 NPU 真正 retire 回复丢失、520 family/1040 child 累计资源以及 Router 强退/暂停恢复/实例替换资格。基于这些证据冻结首版恢复与回收合同 `recovery-retirement-v1`；整个 MOD 的公开配置、Manager 生命周期、跨版本 ABI 和性能策略仍未冻结。前置真实公开 HTTP 有限链见[接线计划](native-public-chain.md)，本轮结果见[恢复与回收证据](../../evidence/recovery-retirement-qualification-2026-10-18.md)。

## 实际剩余问题

Rust RuntimeOwner 外已有候选持久包装：发送许可晚于 commit，存储结果不确定或等待提交的任务被取消时 fail closed；终态 cursor 绑定完整 source/worker/instance/observer 身份，整批先校验后结算。恢复默认暂停，相同实例和旧派发者停止证明通过后才 CAS takeover。真实强退已由 supervisor 停止旧进程这一事实作为证明并完成恢复；它仍不是跨主机租约，WorkerControl 也没有持续控制新鲜度到期机制，不能外推为分布式 leader election。

后端 CoreEnds 已改为只在未确认窗口保留记录和重复保护，Frontend ACK 同一序号后精确回收；固定源码审查和 CPU 反例回归已通过。Frontend family/child 退役已接入双副本 Rust 持久公开链：只有显式受管模式且 durable store 存在时，在 commit 和 ACK 后 retire；默认仍 observe-only。真正回复丢失、520 family/1040 child 有界资源和崩溃恢复已在双 NPU 通过；它们仍不是多日稳定性或容量收益。受信 Router 归属的方案与取舍见[终态源回收 ADR](../decisions/2026-10-16-terminal-source-retention.md)。

## 已冻结：`recovery-retirement-v1`

以下是三人并行开发必须共同遵守的首版合同；字段或顺序变化必须新增 profile/schema 版本并重跑 CPU、固定宿主和双 NPU 资格，不能静默修改：

1. `TerminalCursor` 固定携带 `source_id/worker_url/instance_epoch/observer_epoch/through`；`TerminalRecord` 固定携带 `sequence/attempt_id/worker_url/instance_epoch`。来源绑定不是普通标签，任何冲突都失败关闭。
2. `RuntimeStore` 的 `initialize/load/commit/takeover` 保持 create-once、owner/generation CAS；新建账本在首个请求前持久化所有终态来源的零水位绑定。
3. 顺序固定为：准入预留 commit → 发送许可；终态整批验证 → M3 与 cursor 原子 commit → 源 ACK → 同一水位 retire。响应丢失只允许以完全相同水位幂等重试。
4. 恢复固定为 paused：旧 active 保留为 uncertain；验证旧 dispatcher 已停止、worker/instance/observer/ownership 全相同后才 `resume`。暂停期不回退到无账本转发。
5. Router 重启后 M2 固定从 unknown 开始，不能用原生 replay END 推断暖目录完整。当前 HUST END 不携带权威当前序号/快照水位；即使 worker/instance 相同，也只恢复 M3 的终态来源身份与负债，不安装回放所得暖目录。身份改变仍失败关闭，M2 unknown 不能清除 M3 债务。
6. 已 retire 水位以下的旧 cursor 是不可恢复错误，当前固定 HUST 端点返回 503；调用方必须使用持久 cursor 或保持暂停，不能从 0 猜测重建终态。
7. 默认仍是 `observe_only`。只有专属回环副本池、Router 唯一业务入口、固定源码守卫和显式 `exclusive_router_v1` 同时成立时允许退役；首版不支持共享后端业务直连。
8. 有界资源合同：Router retired tombstone 最多 16；Frontend/Core 已确认历史在 ACK/retire 后为 0；来源每批最多 64，family/child 与目录/传输限额沿用现有显式上限。未知或未决记录不按 TTL 回收。

当前代码仍位于 `experiments/router-extension/` 候选目录，冻结的是语义和调用边界，不是永久文件路径或对外 crate ABI。正式迁入生产目录时只能做机械迁移和适配层映射；语义变化需单独评审。

## 必须保持的提交顺序

1. 准入预留持久提交完成后才返回发送许可。提交失败/不确定或许可丢失不得派发；已提交记录保留，不能借超时清除。
2. 传输 EOF/取消/5xx 只更新传输证据，不释放已发送负债；可靠终态完整批次必须先核对实例、顺序和全部 family，再提交结算。
3. 同一事务提交 M3 更新、终态消费者 binding/cursor 和去重位置。提交确认后才向源 ACK；源端回收再确认这一已持久边界。丢 ACK 可重复投递，不重复释放。
4. Router 重启先取得同一存储的排他所有权，确认旧派发者已停止，分配新请求命名空间并恢复为暂停状态。核对新鲜实际服务身份后再开放准入；旧未决负债保留，不能从缺记录/旧 healthy/新进程假定后端空闲。
5. 当前原生 KV 没有可证明完整的已热快照，因此 Router 重启必须将 M2 保持 unknown 并走合法基础负载策略。后续新 profile 只有提供权威快照水位和连续增量边界，才可另行增加暖目录恢复；该扩展不得清 M3，也不能阻塞 HTTP 请求等待建树。

## 回收协议的设计约束

固定 HUST 源码已确认：外部 ID 被保存，internal ID 默认增加 8 位随机片段，并行 child 使用 `index_internal-id`；清理 observer 位于删除请求的固定路径并受完整源码 hash 守卫。该证据足以让 Core ACK 后回收未确认窗口，但外部 `X-Request-Id` 可被客户端设置，`prm-` 前缀本身不是 Router 归属证明。首版已选择专属池、可信本机进程、回环后端和 Router 覆盖 ID；只有显式 exclusive_router_v1 才允许退役。持久水位、同源 ACK/退役、重复调用、真正回复丢失和强退恢复已在同一 Rust 链验证；持续控制失效/健康过期仍属生产接线门槛。

已完成且已持久消费的记录可以回收；未决 family、未确认提交和旧实例负债不能按 TTL 删除。低于源退役边界的消费者不能从 0 重放或清空账本，必须恢复持久游标或进入可诊断的暂停状态。core seen、frontend children/statuses、发送队列、墓碑、存储和重建暂态都要有总预算。

需要改动的位置集中在 `runtime_owner.rs` 的准入/结算/恢复、独立存储适配、`terminal_transport.py` / `terminal_journal.py` 的自身元数据留存和窄幅控制接口；不改 HUST 调度、KV 分配/淘汰或设备执行。新控制行为形成版本化 fixture/ADR 后再实施，保留默认关闭和源码守卫。

## 本地存储评估

Rust helper 与私有宿主候选锁定 `rusqlite 0.40.2` 的最小 bundled feature：WAL/FULL、owner/generation CAS、有界状态、64 等待槽和 5 秒提交期限；磁盘任务不在同步 Lease 回调或 M3 状态锁内执行。旧 Python SQLite RPC 未进入生产路径。历史快照成绩保留原负载边界；本轮规范化行布局在相同持久性、每次修改一条 active 的两轮 aarch64 对照中，512 active 的 P50 为 2.577–2.598ms，对照完整快照 4.239–4.665ms，0 active 尾延迟无优势。行布局仍有 O(n) checkpoint/比较/头部成本，不将存储结果推导为服务收益。

固定 Router 原依赖保留，显式候选 feature 只在私有复制中新增 SQLite 所需三个包，审计确保原有版本/来源/checksum 不变。先评估该行布局的完整接线成本，再决定是否需要 group commit；它会增加排队/取消复杂度，不预先同时实现第二套机制。若后续采用分组提交，每个请求仍只能在包含自身预留的事务提交后取得许可，统计包含等批时间。生产默认、长期恢复和正式依赖仍待验收；不手写 WAL，不依赖仅在正常退出时保存的 checkpoint。

## 本合同已完成的验收与后续门槛

- CPU：提交前/后异常、丢许可、完整批尾损坏、重复/冲突、磁盘错误、双 owner、配置竞争、强制退出与重启。已发请求在任意截断点不得无负债恢复。
- 真实接线：同一 Rust 链的显式归属、真实回复丢失、520 family/1040 child、强退恢复、旧 cursor 和实际实例替换均已通过，因此本页所列合同已冻结。
- 后续仍须补持续控制失联/健康过期、Manager 启停/排空、预构建制品和同底座配对开销；这些可以扩展控制层，但不能破坏已冻结的保债、身份和 commit→ACK→retire 顺序。
- A/B/C 可按现有端口正式并行；生产目录迁移、公开配置/错误码、Manager 生命周期和测试替身由集成负责人另行冻结。最终容量曲线、M8 和高级策略不是当前合同的一部分。

存储/协议阶段主要用 Linux CPU；真实持续服务与最终恢复回归使用两个 TP1 NPU 副本。当前完成范围和 CI 以 [progress](../progress.md) 为准。
