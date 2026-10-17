# ADR：增量 SQLite 候选与真实持久接线

日期：2026-10-17。状态：实施与测量中，未选定生产默认值。

完整快照基线每次重写全部在途记录，aarch64 已测出提交成本随 active 增长。本轮在同一个 SQLite 适配器内增加显式选择的行布局：沿用 WAL/FULL、单写、owner/generation CAS、原 RuntimeStore 合同和取消失败关闭。完整快照布局保留作正确性及性能对照；两种格式分别创建数据库，不隐式迁移已有文件。

行布局将 worker、active attempt 和 terminal cursor 分开保存，每行包含摘要。小型头部保存所有权、代次、配置、顺序和有界 retired 列表。存储器仅缓存最近成功提交的状态，用它比较变化；同一事务 CAS 更新头部、写入变化行并删除消失行。提交失败不得推进缓存。恢复在同一个 SQLite 读事务中读取头部与行，核验长度、摘要、键、计数和完整 binding，禁止拼接不同代次。

此候选减少重写和序列化完整 active 载荷，但调用方 checkpoint、状态复制、顺序头部仍随记录数增长；不能据此声称请求路径 O(1)。先测相同负载、相同持久性和相同磁盘条件下的基线与行布局，再决定是否有必要引入 group commit。分组提交会增加排队与取消复杂度，本轮不同时实现第二套并发调度机制。

真实接线实验可以显式使用候选存储，要求预留提交后才发送、终态与 cursor 同事务、确认提交后才 ACK。生产 Manager、源端 family 退役、健康租约和进程强制退出恢复仍分别验收；有限实机实验不自动冻结生产接口。

依赖审查发现，直接启用 rusqlite 默认 feature 会带入本实验不使用的语句缓存/wasm 分支，并可能改变原锁中的 wasm 依赖。最终 helper 与候选宿主统一使用 `default-features=false, features=["bundled"]`；相对原宿主只新增 rusqlite 0.40.2、libsqlite3-sys 0.38.2、fallible-streaming-iterator 0.1.9，原包版本、来源、校验和及原依赖边均保持不变。`tools/audit_router_candidate_lock.py` 与 fixture 校验锁差异；这不是升级 HUST 引擎依赖。

代码提交 `3264050` 的 57 项 helper 测试覆盖事务回滚、真正子进程未提交退出、磁盘 active 保债、暂停恢复、owner CAS、整批尾部损坏和行键/身份错配。原宿主依赖不变的 volatile 模式保留；SQLite 仅在私有候选复制中显式启用。后续实机与存储对照结果单列证据，不将构建通过当作性能通过。
