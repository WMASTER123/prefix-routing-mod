# 决策索引

- [2026-10-17：增量 SQLite 候选与真实持久接线](2026-10-17-incremental-runtime-store.md)；最小依赖、变化行写入、同事务游标和有限 commit→ACK 资格，不冻结生产恢复。

- [2026-10-14：原Router准入/转发与HUST终态有限组合](2026-10-14-router-live-chain.md)；沿用原宿主/观察/账本，不冻结生产IPC或增加索引。

- [2026-10-14：复用 HUST utility/endpoint 的有限终态实机接线](2026-10-14-live-terminal-utility.md)；默认关闭、8处可逆包装，不冻结生产ABI。

- [2026-10-13：HUST cedb477 迁移与版本适配](2026-10-13-hust-version-adapters.md)；新旧映射、能力边界、只读制品复用与独立环境。

- [2026-10-13：实际 Router 进程故障与单 owner 恢复](2026-10-13-router-process-recovery.md)；本地排他、运行时标识与完整 CPU 组合验证，真实 HUST 接线另验。

- [2026-10-13：完整入口关联、动态准入与显式启动交接](2026-10-13-entry-recovery.md)；CPU 实验，不冻结引擎观测安装或生产恢复 ABI。

- [2026-10-13：原生回调到事务接收的在线 CPU 通道](2026-10-13-online-transport.md)；本地有界留存、控制线程投递，不增加生产账本服务。

- [2026-10-12：wire 规范化与复用目录 CPU 接线](2026-10-12-wire-directory-cpu.md)；分区额度与控制新鲜度，真实服务未放行。

- [2026-10-12：关联封口与可恢复接收 CPU 接线](2026-10-12-family-capture-recovery.md)；延续批准范围，生产安装与存储选择未冻结。

- [2026-10-12：逻辑终态与有界在线投递 CPU 原型](2026-10-12-terminal-observer-cpu.md)；用户批准评估，不安装引擎 observer，不冻结生产接口。

- [2026-10-12：流式审查修复与有限标准生命周期实验](2026-10-12-stream-lifecycle-review.md)；传输记录连接旧账本，不代替可靠终态或生产投递。

- [2026-10-12：有界目录与流式资源适配实验](2026-10-12-bounded-router-adapters.md)；复用原索引/HTTP，资源与恢复 CPU 证据不替代生产契约及真机验收。

- [2026-10-11：可执行组件边界与宿主未决项](2026-10-11-component-seams.md)；研发候选，独立区分服务/发布代次，CPU 合同先行，不提前选择生产语言。

- [2026-10-11：HUST 配套源码候选与隔离验证](2026-10-11-hust-runtime-candidate.md)；替代继续盲选官方版本的执行方向，尚未冻结运行基线。
- [2026-10-11：可选 HUST publisher hook 实验](2026-10-11-optional-hust-publisher.md)；用户批准，默认关闭，生产交付待压力/开销审查。

- [ADR-001：脚手架和诚实的 MOD 状态](001-scaffold-boundary.md)。
- [2026-10-11：Ascend 优先规划与性能证据边界](2026-10-11-ascend-first.md)。
- [2026-10-11：预装组合与 Qwen2.5-14B 首验](2026-10-11-qwen14b-first.md)；局部基线已定，完整 ADR-003 仍待事件/恢复证据。
- [2026-10-16：终态源回收与受信请求归属](2026-10-16-terminal-source-retention.md)；Core ACK 回收已实现，Frontend 归属与 retire 待选择/验证。
- 待 ADR-002：单 Router 宿主、扩展点、语言和构建制品（P1a）。
- 待 ADR-003：引擎版本组合、原生 KV 事件及 observer 必要性（P1b）。
- 待 ADR-004：完整身份、恢复和账本契约（P2/P3）。
- 待 ADR-005：SLO、实验门槛与默认策略（P2/P4）。

每份 ADR 包括问题、证据、选项、决定、代价、退出条件和状态；不得把待验证选项写成已决定。
