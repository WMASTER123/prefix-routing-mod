# ADR：终态源回收与受信请求归属

日期：2026-10-16。状态：Core ACK 回收已实现并通过 CPU 回归；Router 请求归属方案待选择，Frontend family 退役尚未启用。

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
4. Frontend 的现有 `TerminalJournal.retire` 继续拒绝外部/未证明归属的 family。不能为了跑过长测而把所有 `prm-` 请求直接标成 managed。

## 待选择的 Router 归属方案

静态派生 proof 不能作为最终方案：它是可重复使用的 bearer credential，既不绑定单个 attempt，也不能独立阻止抓包重放；此前“64 位 proof”的表述还把 64 个十六进制字符错误写成了 64 bit，现予撤回。

当前候选是每个控制会话派生独立密钥，并对不可复用的 attempt ID、worker/instance/observer 会话身份和固定协议域计算逐请求 MAC；Router 覆盖客户端的 request/proof 头，adapter 常量时间验证后才创建 managed family。它仍必须配合服务端重放状态或严格的会话/序号退役边界，不能只靠 MAC 宣称防重放；HTTP Message Signatures 的安全说明同样要求 nonce/timestamp 等重放缓解，而不是把签名本身当成防重放机制：[RFC 9421 §7.2.2](https://www.rfc-editor.org/rfc/rfc9421.html#section-7.2.2)。受控网络与后端只允许 Router 访问可作为首版部署前提，但不能替代协议检查。新增独立 attempt secret 隔离更强，却增加配置、轮换和恢复状态；最终选择须先做协议反例与运维成本评审，再写代码。

## 退役接口的后续边界

方案确认后才增加版本化 retire 控制调用。调用必须携带准确 observer binding、`through == durable checkpointed_through`，先由 M3 原子提交终态与 cursor，再 ACK，再退役 completed managed family，并同步回收 child/status。低于 `retired_through` 的恢复必须带持久 cursor，否则暂停并报告重放窗口耗尽。任何不确定结果保留负债并关闭增强准入。

验收至少覆盖：伪造前缀、客户端注入 proof、重复/乱序 ACK、丢 ACK、退役调用不确定、token 轮换、实例替换、超过 512 family/1024 child 的稳态资源，以及旧 cursor 重启。该 ADR 不把待选 proof 或 retire 接口写成已交付能力。
