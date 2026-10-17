# 关联、恢复与 wire：当前候选接线与生产退出条件

2026-10-13 本次更新：HTTP serving 的多 prompt 总关联已在固定源码方法执行中补齐，包含普通 AsyncLLM 分支和实际 ID；renderer/IPC 为替身，尚非网络端到端或已安装 hook。动态按请求 reserve 与接收旧观察共用一个 CPU 账本 owner；新 runtime 的显式交接保留旧负债，旧许可不能再发送。异步准入的宿主改动仅原 reserve 调用加 await，继续在实际 send 前检查 registry/control fence。剩余是 Router 自有身份 carrier 到真实 HUST 适配器安装、可信服务身份/启动排他与现场观测、终态组合及真实 KV 订阅；不能把 CPU takeover 调用当完整生产恢复。下方为历史过程，最新范围见[ADR](../decisions/2026-10-13-entry-recovery.md)。

2026-10-13 更新：M5 原生 trace 已增加独立控制线程与有限 loopback 投递端，M3 接收观察后提交/确认并独立编号；真实 socket 与接收进程重建已回归。完整宿主结果见[本轮证据](../../evidence/online-transport-2026-10-13.md)。该通道只覆盖传输观察；真实 HTTP 总关联、动态预留、后端终态观察和启动时钟交接仍未完成。Python 接收端仅是实验，不增加生产进程。下表保留 2026-10-12 原始候选边界。

日期：2026-10-12。延续用户批准的小范围 CPU 评估，工具不安装到引擎，MOD 保持 import_only；生产组件尚未完整验收。

| 边界 | 本轮具体代码/语义 | 下一道实际门槛 |
|---|---|---|
| M5/M7 请求关联 | journal begin/bind/seal/abandon；预留完整 child 额度，实际 ID 不靠字符串猜测；原生 AsyncLLM n=1/3 和失败/取消方法驱动 | 普通 renderer/HTTP/chat/多 prompt 到完整总组数、实际前端→核心绑定通道 |
| M3/M5 生命周期 | DurableLedger 复用 ReferenceLedger；提交后返回成功、统一序号、重复/迟到收敛、有界留存 | 接入原生 Router 回调/控制线程和鉴权；确定业务结果与逻辑结束的组合，不能用通用 ended 冒充成功 |
| M3 恢复 | 实际独立进程强制退出后从 SQLite 元数据记录重建；已提交终态重复幂等，未提交保持负债 | 新 Router 单调时钟与服务新鲜度交接、旧实例/observer 历史丢失对账、明确 GC/去重窗口 |
| M7→M2 数据 | 解码前 1 MiB 限额、原生 codec tagged map 规范化为 opaque anchor/local digest；不输出 tokens；新目录继续使用原 KvBlockIndexer | 真实四帧订阅、控制回复/epoch/身份核验、完整 cut 与缓冲/重复窗口，目标设备事件语义 |
| M2 trust/额度 | Pool 以恢复 ticket 防迟到结果；请求发送时点计算控制 lease；注册额度之和受全池上限约束 | 分区替换/注销、并发 owner/read 接入及实际 RSS/重建开销；额度是逻辑预算，不能当 RSS 硬限制 |

## 当前原型的精确范围

- 固定源 `13f11da...` 七文件 guard 后执行完整 ID 分配、ParentRequest 和 AsyncLLM.add_request/_add_request、OutputProcessor.add_request/_finish_request。请求/参数/IPC/collector 是替身；当前走直接 EngineCoreRequest 分支，没有运行普通 renderer、真正 tokenizer 或完整 HTTP 服务。
- first child 结束时不 seal；全部提交成功才 seal。中途发送失败或 asyncio 任务取消保留未决。一个 AsyncLLM family 不等于多 prompt HTTP 总 family，真实入口还须确定总组数与关联封口。
- 接收侧保存 request/attempt/worker、domain、控制版本和 lifecycle 元数据，省略 prompt/token 序列。重放只验证本实验按请求数计费的账本，不能推出输入 token 工作量的生产持久化已经完备。
- SQLite `synchronous=FULL` 的提交是 receipt 边界；新对象恢复后 `admission_ready=false`，不能凭历史健康/时钟开始新发送。实验提供按已知可靠终态继续对账，跨进程真实时间交接仍未实现。
- 原生 codec 的合成事件在现有隔离服务 venv 的 msgspec 0.22.0 中编码；只抽取 KV Struct 定义，不导入 vllm/torch。JSON 文件为离线实验桥，不是生产每请求序列化方案。
- M7 stateless normalizer不自行建立可信目录；生产还须管理连接/去重/握手与完整恢复。Rust pool 必须拿当前 ticket 安装/queue/live/confirm，迟到任务不影响较新视图。control started_ns 是 Router 发送请求的时点，不能用后端时间或迟到回复到达时点续期。

## 实验结果引起的计划调整

20 个合成 family 的逐请求三次 FULL 事务：中位约 11.31 ms，p95 约 13.09 ms；批量候选把相同 60 条操作用三个显式事务完成，共约 28.85 ms，摊销 1.44 ms/family。两者不是同一种到达/等待模型，不能把摊销值当单请求延迟或直接报倍数提速。

因此生产不直接选逐请求同步 SQLite。先保持“预记账确认早于发送、提交后才能 ACK、故障不清未知”语义，再比较有限 group commit 的等待、队列和持续容量；也可以评估可靠后端对账支持的恢复方案。当前没有 batching timer/常驻线程，批量上限 64 仅作 CPU 边界，不是生产调参。存储语言/制品和协议不据此冻结。

留存满均拒绝新记录，GC 没有实现。observer 重建丢历史、Router 日志丢失或 schema/序号损坏不能自动宣布“无在途”。原生 HUST/Router 源码保持固定且不改动，额外 serde 只在实验 crate；原宿主 lock 不动。

## 下一步顺序

1. 收敛真实 HTTP 总关联、业务结果/逻辑终态组合、可逆观测与受控控制通道；提交具体安装范围和关闭行为后再做引擎接入。
2. 以原生 Router 实际 Lease 回调接同一序号/接收 owner，覆盖真实断连、ACK 丢失、过载与重启；确定长时间 GC 与启动恢复。
3. 将真实 opt-in KV 订阅/恢复接到已验证规范化/目录端口，完成全池生命周期与资源测量。
4. 首个真实后端阶段需要 1 张 NPU、公共 Qwen2.5-14B-Instruct；随后两个 TP=1 副本用 2 张。需要时提前反馈。关键接缝通过后冻结首版生产目录/接口，再分工。

本轮完成的是 CPU 候选接线和反例验证；真实 NPU 终态、完整服务订阅、插件安装/启停、性能收益和生产接口冻结仍待验收。
