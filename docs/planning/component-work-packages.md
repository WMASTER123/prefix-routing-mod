# 三人工作包与接口冻结条件

更新：2026-10-17。完成情况以 [progress](../progress.md) 为准，旧讨论保存在 [历史记录](component-work-packages-history.md)。本页给出职责和准入条件，尚未指定人员；实际端口、线程/错误边界及文件协调规则见[接手指南](../项目接手指南_2026-10-17.md)。

## 已确定的实现形态

一个 Rust Router 进程承载原 HTTP/SSE、M1 特征、M2 原索引、M3 状态与准入、M4 选点。固定宿主为 `vllm-project/router@91b65ee`；推理后端为 HUST Core `ced6857` / Ascend `b47795c`。它们是分别验证的仓库，不根据上游版本号互相推断兼容性。

Python 包负责 MOD 描述、doctor、版本映射和可选后端适配。M6 评测在请求链外；M8 在基础开销与收益明确后再考虑。MOD 当前保持 `import_only`。

```text
HTTP → M1 → M4 → M3 reserve → M5 原宿主发送/SSE
            ↑       ↑                   ↓
         M2 目录   M3 状态 ← 可靠后端终态
            ↑
       M7 事件/实例身份/控制租约
```

原索引、Directory/Pool 与本地适配只保留一个 owner。请求派发不能产生 KV 驻留事实；HTTP 结束不能直接释放已发送负债。

## 工作包与代码候选

| 包 | 独占职责 | 当前候选位置 | 可先准备的独立任务 |
|---|---|---|---|
| A：M7/M2 | 版本/源身份、事件接收、恢复/失效、目录预算和查询 | `src/prefix_routing_mod/adapters/`；`experiments/router-extension/native_kv_*.rs`、`runtime_cache_port.rs`；`experiments/router-index/src/` | 固定协议 fixture、真实 IPC 故障、父链与全池资源测试 |
| B：M1/M4/M5 | 一次本地分词、纯选点、原 HTTP/SSE、发送门与传输观察 | `request_feature_port.rs`、`runtime_provider.rs`、`prefix_extension.rs` 和三文件宿主 patch | M1 输入/身份反例、策略纯函数、慢客户端/取消与默认关闭测试 |
| C：M3/M6/交付 | 唯一账本、持久提交/游标、重启对账、统一评测和 MOD 工具 | `runtime_owner.rs`、`durable_runtime.rs`、`runtime_store.rs`（SQLite 仍为候选）；`tools/`、`.github/workflows/`、Python MOD 包 | aarch64 存储对照、真实 ACK/GC、强制退出恢复、负载与制品检查 |

上述位置是候选实现，生产目录在集成验收后统一迁入；禁止每包另建代理、索引或账本。共享类型由集成负责人维护，契约变更与调用方和反例同 PR 更新。每包自测，另一成员交叉审查；C 不承担全队唯一测试职责。受当前端口约束的独立任务现在即可指定人员开展，不必等生产 ABI 全部冻结；跨包恢复/回收协议不能各自定稿。

## 正式分工的集成门槛

- 固定宿主真实公开入口同时使用 Qwen M1、M7→M2、M3 准入、M4 基础选点、M5 转发与真实终态；有干净环境启动命令和两个 TP1 配置。
- 记录每个端口的实际位置、调用线程、时钟、错误/超时、降级、配置发布和启停所有权；替身与正反例可独立运行。
- 真实请求链覆盖正常、重复、取消、断连、目录缺口、配置竞争与 Router 重启；未决负债保留，旧实例不能影响新实例。
- M3 持久介质和提交边界明确；终态 ACK/回收不能早于持久提交。超过早期 64 family/256 child 的持续运行有证据，资源满额行为明确。
- 基础开销检查排除重复分词、逐请求跨进程目录查询及明显锁/持久化瓶颈。正式容量和高级算法可以在分工后继续完成。
- 独立审查通过后，记录接口版本、生产目录和交付方式，再正式指定 A/B/C 人员。独立准备任务可提前进行，但不能把候选类型当冻结 ABI。

## 当前尚差的接线

原生 IPC/控制和固定完整宿主测试已通过；真实 M1、Rust M7/M2、M3/M4/M5 与核心终态已在同一公开链完成有限实机验证，包括生成后取消。M3 增量 SQLite、持久提交/cursor/暂停恢复候选已经过 CPU 故障测试，本轮显式装入私有宿主做真实 ACK 资格；仍须完成长期回收、健康新鲜度和强制退出恢复，见[计划](durable-runtime-and-retention.md)。这些通过前不宣布生产接口冻结；具体成绩以 progress 和对应 evidence 为准。

Linux CPU 负责传输和故障回归；1 张 NPU 做单副本真实联调，2 张做双 TP1。三人使用独立分支/工作区，由集成提交统一同步服务器，不同时修改远端同一 checkout。
