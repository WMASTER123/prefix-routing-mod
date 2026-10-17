# Rust M3 持久提交候选与本地开销

日期：2026-10-16。结论：已形成可编译、可故障注入的 Rust 候选层，但尚未装入固定 Router 宿主，也未冻结为生产存储方案。

## 本轮验证了什么

- `reserve_async` 在返回发送许可前完成持久提交；提交失败或结果不确定时不返回许可，并将当前 runtime 置为暂停。
- 终态结算、M3 checkpoint 和终态 source cursor 在一个存储替换事务中提交；成功后才允许调用方推进 ACK。终态提交失败时，磁盘仍保留旧负债，当前进程停止新准入。
- Router 恢复后默认暂停，旧 active 全部恢复为 `uncertain`。只有调用方确认旧派发者已停止、相同 worker URL/instance/cache identity 已重新核对且配置版本进位，才允许 CAS takeover。
- SQLite 候选使用单行有界快照、SHA-256 内容校验、owner/generation CAS、WAL、`synchronous=FULL`、64 页自动 checkpoint 和 4 MiB journal size limit。磁盘操作只在异步准入/终态提交任务中执行，不进入同步 `Lease` 回调；等待槽位为 64，单次提交期限为 5 秒。
- 旧 Python SQLite oracle 没有进入生产请求链；新增 Rust 层仍复用同一个 `RuntimeOwner` 状态机，没有第二套 M3 语义实现。

`rusqlite 0.40.2` 被锁在独立 helper crate，尚未修改固定 Router 的原 `Cargo.toml`/lock。SQLite 官方说明 WAL 同机单写者约束及 checkpoint 尾部成本；`synchronous=FULL` 在 WAL 下为每次提交增加同步以保证断电耐久性。[WAL](https://www.sqlite.org/wal.html)、[synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)、[rusqlite transaction](https://docs.rs/rusqlite/0.40.2/rusqlite/struct.Connection.html#method.transaction_with_behavior)。

## 正确性与审查结果

Linux x86_64、Rust 1.99.0：helper release 测试 `44 passed`，`cargo clippy --all-targets -- -D warnings` 通过。新增反例包括：

- commit 前失败与 commit 后返回不确定均不产生发送许可；
- 终态 commit 失败后当前进程暂停，重开仍保留旧负债；
- 恢复默认暂停、实例不一致拒绝、旧 owner 写入被 CAS 围栏；
- 数据库缺失不按空账本启动，重复初始化、内容损坏、快照超预算均拒绝；
- 终态序号/身份/未知 attempt 冲突不推进 cursor。

这组测试没有模拟整机断电，也没有证明旧派发进程已经被操作系统停止；`previous_dispatcher_stopped` 仍是宿主必须提供的外部前置证据。

## 本地提交开销

命令使用 release example `sqlite_runtime_store_probe`，逐次提交完整当前快照，每组分别创建新数据库。结果只用于方案选择，不是 Router/NPU 性能成绩。

| 文件系统 | active | 次数 | P50 | P95 | P99 | 最大 |
|---|---:|---:|---:|---:|---:|---:|
| WSL ext4 `/tmp` | 0 | 1000 | 580 µs | 1814 µs | 3345 µs | 7481 µs |
| WSL ext4 `/tmp` | 64 | 1000 | 585 µs | 1856 µs | 2614 µs | 12677 µs |
| WSL ext4 `/tmp` | 512 | 1000 | 672 µs | 1916 µs | 2632 µs | 4836 µs |
| WSL 挂载 NTFS | 0 | 500 | 1965 µs | 4180 µs | 5833 µs | 12164 µs |
| WSL 挂载 NTFS | 64 | 500 | 2526 µs | 5525 µs | 7572 µs | 12727 µs |
| WSL 挂载 NTFS | 512 | 500 | 2574 µs | 6388 µs | 16020 µs | 26376 µs |

判断：完整快照方案的正确性边界清晰，但每请求一次 FULL commit 的 P50 已是约 0.6 ms（ext4）且挂载文件系统尾部明显放大，不能直接宣布适合生产。下一步在 aarch64 目标盘重测，并比较规范化增量表与有界 group commit；任何分组都必须保持“该请求提交完成后才返回其许可”。

## 尚未解决

1. 真实 HUST terminal source 的 ACK/retire/GC 还没有接到这个 cursor；core seen 和 frontend family/child 的长期上限未解除。
2. 恢复时只核对显式传入的相同实例信息，尚未实现持续控制新鲜度到期、实际 identity endpoint 刷新及旧 dispatcher 的自动停止证明。
3. 当前终态批次拒绝未知 attempt；共享同一后端的非本 Router 请求如何用受信命名空间安全跳过尚未确定。
4. 尚未做进程 `SIGKILL` 各截断点、aarch64 编译/文件系统开销、512 family/1024 child 持续服务和真实双副本恢复。

因此 MOD 继续保持 `import_only`，真实公开链成绩和容量结论不变。
