# 常驻终态循环的单来源驱动边界

状态：驱动及固定 HUST HTTP 运输已通过 CPU 联调；有限 probe 新路径显式可选，尚待 NPU 资格与常驻装配，见[运输证据](../../evidence/hust-terminal-transport-2026-10-22.md)。

后续接入合同：使用固定 ced6857/b47795c 可选终态端点的 HTTP 运输。连接前由现有来源控制核验身份，适配器绑定完整 cursor、Frontend/Core PID 和 ownership；仅回环 HTTP、禁代理/重定向，单次 RPC（含响应体）限时，解码前限制字节。poll/ACK 均整批核验 PID、身份、mode、连续序号和完整 family；ACK 成功响应按该固定处理器的先 ACK 后返回语义确认请求水位，允许携带后续记录，后续 poll 才提交这些记录。retire 只允许显式受管模式，并核验相同 binding 和精确水位。失联/5xx 可重试，拒绝游标、身份冲突、格式及资源超限失败关闭。CPU 协议联调不得写成 NPU 核心清理资格。

有限 probe 的 `reconcile` 由实验请求间显式调用，并在 active=0 时退出。生产服务不能依赖该调度：最后一批终态已经持久提交但 ACK/retire 回复丢失时，active 可以已经为 0，源端却还保留记录。若只看负债数停止对账，长期资源回收会中断。

在同一个 `DurableRuntime` 外增加一个每来源唯一的 `TerminalDriver`，不是新账本。它只保存固定来源 cursor、ACK/retire 待确认阶段和失败标记，不保留第二份终态历史。一个 step 最多处理 64 条，持续循环与 HTTP 请求无关；错误退避显式有界，不创建逐记录任务或无界队列。

顺序为：恢复持久 cursor → 补同水位 ACK/受管 retire → poll → 完整批校验 → 原账本持久提交 → ACK → 显式受管 retire。ACK 或 retire 失联只重试原水位，不重放已经提交的结算；observe_only 永远不调用 retire。启动即补确认，即使 active=0。源身份/批次/游标错误及不确定提交失败关闭；仅明确未变更的 commit 队列满可重试。

运输适配器负责实际 PID/instance/observer/mode、完整 family、payload 上限和 RPC 期限。ACK 响应可能同时含新终态，不能把“响应必须为空”作为正常并发条件；新数据必须在后续 poll 依同一持久 cursor 消费。最初驱动测试使用运输替身；新增 HTTP 适配已跨进程接入现有 FrontendEnds，但 Core utility 仍为 CPU 替身，不能作为 NPU 核心清理资格。

停止请求只在有界操作间生效，禁止直接取消进行中的持久转换。自审进一步补上任务监督围栏：正常停止、监督通道丢失、panic 展开或 future 被取消时均关闭 owner 新准入，不能让 HTTP 在无人对账时继续派发；同进程不自动复活该 owner。进程强退仍走原持久恢复合同。Manager/信号层仍需先关准入，再继续终态与 HTTP 排空；负债清零、HTTP 清零和远端确认完成是不同条件。期限到期必须保留持久状态，不能伪造终态。实际信号、任务监管、配置和启动恢复装配仍待验收，MOD 保持 import_only。

代价与退出条件：每来源一个可监管任务和明确轮询间隔；采用原源端协议而非新消息服务。若现有协议不能验证同一水位 ACK 或 mode，就停止真实装配并补 profile 资格，不放松来源守卫。性能默认值必须由同底座测量确定。
