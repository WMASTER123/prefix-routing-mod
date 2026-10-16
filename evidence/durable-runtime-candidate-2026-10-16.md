# Rust M3 持久提交候选与本地开销

日期：2026-10-16。结论：已形成可编译、可故障注入的 Rust 候选层，并补齐取消安全和终态源身份围栏；但尚未装入固定 Router 宿主，也未冻结为生产存储方案。

## 本轮验证了什么

- `reserve_async` 在返回发送许可前完成持久提交；提交失败或结果不确定时不返回许可，并将当前 runtime 置为暂停。
- 终态结算、M3 checkpoint 和终态 source cursor 在一个存储替换事务中提交；成功后才允许调用方推进 ACK。终态提交失败时，磁盘仍保留旧负债，当前进程停止新准入。
- Router 恢复后默认暂停，旧 active 全部恢复为 `uncertain`。只有调用方确认旧派发者已停止、相同 worker URL/instance/cache identity 已重新核对且配置版本进位，才允许 CAS takeover。
- SQLite 候选使用单行有界快照、SHA-256 内容校验、owner/generation CAS、WAL、`synchronous=FULL`、64 页自动 checkpoint 和 4 MiB journal size limit。磁盘操作只在异步准入/终态提交任务中执行，不进入同步 `Lease` 回调；等待槽位为 64，单次提交期限为 5 秒。
- 旧 Python SQLite oracle 没有进入生产请求链；新增 Rust 层仍复用同一个 `RuntimeOwner` 状态机，没有第二套 M3 语义实现。
- 终态 cursor 现在绑定 source ID、worker URL、instance epoch 和 observer epoch；同一实例不能静默换 source/observer 后从旧水位继续。整个批次先完成结构校验，再修改任何请求负债。
- `reserve`、终态结算和 takeover 在修改内存后均由取消围栏保护。异步任务若在阻塞存储提交期间被丢弃，当前 runtime 立即失败关闭；后台提交即使随后成功，也只能由恢复流程重新装载，不能让旧进程继续准入。这样处理是因为 Tokio 明确说明已经开始的 `spawn_blocking` 任务不能被 abort：[Tokio 1.53.1 spawn_blocking](https://docs.rs/tokio/1.53.1/tokio/task/fn.spawn_blocking.html)。

`rusqlite 0.40.2` 被锁在独立 helper crate，尚未修改固定 Router 的原 `Cargo.toml`/lock。SQLite 官方说明 WAL 同机单写者约束及 checkpoint 尾部成本；`synchronous=FULL` 在 WAL 下为每次提交增加同步以保证断电耐久性。[WAL](https://www.sqlite.org/wal.html)、[synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)、[rusqlite transaction](https://docs.rs/rusqlite/0.40.2/rusqlite/struct.Connection.html#method.transaction_with_behavior)。

## 正确性与审查结果

Linux x86_64、Rust 1.99.0：helper release 测试 `47 passed`，`cargo clippy --all-targets -- -D warnings` 通过。目标 aarch64、Rust 1.99.0 的同一 lock/release 测试也是 `47 passed`；目标隔离工具链未安装 clippy component，因此静态检查只在 x86_64 执行，没有临时修改目标工具链。新增反例包括：

- commit 前失败与 commit 后返回不确定均不产生发送许可；
- 终态 commit 失败后当前进程暂停，重开仍保留旧负债；
- 恢复默认暂停、实例不一致拒绝、旧 owner 写入被 CAS 围栏；
- 数据库缺失不按空账本启动，重复初始化、内容损坏、快照超预算均拒绝；
- 终态序号/身份/未知 attempt 冲突不推进 cursor。
- observer/source 身份变化在结算前被拒绝且保留负债；取消正在等待的准入或终态存储提交会停用旧 runtime，后台提交完成后也不能继续签发 attempt。

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
| 目标 aarch64 项目本地盘 | 0 | 1000 | 673 µs | 2196 µs | 3667 µs | 14547 µs |
| 目标 aarch64 项目本地盘 | 64 | 1000 | 809 µs | 2482 µs | 3783 µs | 13015 µs |
| 目标 aarch64 项目本地盘 | 512 | 1000 | 2320 µs | 3474 µs | 4683 µs | 15766 µs |

判断：完整快照方案的正确性边界清晰，但每请求一次 FULL commit 的 P50 已是约 0.6 ms（x86 ext4）；在目标 aarch64 上随 512 条 active 增至 2.32 ms。它保留为正确性基线和小规模回退候选，不作为默认生产热路径。下一步比较规范化增量表与有界 group commit；任何分组都必须保持“该请求提交完成后才返回其许可”，且不能把排队时间排除在开销统计之外。

目标服务器还用固定 `ced6857`/`b47795c`、两个 Qwen2.5-14B TP1 重新运行既有公开链：实际请求目标和后端 `cached_tokens` 为 `(0,0)/(0,384)/(1,384)`，取消得到真实 `FINISHED_ABORTED` 后最终 active 为 0，Router 与两个服务均正常退出、无强制进程组清理。当前 wheel SHA-256 为 `6f610c42e44e48a7d702cdd7d5f6a271bbc6d2e2bd7323d27ed69165a4fbbf08`；报告/Router 日志 SHA-256 分别为 `ba9be9c007d67c9cefd90471cc180971ec4f7b83e5a31cd678f041c6b69aa059` 和 `1d00f98d89af75d2e30591a0a1a5a9200dc0431f2482f09562fc2b7a3e448303`。前置尝试依次暴露了服务 venv 缺少 MOD entry-point 元数据、误选旧 `13f11da` venv 被 profile 守卫拒绝、IPC 绝对路径超过 100 bytes；失败报告均保留，未放宽版本或路径约束。该结果只证明本次代码/环境整理没有破坏既有有限链；新持久层没有装进这个 Router 二进制，因此不构成持久恢复实机通过。

## 尚未解决

1. 真实 HUST terminal source 的 ACK/retire/GC 还没有接到这个 cursor；core seen 和 frontend family/child 的长期上限未解除。
2. 恢复时只核对显式传入的相同实例信息，尚未实现持续控制新鲜度到期、实际 identity endpoint 刷新及旧 dispatcher 的自动停止证明。
3. 当前终态批次拒绝未知 attempt；共享同一后端的非本 Router 请求如何用受信命名空间安全跳过尚未确定。
4. 尚未做进程 `SIGKILL` 各截断点、aarch64 编译/文件系统开销、512 family/1024 child 持续服务和真实双副本恢复。

因此 MOD 继续保持 `import_only`，真实公开链成绩和容量结论不变。
