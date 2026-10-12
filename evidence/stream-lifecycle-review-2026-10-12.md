# 流式审查修复与有限标准生命周期：验证记录

日期：2026-10-12。分支：`codex/stream-review-lifecycle`，基于 `7c7c5fa`。当前 CPU 分配可访问、仓库基础提交一致且干净；工作仅在 `/root/work/prefix-routing-mod`。没有 NPU 实验或系统/HUST 源码修改；MOD 仍 import_only。原生共享 helper/标准事件与完整宿主验证分别记录。

## 上轮问题及复验

上轮 Windows CPU 首次复测：269 passed、1 failed、4 skipped。失败为大响应 fixture 未消费请求体就关闭连接导致偶发 TCP reset；100 次对照中原写法 94 次预期 ValueError、5 次 ConnectionAbortedError、1 次 ConnectionResetError，读完请求体后 100 次预期 ValueError。本轮修复 fixture，重复 100 次均为预期 ValueError，超限断言不放宽。

DirectStream 原实现在 Ready 空块路径既不重置 idle，也不检查到期。新实现过滤空块，检查单调 deadline/定时器，每次处理一个空块后唤醒并让出，避免持续空帧绕过限制或单次 poll 内忙等。已有非空数据优先交付；idle 在消费时检查，客户端完全停止消费及整体 HTTP 栈资源边界仍待验收。

从 `7c7c5fa` 导出未修改 helper，与新反例构建：`continuously_ready_empty_frames...` 失败、exit 101；新代码通过。反例使用暂停的 Tokio 时间，不依赖真实睡眠。原生共享 helper 10 项通过，覆盖空块、进度重置、慢消费者、大小限制、drop/error/EOF、标准事件与 ID 限制。测试 crate 精确直依赖对应固定宿主锁的版本，自己的 Cargo.lock 保存；不是完整 Router 构建的替代。

Windows 当前代码：CPU 282 passed / 4 platform skips，integration 3 passed；ruff 检查/格式通过。新 sdist→wheel（hatchling 1.32.4）构建及源码外干净安装通过。pip 更新检查有网络 TLS 警告，但 wheel 本地安装和 descriptor/默认关闭检查成功，不将该警告解释为项目依赖或 NPU 兼容失败。

## 标准事件实验边界

新增 `lifecycle_bridge.rs` 包装真实 native Lease，生成 component-v0.1 的 `send_started`、`not_sent/not_accepted` 和 `outcome_unknown/none`，每尝试最多两个记录。Router 单调时钟在同一 provider 内共享；headers 不产生 ACCEPTED，所有传输结束不产生 ENDED。ASCII ID 子集、实例身份与 attempt ID 是受限 CPU fixture，生产分配/映射未实现。

`tools.lifecycle_trace_probe` 在 native 进程退出后将记录重放到既有 ReferenceLedger，校验事件身份、连续序号、时钟、重复投递、预算与未知保留。重放的预记账是合成输入；没有在线可靠投递、生产持久化或重启恢复。原 fixture 的中途合成 ENDED 清债与累计传输记录分开报告。

完整 aarch64 宿主的流式 helper 新编译通过（10m11s，原 Cargo 锁/私有 SDK 保持）；包含新 lifecycle example 的最终 release 构建通过（2m47s）。实际 HTTP stream 回归 8 次 POST、两种 load 分支通过；admission 的 default-off/准入/控制 fence/503/错误/drop/取消/并发回归通过，后端尝试数仍 26，未重发。两工具正常退出、owned group 无强制清理。

标准记录 24 个已预记账尝试、46 条事件：现有账本重放接受且重复事件返回 duplicate；22 个已发送仍 unknown、2 个明确未发送释放，backend_confirmed=0。原 fixture 最后一批未决数为 8，因为前面分批用了明确合成 ENDED 清债；不能把 22 与 8 混为同一口径。

最终 native binary SHA256：

- stream：`a6b8c63521187f8c5f531e1ece152cfd78bdbb4f6af1d0023fcfe8cdff19f3e5`
- admission：`9d688cfec6d8eac035986f96363ba3f0f2cd712653329136d358264a56db2739`

代码 `d51be7a` 同步到服务器新分支后，Linux aarch64 CPU 286、integration 3、新 sdist/wheel 和源码外干净安装通过。直接在同步后的共享 helper 上再次 cargo fmt/checks 10 项通过；与实际编译 helper、lifecycle helper 和 admission example 的内容比较仅归一化 CRLF 后一致。完整原生制品独立于 Python wheel 交付，MOD 激活仍未实现。本提交 CI 尚待创建草稿 PR 后执行，不继承旧 head 成绩。

## 固定 HUST 源码审计

归档 `13f11da44455d2f85f00f810a04e1bd2eaf30ba6` 的工作树无源码修改。阅读 completion serving、AsyncLLM、OutputProcessor、CoreClient、Core 和 Scheduler 的相关函数；不是套用移动的上游文档。

确认 `[DONE]` 错误路径也发送；前端 stop string 可在核心未终止时输出 finished；abort_async 等待的是输入发送，没有逐请求处理应答；finished ID 导出受内部 DP balancing 条件控制。依据和下一门槛见[终态审计](../docs/planning/backend-terminal-evidence.md)。没有添加终态 observer、设备同步或请求性能系数。

归档读取的文件 SHA256：

| 文件（相对 vllm） | SHA256 |
|---|---|
| `entrypoints/openai/completion/serving.py` | `96d7fbf0feddc3e93da8a4ac3bafbdd941ba7ba42529f757c998693f1b5e3f38` |
| `v1/engine/async_llm.py` | `ff8f9c021dab4c057886266fe3fa845b35924828ae0c6684d4beb0da66a1e680` |
| `v1/engine/output_processor.py` | `464c3cda347512cf1090f8e6471d0d3f0f65e286eb9a7b97be52efbc1ffd572a` |
| `v1/engine/core_client.py` | `4ed1f6bc628b586a3b33aa904fe0ef4b2d2cd767dfb2ec9ebbbaea738ea59eeb` |
| `v1/core/sched/scheduler.py` | `95ee1826e098ff63162ff144f0951a90be19ac1278851867530427572a23db0f` |

## 尚未通过

M3/M5 可靠在线投递、正常/取消确认、留存和重启恢复；M7→M2 wire/新鲜度、多分区总预算；HTTP/2/未消费连接期限和请求/非流式预算；真实 NPU/图/持续运行、两副本容量及生产 ABI/交付冻结。已自检，独立成员审查和本提交 CI 待完成，不自动合并。后续先核实可靠终态观测的可维护接缝，CPU 当前够用；真机验证需要 1 张，两个 TP=1 后续需要 2 张。
