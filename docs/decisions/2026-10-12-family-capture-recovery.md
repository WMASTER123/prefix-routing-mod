# ADR：关联封口与可恢复接收的 CPU 接线

状态：延续用户批准的终态 CPU 评估；不安装引擎适配。日期：2026-10-12。

固定 AsyncLLM 按顺序 await 提交 n 个 child。已提交 child 可以在整个提交循环完成前结束，所以只有“当前已知 child 全部结束”不够：必须先取得期望数量，并在整个提交成功后封口。部分提交失败或任务取消保留未决状态。关联使用实际 request.external_req_id、request.request_id、parent.n 与 index；不解析随机 ID 或 child 前缀。

本轮复用既有有限 journal，增加 begin/bind/seal/abandon 的实验语义，保持旧完整 register 可用；固定源码完整 add_request/ID 分配/parent 方法驱动联调。多 prompt 的 HTTP 总组数与真实跨进程绑定仍须独立核验，不能由单个 AsyncLLM family 推断。

恢复实验使用 Python 标准库 SQLite 事务与既有 ReferenceLedger：持久接收“预记账/生命周期”元数据，事务提交后才返回成功；新进程从日志重建参考账本并保持发送后负债。不持久化 prompt 或 token 序列。SQLite 和 Python 仅验证原子接收/重放语义，生产存储与语言未冻结。设备/服务观测未重新确认时禁止恢复后直接新派发；不跨进程继承单调时钟新鲜度。

要求测试提交前后崩溃、丢 ACK、重复、冲突、限额、损坏拒绝和重新建立观测前的派发隔离。记录有界，满额拒绝；长期 GC 与真实 M5 控制线程接入后续验收。若实际安装需要扩大修改范围，按现有否决线讨论。
