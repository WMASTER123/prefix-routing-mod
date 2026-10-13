# 原生 Router 生命周期在线接收：CPU 证据

日期：2026-10-13。基于 c3b1736，分支 codex/online-lifecycle-channel。本轮不修改 HUST 或 MOD 入口，不运行 NPU；原宿主三文件 patch 与锁文件不变。

## 已验证范围

- Windows CPU 402 pass / 4 平台 skips；Linux aarch64 CPU 406 pass；两端模拟 integration 各 3 pass。这些是本轮最终 Python 内容的检查，不是完整 NPU 或性能验收。
- 新增 26 项 Python 回归：真实 loopback/socket、独立接收进程终止和重建、重复/冲突观察、确认丢失、存储故障、留存超限、鉴权、帧上限、绝对读期限、预留 fixture 和验证失败不得返回 pass。
- 原生 helper 17 项通过，其中新增在线通道 7 项；两端本轮 sdist→wheel/源码目录外干净安装通过，MOD 保持 import_only。
- 继续复用 ReferenceLedger 和现有事务日志。源观察与生成的账本转换同事务提交；逻辑终态已结清后，迟到的传输关闭是 receipt-only，不重新计费。不从 HTTP EOF 推断 backend ENDED。
- 接收进程重启后可以接收旧发送观察，但 admission_ready 仍为 false；接收历史事实不构成新的发送许可。真实派发时钟交接和服务重新观测尚未完成。

完整 aarch64 release 在重新解包的固定归档上构建；原三文件 patch 不变。最终 admission example SHA256 为 `007b2cf8a2f6d3045d6661cb3cce8548e429e8e85705e3fa4ad9a21a1b87d9f0`。实际普通 HTTP/SSE/取消/并发/故障与关闭回归通过：

| 情形 | 实际结果 |
|---|---|
| 在线正常投递 | 24 尝试、46 观察，46 次确认均在 Router 存活期间；22 uncertain、2 not_sent；40 未使用 fixture 预留仍在，共 62 在途 |
| 提交后丢第一次 ACK | 第一次提交成功后丢连接，重投原载荷；3 次确认尝试、2 个唯一观察、1 次注入失败；1 uncertain、63 未使用预留；新请求返回 503 且后端无重复发送 |
| 关闭在线选项 | 原 24 尝试/46 观察与转发计数回归通过；不启动控制线程，不继承实验 token/端口/故障环境变量 |
| 原有 stream example | 10 类行为/8 次 backend POST 通过；原生两种负载分支、drop/idle/大小限额不退化 |

报告位于服务器 `artifacts/online-transport-20261013/http-online-final`、`http-ack-loss-01`、`http-disabled-final`、`http-stream-01`。无残留自有 fixture 进程；所有清理均为正常退出。指标为功能断言，不是延迟或吞吐实验。

最终 helper 的运行时代码与宿主实际编译输入逐字节核对一致；后加的 bounded accept 只在 `#[cfg(test)]` 测试模块，独立 checks crate 已重编译验证。两个 example/trace 与编译输入一致。宿主 lock 保持 `8675caa4...`；HUST 固定 `13f11da...` 工作树干净。

初始 e1ee5a3 的 CI 已核验：Python 3.11/3.12 [运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38348104195)、原生 helper [运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38348104184) 均 success。后续故障/审查增量的 CI 按确切提交另外核验，不继承初始成绩。

最终代码提交 `ce36e48bf884218320351000f1ea9b66751c73c2` 已单独核验：Python 3.11/3.12 [运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38348885779)、原生 helper [运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38348885827) 均 completed/success，headSha 与代码一致。本地、GitHub、服务器已同步该提交；服务器本轮暂存源码逐文件对照一致后保存在可恢复 stash，再 fast-forward，未覆盖不明修改。

## 失败与自审

首次单测中 socketpair 返回 tuple，不能整体作为 context manager；已改为分别管理两个 socket，四项失败保留在本轮命令记录，复测通过。

第一次宿主联调在启动 Router 前被既有契约拒绝：fixture 的 local_outstanding 来源误填 fixture。已修为 local_ledger/router_session，并把未观测 backend_running 保持 None；补完整预留 fixture 回归。该次没有执行新 Router，不能计入转发成绩。

自审补齐：单 outbox 只允许一个控制线程；控制线程退出后锁住新派发；读写整体期限不能被零碎数据延长；超大 ACK 在分配前拒绝；原生桥保留错误 ACK 的记录；关闭在线选项时剔除继承的实验环境变量。

自审发现探测工具在在线结果验证前已把状态设为 pass，后续异常可能保留错误状态；已改为所有验证和上下文关闭成功后才写 pass，异常显式写 fail，新增反例回归。记录旧成绩时不得自动覆盖此修正。一次误用不存在的文档测试文件名未执行测试，随后正确的 `tests/test_documentation.py` 两项通过。

## 实验限制与维护成本

Python 接收器是跨语言/故障实验，不是生产新增服务。Rust 控制线程轮询有限槽位，注册最多 128 尝试、每尝试最多 2 条；没有 GC 或无锁延迟保证。回调仅更新本地 trace，网络/数据库在回调之外。收到通道故障后锁住新派发，重连重投不自动解锁；故障检测前的窗口依靠预留负债保守覆盖。

宿主 fixture 将 64 条合成预留在启动前持久化，不能据此声称动态 HTTP 预记账已经联通。未使用预留保留并单独统计。预留创建、接收器、Router 仍是受控 CPU 实验，真实 HUST 关联、业务结果、终态观测安装与 KV 服务订阅没有在本轮验收。

未选择生产持久化或新增性能机制；没有测本通道 SLO/可持续容量，没有将异步投递当成 SQLite 性能问题解决。下一步需收敛动态准入与启动恢复，接真实后端时先申请 1 张 NPU；双 TP=1 后续 2 张。

自审完成；[草稿 PR #15](https://github.com/WMASTER123/prefix-routing-mod/pull/15) 基于 PR #14，另一成员独立审查待完成，不自动合并。

复现新增宿主故障入口：`python -m tools.router_extension_probe --binary <本轮构建的 example> --output <新 artifacts 目录> --online --lose-first-ack`。单测和制品继续使用统一检查入口；Rust 使用项目私有 1.99.0 工具链，`cargo test --offline --locked --manifest-path experiments/router-extension/checks/Cargo.toml`。
