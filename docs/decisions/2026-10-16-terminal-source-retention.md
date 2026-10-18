# ADR：终态源回收与受信请求归属

日期：2026-10-16，2026-10-18 更新。状态：首版部署边界已选择；Core ACK 回收、Frontend family 退役、真正响应丢失、520 family/1040 child 资源资格及 Router 强退恢复/实例替换均已通过。`recovery-retirement-v1` 语义合同冻结；多日稳定性、Manager 和性能不在本 ADR 的通过范围。

## 问题与证据

真实终态适配有两层留存。Core 层保存核心清理记录及未确认窗口内的重复保护；Frontend 层保存一个 Router attempt 到全部真实 internal child 的完整映射。两层都不能用 TTL 清理未决工作，也不能只因 HTTP 结束而释放 M3 负债。

本轮直接检查固定 HUST 档案 `hust-cedb477-native-v1` 中受 SHA-256 守卫的源码，而不是套用上游版本说明：

- `InputProcessor.assign_request_id` 将外部 ID 保存为 `external_req_id`，默认在内部 ID 后增加 8 位随机片段；
- `ParentRequest.get_child_info` 用 `index_internal-id` 生成并行采样 child；
- 可选 adapter 包装 `Scheduler._free_request`：固定原方法成功返回后才调用 observer。已验的无 connector、TP1 范围中，原方法完成核心请求表清理；正常完成、停止字符串和取消均由此前固定源码及真实 NPU 测试覆盖。不能把这个顺序外推到未验 connector 配置；
- HTTP 的 `X-Request-Id` 仍可由客户端提供。字符串前缀 `prm-` 只能做格式筛选，不能证明请求由 Router 分配，也不能证明攻击者/旁路客户端不会伪造。

因此，Core 端可以在唯一 Frontend ACK 某一序号后同时删除该条记录和重复保护，使 `limit` 约束未确认窗口而非进程累计请求数。Frontend family 只有在 durable M3 已提交同一终态 cursor，且请求归属得到可信证明后，才可退役。

## 已决定并实现

1. Core source 仍为单消费者、严格连续 cursor；ACK 前保留记录和重复保护，ACK 后按同一记录精确回收。
2. ACK 后若未来出现相同 internal ID，按新请求处理。固定 HUST 一次性清理路径由源码 hash 守卫；这不是允许同一未确认请求重复发终态。
3. Core 身份、PID、epoch、完整批校验和失败关闭语义不变；不修改 HUST scheduler、分配、淘汰或 NPU 执行。
4. 默认 `observe_only` 下，Frontend 的 `TerminalJournal.retire` 继续拒绝外部/未证明归属的 family。不能为了跑过长测而把所有 `prm-` 请求直接标成 managed。

## 首版选择：专属后端池与显式独占入口

首版只管理明确加入本 MOD 的副本池，业务请求必须经固定 Router；HUST HTTP 后端只绑定 `127.0.0.1`，不对外暴露。Router 覆盖客户端提供的全部 `X-Request-Id`，为每个 Router 进程使用 128 位随机命名空间和进程内单调 attempt 序号。适配器仍默认 `observe_only`；只有运维显式设置 `PREFIX_ROUTING_TERMINAL_OWNERSHIP=exclusive_router_v1`，且既有 loopback、TP1/DP1/eager、固定源码守卫全部成立时，捕获的 family 才标记为可退役。其他模型服务和未加入本池的流量不受影响。

这不是“前缀即鉴权”。首版信任边界是专属副本、回环监听、同主机可信进程以及 Router 覆盖请求头的组合；在同一主机存在不可信进程或必须允许旁路客户端时，此模式不成立，必须保持 `observe_only`，之后再增加逐请求 proof/MAC 和重放状态。首版不宣称支持共享后端直连共存。

采用该边界的原因是：当前目标是易用的工程首版，额外逐请求 MAC、密钥轮换和服务端 nonce 状态会进入热路径及恢复协议，但并不能单独防重放。先把专属池正确性、长期资源和性能做实；需要旁路共存时再以独立能力扩展，而不是让默认路径承担未验证复杂度。

## 已实现的退役候选

新增版本化控制路径 `/prefix-routing/terminal/retire`，仍复用既有私有控制 token。顺序固定为：M3 原子提交终态与 consumer cursor；读取接口以同一 cursor ACK；退役调用携带精确 observer epoch、`through` 与相等的 `checkpointed_through`；Frontend 仅删除该水位内、完整终态且受管的 family/child/status。任何外部 family、binding 不符、cursor 回退、未 ACK 或持久水位不等均拒绝，未知/未决 family 不参与回收。

身份响应新增 `reserved_children/acknowledged/retired_through/ownership_mode` 后，固定 Rust `HustSourceControl` 必须显式解码并校验这些字段；它仍使用 `deny_unknown_fields`，不以忽略未知字段换取表面兼容。计数必须满足 `retired_through <= acknowledged <= frontend_boundary`，资源上限分别为 64 family/256 child，ownership mode 在同一实例内不可改变。该调用方同步修改是接口变更的一部分。

退役后 `retired_through` 以前的 cursor 不再能请求“完整快照”，必须失败关闭并从持久 consumer cursor 恢复。重复退役调用是幂等的：已达到同一水位时可返回 `retired=0`，不能把不确定响应误判为数据丢失。CPU 已覆盖累计 600 个 family 的小窗口循环、重复/旧 cursor 和 observe-only 拒绝；这只证明状态机没有 64 次生命周期上限，不代替真实 NPU、进程崩溃或内存趋势资格。

## 后续共享后端方案

静态派生 proof 不能作为最终方案：它是可重复使用的 bearer credential，既不绑定单个 attempt，也不能独立阻止抓包重放；此前“64 位 proof”的表述还把 64 个十六进制字符错误写成了 64 bit，现予撤回。

若以后要求同一 HUST 后端同时接受 Router 与旁路客户端，候选是每个控制会话派生独立密钥，并对不可复用的 attempt ID、worker/instance/observer 会话身份和固定协议域计算逐请求 MAC；Router 覆盖客户端的 request/proof 头，adapter 常量时间验证后才创建 managed family。它仍必须配合服务端重放状态或严格的会话/序号退役边界，不能只靠 MAC 宣称防重放；HTTP Message Signatures 的安全说明同样要求 nonce/timestamp 等重放缓解，而不是把签名本身当成防重放机制：[RFC 9421 §7.2.2](https://www.rfc-editor.org/rfc/rfc9421.html#section-7.2.2)。该扩展不是首版依赖，未实现。

## 资格结论与仍未覆盖范围

双副本 Rust 公开链的真实 completion/cancel、commit→ACK→retire、真正退役回复丢失、520 family/1040 child、旧 cursor、Router 强退暂停恢复和实际控制实例替换均已通过，详见[资格记录](../../evidence/recovery-retirement-qualification-2026-10-18.md)。因此 `recovery-retirement-v1` 语义合同可以冻结。

仍未覆盖持续控制 token 轮换/健康过期、跨主机旧 dispatcher 围栏、共享后端业务直连、任意已热/超窗 KV 恢复、多日稳定性和性能。MOD 继续 `import_only`；冻结本合同不等于发布整个生产 ABI 或宣称容量能力。
