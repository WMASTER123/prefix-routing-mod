# ADR：终态源回收与受信请求归属

日期：2026-10-16。状态：Core ACK 回收已实现并通过 CPU 回归；Router 请求归属方案待选择，Frontend family 退役尚未启用。

## 问题与证据

真实终态适配有两层留存。Core 层保存核心清理记录及未确认窗口内的重复保护；Frontend 层保存一个 Router attempt 到全部真实 internal child 的完整映射。两层都不能用 TTL 清理未决工作，也不能只因 HTTP 结束而释放 M3 负债。

本轮直接检查固定 HUST 档案 `hust-cedb477-native-v1` 中受 SHA-256 守卫的源码，而不是套用上游版本说明：

- `InputProcessor.assign_request_id` 将外部 ID 保存为 `external_req_id`，默认在内部 ID 后增加 8 位随机片段；
- `ParentRequest.get_child_info` 用 `index_internal-id` 生成并行采样 child；
- `Scheduler._free_request` 在清理路径调用 observer 后从 scheduler 请求表删除该 internal ID；正常完成、停止字符串和取消均已由此前固定源码及真实 NPU 测试覆盖；
- HTTP 的 `X-Request-Id` 仍可由客户端提供。字符串前缀 `prm-` 只能做格式筛选，不能证明请求由 Router 分配，也不能证明攻击者/旁路客户端不会伪造。

因此，Core 端可以在唯一 Frontend ACK 某一序号后同时删除该条记录和重复保护，使 `limit` 约束未确认窗口而非进程累计请求数。Frontend family 只有在 durable M3 已提交同一终态 cursor，且请求归属得到可信证明后，才可退役。

## 已决定并实现

1. Core source 仍为单消费者、严格连续 cursor；ACK 前保留记录和重复保护，ACK 后按同一记录精确回收。
2. ACK 后若未来出现相同 internal ID，按新请求处理。固定 HUST 一次性清理路径由源码 hash 守卫；这不是允许同一未确认请求重复发终态。
3. Core 身份、PID、epoch、完整批校验和失败关闭语义不变；不修改 HUST scheduler、分配、淘汰或 NPU 执行。
4. Frontend 的现有 `TerminalJournal.retire` 继续拒绝外部/未证明归属的 family。不能为了跑过长测而把所有 `prm-` 请求直接标成 managed。

## 待选择的 Router 归属方案

推荐方案是复用现有每后端 control token 派生一个单用途 proof：

- Router 根据 control token 和固定域标签派生 64 位 proof，只把 proof（不把 control token）写入一个专用请求头；同时覆盖客户端的 `X-Request-Id` 和 proof 头；
- HUST 默认关闭的固定版本 adapter 以常量时间比较派生 proof，验证成功才创建可退役的 managed family；前缀相同但 proof 缺失/错误必须拒绝进入 managed 链；
- proof 只表示“此 attempt 由当前 Router 控制域发出”，不表示后端接受、完成或 KV 命中；终态、durable cursor、ACK、retire 的顺序仍不可省略；
- 后端仍应只在受控网络暴露，proof 不写日志。token 轮换会产生新的控制会话，不能跨旧实例清债。

备选一是新增独立 attempt secret，隔离更强但增加部署配置与轮换状态；备选二是要求后端只能由 Router 独占访问，改动较少但限制企业部署，且网络边界配置错误会重新引入伪造风险。

## 退役接口的后续边界

方案确认后才增加版本化 retire 控制调用。调用必须携带准确 observer binding、`through == durable checkpointed_through`，先由 M3 原子提交终态与 cursor，再 ACK，再退役 completed managed family，并同步回收 child/status。低于 `retired_through` 的恢复必须带持久 cursor，否则暂停并报告重放窗口耗尽。任何不确定结果保留负债并关闭增强准入。

验收至少覆盖：伪造前缀、客户端注入 proof、重复/乱序 ACK、丢 ACK、退役调用不确定、token 轮换、实例替换、超过 512 family/1024 child 的稳态资源，以及旧 cursor 重启。该 ADR 不把待选 proof 或 retire 接口写成已交付能力。
