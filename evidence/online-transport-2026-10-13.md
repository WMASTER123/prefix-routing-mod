# 原生 Router 生命周期在线接收：CPU 证据

日期：2026-10-13。基于 c3b1736，分支 codex/online-lifecycle-channel。本轮不修改 HUST 或 MOD 入口，不运行 NPU；原宿主三文件 patch 与锁文件不变。

## 已验证范围

- Windows CPU 401 pass / 4 平台 skips；Linux aarch64 CPU 405 pass；两端模拟 integration 各 3 pass。这些是本轮 Python 内容的检查，不是完整 NPU 或性能验收。
- 新增 25 项 Python 回归：真实 loopback/socket、独立接收进程终止和重建、重复/冲突观察、确认丢失、存储故障、留存超限、鉴权、帧上限、绝对读期限、预留 fixture。
- 继续复用 ReferenceLedger 和现有事务日志。源观察与生成的账本转换同事务提交；逻辑终态已结清后，迟到的传输关闭是 receipt-only，不重新计费。不从 HTTP EOF 推断 backend ENDED。
- 接收进程重启后可以接收旧发送观察，但 admission_ready 仍为 false；接收历史事实不构成新的发送许可。真实派发时钟交接和服务重新观测尚未完成。

完整宿主构建、最终原生 helper 检查和新提交 CI 在执行完成后记录，不能继承旧分支成绩。

## 失败与自审

首次单测中 socketpair 返回 tuple，不能整体作为 context manager；已改为分别管理两个 socket，四项失败保留在本轮命令记录，复测通过。

第一次宿主联调在启动 Router 前被既有契约拒绝：fixture 的 local_outstanding 来源误填 fixture。已修为 local_ledger/router_session，并把未观测 backend_running 保持 None；补完整预留 fixture 回归。该次没有执行新 Router，不能计入转发成绩。

自审补齐：单 outbox 只允许一个控制线程；控制线程退出后锁住新派发；读写整体期限不能被零碎数据延长；超大 ACK 在分配前拒绝；原生桥保留错误 ACK 的记录；关闭在线选项时剔除继承的实验环境变量。

## 实验限制与维护成本

Python 接收器是跨语言/故障实验，不是生产新增服务。Rust 控制线程轮询有限槽位，注册最多 128 尝试、每尝试最多 2 条；没有 GC 或无锁延迟保证。回调仅更新本地 trace，网络/数据库在回调之外。收到通道故障后锁住新派发，重连重投不自动解锁；故障检测前的窗口依靠预留负债保守覆盖。

宿主 fixture 将 64 条合成预留在启动前持久化，不能据此声称动态 HTTP 预记账已经联通。未使用预留保留并单独统计。预留创建、接收器、Router 仍是受控 CPU 实验，真实 HUST 关联、业务结果、终态观测安装与 KV 服务订阅没有在本轮验收。

未选择生产持久化或新增性能机制；没有测本通道 SLO/可持续容量，没有将异步投递当成 SQLite 性能问题解决。下一步需收敛动态准入与启动恢复，接真实后端时先申请 1 张 NPU；双 TP=1 后续 2 张。

自审进行中；另一成员独立审查待完成，不自动合并。
