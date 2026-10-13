# 入口关联与启动恢复的 CPU 接缝

状态：CPU 实现与宿主验证中；不冻结生产 ABI，不安装引擎 hook。

本轮先补一个 HTTP attempt 对全部 prompt × sampling child 的关联，再补按请求准入及恢复。复用固定 HUST 的入口、ID 分配和 fanout 方法，不复制其 ID 生成规则。标准文本 completion/chat 为范围；beam search、流式输入、多模态、动态采样分叉不以本次证据宣称支持。

关联在 renderer 给出完整输入集合后预留总 child 额度；generate 调用登记实际 external ID，OutputProcessor.add_request 前登记真实 internal ID，每组 add_request 成功返回后标记提交完成。所有组完成且所有 child 已知后才 seal。取消、部分提交失败和未被消费的 generator 都不能提前结清。复用 X-Request-Id 的前提是 Router 覆写不可信客户端值且后端入口受控；不能把外部任意 header 当授权。

恢复必须将“每次 attempt 的旧时钟”与“当前 worker 观测时钟”分离。持久化新 runtime 及新观测，旧 attempt 仍保留原时钟和负债；不把旧时间戳当新时钟读数。仅允许同一已确认 backend instance 的恢复，实例替换另走终止/对账。CPU 接缝要求操作者明确确认旧 dispatcher 已停；这不是自动探活、分布式租约或多 Router fencing。重开日志本身不开放派发。

可靠 ENDED 新接收确认可以使用当前 runtime 时钟，只有同一时钟下才比较时间先后。原发送所属时钟仍保留用于拒绝旧发送命令；不能把 receipt 时间与后端执行时间混为一谈。普通准入拒绝不应使已接收工作失去对账通道；事务/序列一致性错误仍关闭派发。

范围成本：原有三个 Router 宿主文件不变更集合；reserve 调用由同步改为 await，另在同一 send_typed_request 分支增加自有 header 的校验/覆盖。Lease 默认不提供 carrier，reserve_async 默认委托旧 reserve。新增 admission_bridge 只供 CPU 实验：最多八个阻塞 RPC job，连接 200 ms、交换总期限 2 s，取消不放大队列、不启动后端发送。未做性能验收，不能把这个网络/存储方案直接作为生产热路径。

动态准入必须先提交再发出许可，旧许可、重复申请、过期观测及不足额度不能再次发送。异步调用不得在 Router 同步回调里阻塞 fsync/RPC；现有固定宿主需要可取消的 await 接缝，其范围和维护成本须单独记录。SQLite、Python 和 loopback 都仍为 CPU 实验，不引入第二个生产账本服务。

真实 KV 订阅排在上述接线之后，不因规范化帧测试通过就宣称真实订阅或 NPU 验收完成。
