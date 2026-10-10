# 高性能分布式 KV 智能路由与管理系统调研

更新时间：2026-10-01  
调研目标：筛选对“分布式 KV 智能路由管理器”最有工程借鉴价值、且公开结果较好的系统，回答它们**解决什么问题、如何设计、优势来自哪里、何时会退化**。

> 本文不是项目名录，也不按宣传数字排名。顶会论文、官方技术文档、项目方 benchmark、生产团队博客和社区经验的证据强度不同，文中会明确区分。

---

## 1. 先给结论

### 1.1 当前性能较好的系统，已经分成三条技术路线

1. **让请求去 KV 所在的计算节点**：Preble、vLLM Router、SGLang Model Gateway、llm-d、Dynamo、Ray Serve 都属于这一类。核心是维护某种 prefix→worker 映射，同时避免热点节点过载。
2. **让 KV 去请求所在的计算节点**：LMCache、Mooncake Store、Strata、CacheGen 更关注跨 GPU、CPU、SSD 或远端存储恢复 KV，降低请求迁移和本地淘汰的代价。
3. **联合决定“去哪里算、从哪里取、是否值得复用”**：Dynamo、Mooncake、Strata、Bidaw 和较新的 Ray KVAwareRouter 已开始靠近这一方向，但公开系统仍很少真正统一建模路由、排队、传输、缓存层级和失败恢复。

单纯做一个“prefix 命中分数 + 队列长度”的静态加权插件，能作为 baseline，却很难形成长期价值。原因是：

- 命中 10K token 的价值取决于 KV 在 HBM、CPU、SSD 还是远端；
- 同一个命中可能节省大量 prefill，也可能因为网络拥塞而不如重算；
- 当前负载低不代表请求完成得快，decode 长度和正在进行的数据传输都会改变等待时间；
- 追求最高命中率会把共享系统提示词的请求聚集到一个副本，造成 **request herding**；
- 路由器依据“曾经发给谁”推断缓存时，会因淘汰、失败和扩缩容迅速变成陈旧状态。

### 1.2 对本工程最有价值的不是再造一个推理引擎

更合理的产品位置是：

> **位于客户端/网关与现有推理副本之间的、可插拔的 KV 代价感知控制器。它从 vLLM 等引擎采集少量状态，对本地命中、远端恢复、重计算、排队和过载风险做在线选择；不接管模型执行，也不要求把 vLLM、SGLang、Dynamo 的代码合并。**

这个位置的实际价值是把目前分散在多个系统里的能力变成一个可验证的闭环：**观测真实状态 → 估计各候选路径的完成时间 → 选择动作 → 用实际结果校准估计 → 在不确定时安全退化**。

---

## 2. 调研口径与证据等级

### 2.1 纳入标准

系统至少满足一项：

- 公开实现并可作为 vLLM/SGLang 等推理引擎的外部组件；
- 在 OSDI、FAST、SIGCOMM、ICLR 等会议给出完整设计和实验；
- 有较完整的生产或接近生产的 workload、硬件和对照组说明；
- 提出了会改变本项目组件边界的重要机制，而不只是更换一个打分公式。

### 2.2 证据等级

| 标记 | 来源 | 应如何理解 |
|---|---|---|
| A | 同行评审顶会论文 | 设计和实验相对完整，但仍需检查负载与本工程是否一致 |
| B | 官方文档、开源实现、可复现 artifact | 能确认能力确实存在，不自动代表效果普适 |
| C | 项目方或生产团队博客/benchmark | 对工程细节很有价值，但结果可能选择了有利负载 |
| D | 第三方博客、社区分享 | 适合发现实际痛点和部署问题，不宜单独支撑性能结论 |

特别提醒：不同文章的“提升 2×、10×、47×”通常不可横向比较。长上下文、完全重复前缀、低并发时，cache-aware 相对 round-robin 很容易得到极大的 TTFT 倍数；在低复用、短提示、高并发或 decode-heavy 负载中，收益会明显缩小甚至反转。

---

## 3. 系统总览

| 系统 | 类型 | 路由器知道什么 | 能执行什么 | 主要优势 | 主要短板 | 证据 |
|---|---|---|---|---|---|---|
| vLLM Router | 轻量网关 | 请求数、负载、近似 prefix、P/D 拓扑 | worker 选择、P/D 编排、重试/熔断 | 易接入 vLLM，网关开销低，生产能力完整 | 缓存状态和分层数据代价仍不够精确 | B/C |
| SGLang Model Gateway | 轻量网关 | 路由历史构造的近似 prefix tree、负载 | cache-aware / power-of-two / random、P/D | 实现简洁、Rust 网关、与 RadixAttention 配合好 | 近似树不感知真实淘汰，容易陈旧 | B/C |
| NVIDIA Dynamo | 分布式推理编排层 | KV 事件、prefix block、in-flight token load、队列、层级位置 | cache/load-aware 路由、优先级、P/D、offload | 状态最丰富、后端兼容范围广、可扩展策略 | 组件多、部署和调参复杂，仍依赖成本权重 | B/C |
| llm-d | Kubernetes 推理层 | vLLM KV 事件、队列/负载、P/D endpoints | EPP 选点、P/D、KV offload/P2P | K8s 原生，组件边界清楚，适合生产运维 | 控制链较长，很多能力依赖外部组件 | B/C |
| Ray Serve LLM | 应用/集群编排层 | session、prefix/KV affinity、请求生命周期、token load | 一致性哈希、prefix/KV-aware 路由、扩缩容 | 把 KV 命中降为“剩余工作量”的输入，避免纯命中扎堆 | 输出长度仍未知；精确状态和多层代价尚不完整 | B/C |
| Preble | 学术调度系统 | 全局 prefix tree、请求负载、热点 prefix | 分层调度、热点复制 | 首次系统化解决复用与负载冲突 | 对真实淘汰、外部 KV 层和网络代价覆盖较弱 | A |
| LMetric | 负载度量/调度 | 新 prefill token、batch、worker 状态 | 用新度量选点 | 简单但贴近 GPU 执行成本，易作为强 baseline | 不负责跨层 KV 移动，未来 decode 成本仍难预测 | A |
| LMCache | 外部 KV 层 | chunk 位置、worker/peer、CPU/远端层状态 | store/load、P2P、offload | 以连接器方式扩展 vLLM，不重写执行引擎 | 路由与存储优化仍常被分开；传输不一定比重算便宜 | B/C |
| Mooncake | 分布式 KV 存储 + P/D | 全局 block metadata、节点健康、DRAM/SSD、网络路径 | RDMA KV 传输、分布式池、P/D connector | 数据路径强，能消除跨副本 KV 孤岛 | 元数据和网络是新瓶颈；其博客也承认仍需联合 cache-aware routing | A/B/C |
| Strata | 分层 KV 系统 | HBM/CPU/SSD 位置、正在加载状态、I/O 进度 | 分层放置、GPU 辅助 I/O、delayed-hit 调度 | 不是只看“是否命中”，而是看“何时可用” | 设计更侵入，适用于有本地高速 SSD 的部署 | A |
| Bidaw | 计算-存储协同调度 | 计算队列、存储队列、预测复用价值 | 双队列协同、响应长度辅助保留 | 显式处理计算与 I/O 相互阻塞 | 预测误差和部署复杂度是风险 | A |
| CacheGen | KV 传输编码 | KV 张量统计、网络条件 | 压缩、流式传输、逐层恢复 | 降低远端 KV 传输量，改变“取还是算”的临界点 | 本身不是集群路由器；有编码开销和质量/格式约束 | A |
| CacheRoute | 联合路由/缓存研究原型 | cache 内容、请求亲和和负载 | 以缓存结果反馈路由/放置 | 直接针对路由与缓存相互影响 | 预印本阶段，结论需更多独立复现 | 预印本 |

---

## 4. 路由控制面系统

### 4.1 vLLM Router：把通用负载均衡器替换为 LLM 状态感知网关

**主要解决的问题**

普通 Kubernetes Service 或 round-robin 不知道请求是否能复用 KV，也不知道哪些 worker 是 prefill、哪些是 decode。vLLM Router 的目标不是改动 vLLM 内核，而是在前面增加一个低开销的 Rust 网关，负责智能选点、P/D 流程和生产级故障处理。[vLLM Router 发布说明](https://vllm.ai/blog/2025-12-13-vllm-router-release)

**系统设计**

- 位于客户端与 vLLM workers 之间；
- 支持 round-robin、least-loaded、power-of-two、consistent hashing 和 cache-aware 等策略；
- 在 P/D 模式下分别选择 prefill 与 decode worker，并支持 NIXL、NCCL/ZMQ 等连接方式；
- 附带 Kubernetes 服务发现、健康检查、指数退避重试、熔断和 Prometheus 指标。

**优势来自哪里**

它的核心优势并不是提出了最复杂的调度算法，而是把“可部署的专用路由器”补齐：比通用网关更懂 LLM 状态，又比完整 Dynamo/llm-d 更轻。对于本项目而言，这是最接近“插件产品形态”的参考。

**局限与退化条件**

- vLLM Router 源自 SGLang Model Gateway 分支，当前创新重点仍是高性能网关与 P/D 编排，而非完整的多层 KV 成本模型；
- 如果 cache-aware 状态来自请求历史而非真实 block 事件，淘汰后会误判；
- 单独比较网关吞吐不能证明端到端模型服务更快，应关注 TTFT、TPOT、goodput 和尾延迟。

**对本项目的启示**

必须保留 vLLM Router 的轻量、故障隔离和可观测性；但差异点不能只是再添加几个策略，而应是更准确的**动作成本与置信度估计**。

---

### 4.2 SGLang Model Gateway：低成本近似 prefix 状态

**主要解决的问题**

SGLang 的 RadixAttention 能在单个 engine 内复用公共前缀，但多副本部署中，如果上层随机分流，单机缓存优势会被稀释。Model Gateway 在网关维护近似 prefix tree，把共享前缀请求尽量发往同一副本。[SGLang Model Gateway](https://github.com/sgl-project/sglang/blob/main/sgl-model-gateway/README.md)；[RadixAttention 设计介绍](https://www.lmsys.org/blog/2024-01-17-sglang/)

**系统设计**

- 根据已经路由的请求在网关构造近似前缀树；
- 找到最长公共前缀对应的 worker；
- 当前缀优势不足或目标节点负载过高时，退回 power-of-two/负载策略；
- 与 engine 内 RadixAttention 的 radix tree 形成“集群近似索引 + 单机真实缓存”的两层结构。

**效果与优势**

SGLang 项目给出的特定 8×A100 benchmark 中，cache-aware routing 将 cache hit 从约 20% 提高到 75%，吞吐从 82,665 提高到 158,596 token/s（约 +92%）。这是**项目方 benchmark**，说明机制在高共享前缀负载下有效，但不代表任意生产流量都能复现。[SGLang DP/DPA/SMG 指南](https://github.com/sgl-project/sglang/blob/main/docs_new/docs/advanced_features/dp_dpa_smg_guide.mdx)

**局限与退化条件**

- 树记录“请求曾发往何处”，并不等于 block 仍在 HBM；
- 不直接知道 engine 淘汰、offload、加载中和失败状态；
- 大量请求共享同一个系统提示词时会产生热点；
- SGLang 自身 issue 也记录了 agent trace 下近似树可能落后于真实缓存、导致 cache-aware 反而退化的情形，并提出用实际 cache state 和反馈驱动路由改进。[相关工程讨论](https://github.com/sgl-project/sglang/issues/34513)

**对本项目的启示**

近似前缀树很适合作为无需改 engine 的兼容模式，但必须输出置信度，并在状态陈旧、低复用或负载失衡时自动切换，而不能把它当作唯一真相。

---

### 4.3 NVIDIA Dynamo：状态最完整的通用参考架构

**主要解决的问题**

Dynamo 试图在 vLLM、SGLang、TensorRT-LLM 等 runtime 之上提供分布式推理编排：路由器不仅要知道缓存重合，还要考虑 in-flight 请求、prefill/decode 工作量、分层 KV、P/D 拓扑和优先级。[Dynamo KV-aware routing 文档](https://docs.nvidia.com/dynamo/knowledge-base/concepts/system-architecture/kv-aware-routing)

**系统设计**

- 通过 engine 发出的 KV events 维护全局 prefix/block 索引；
- 为候选 worker 计算 cache overlap，同时对已经分配但尚未反映在 engine 指标里的新请求进行本地“预记账”，减少并发请求同时涌向一个 worker；
- 支持精确事件驱动索引，也支持成本更低的近似模式；
- 能给 HBM、host、disk 等不同位置设置权重，并结合 active blocks、prefill tokens、队列等负载；
- 支持 priority、队列阈值、P/D disaggregation、KV offload 和自定义策略。[路由配置与调优](https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/router/configuration-and-tuning)

2026 年的 agentic 方案进一步引入 harness hints：上层可提供优先级、预期输出长度、未来 prefill 意图和生命周期信息；系统可以保留长期会复用的 prefix，尽早淘汰子任务结束后失去价值的 KV。[NVIDIA agentic inference 技术博客](https://developer.nvidia.com/blog/full-stack-optimizations-for-agentic-inference-with-nvidia-dynamo/)

**效果与优势**

Baseten 在 Qwen3 Coder 480B、平均约 50K 输入/1K 输出的高压测试中报告：启用 Dynamo KV routing 后，TTFT 平均下降 50%、TPOT 下降 34%，RPS 增加 61%；其 shadow production traffic 中 P95/P99 latency 分别下降约 48%/49%。这是**生产团队博客结果**，比纯合成 prefix benchmark 更有参考性，但仍不是独立评测。[Baseten 实测](https://www.baseten.co/blog/how-baseten-achieved-2x-faster-inference-with-nvidia-dynamo/)

**局限与退化条件**

- 系统组件和配置面较多，不适合原样复制成一个轻量插件；
- 位置权重与负载权重本质上仍需依赖硬件和 workload 调参；
- 输出长度不确定、网络瞬时拥塞、事件延迟仍会造成预测误差；
- 如果把所有 Dynamo 能力都纳入首版，本工程会退化为重做一个编排平台。

**对本项目的启示**

Dynamo 最值得借鉴的是三件事：真实 KV 事件、in-flight 预记账、多层位置差异。最不该照搬的是它的全栈范围。

---

### 4.4 llm-d：Kubernetes 原生的可组合推理层

**主要解决的问题**

llm-d 关注在 Kubernetes 中把 vLLM 扩展为可运维的分布式服务。其 Endpoint Picker（EPP）在请求进入具体 pod 前，结合 KV 索引和负载选点，从而避免 Kubernetes 默认分流破坏 prefix reuse。[llm-d 与 vLLM 集成说明](https://docs.vllm.ai/en/latest/deployment/integrations/llm-d/)

**系统设计**

- 从 vLLM KV events 构造 prefix→pod 索引；
- 在 Gateway API / EPP 路径中做候选过滤和评分；
- 可组合 P/D disaggregation、KV offload、LMCache/P2P 等数据面；
- 组件边界清晰：vLLM 执行、EPP 选点、外部 KV 层移动数据、Kubernetes 管理生命周期。[llm-d KV 管理架构](https://github.com/llm-d/llm-d/blob/main/docs/architecture/advanced/kv-management/README.md)

**效果与优势**

一项公开 H200 测试在 3×H200、Llama 3.1 70B、vLLM 0.8.5 环境中，只改变 round-robin 与 cache-aware 策略：8K context 的 mean TTFT 从 220.1 ms 降至 55.5 ms，P95 从 288.3 ms 降至 73.0 ms；32K 完全可复用 context 下报告 47.5× TTFT 改善。[测试方法与结果](https://markellr-redhat.github.io/llm-d-benchmark/)

这里的 47.5× 不应理解为一般生产收益：实验刻意强调长上下文和强复用，且页面中的 fleet scaling 部分带有投影成分。更可信的结论是：**只要路由器能准确找到已存在的长 prefix，TTFT 会从随上下文增长变成近似固定的增量成本。**

**局限与退化条件**

- Kubernetes 调度路径和多个控制组件增加了调试成本；
- cache-aware 收益高度依赖可复用 prefix；
- routing、offload、P2P 并非天然联合优化，可能出现路由器追求命中但数据路径拥塞。

**对本项目的启示**

llm-d 证明外部插件不需要侵入 vLLM scheduler；但首版不必绑定 Kubernetes，可先提供独立 sidecar/standalone router，再做 EPP 适配。

---

### 4.5 Ray Serve LLM：从“最大化命中”转向“平衡剩余 token load”

**主要解决的问题**

Ray Serve 最初的 PrefixCacheAffinityRouter 使用字符级前缀树近似 vLLM cache，并在命中不强或负载失衡时退回 power-of-two。项目方在 32B 模型上报告 TTFT 降低 60%、端到端吞吐提高 40% 以上。[早期 prefix-aware router](https://www.anyscale.com/blog/ray-serve-faster-first-token-custom-routing)

后续工程发现：最大化 cache reuse 会造成 request herding，而且“请求数相同”不等于 GPU 工作量相同，于是 KVAwareRouter 将目标改成 **剩余 prefill token load + 当前 decode token load**。[token-load-aware routing](https://www.anyscale.com/blog/llm-kv-token-aware-routing)

**系统设计**

- 支持 session affinity（一致性哈希）、prefix affinity（近似树）和 KV affinity（engine cache 信息）；
- 跟踪请求 admitted、first-token、decoding、completed 等生命周期，持续更新 token load；
- cache overlap 不作为最终目标，而用来扣除无需重算的 prefill tokens；
- 多 router 副本本地决策、异步传播负载，避免把同步共识放在 token streaming 热路径上；
- 集成 Dynamo 的 selection service 模块做候选评分。

**优势来自哪里**

它给出了一个比静态线性加权更正确的抽象：

> cache hit 是“减少了多少待执行工作”的观测，而不是一个应单独最大化的 KPI。

在异构 agentic multi-turn trace 中，KVAwareRouter 会主动牺牲一部分 hit rate 来降低 decode-block 不均衡，从而改善 TTFT、TPOT 和吞吐。

**局限与退化条件**

- admission 时仍不知道真实输出长度；长 decode straggler 会破坏已有平衡；
- prefix tree 不感知淘汰，多层 KV 的加载成本也未完全纳入；
- session affinity 在跨 session 几乎无复用、且同一 session 的局部性极强时可能更稳；
- 需要较准确的请求生命周期事件，网关与 engine 接口比纯 HTTP proxy 更深。

**对本项目的启示**

这是当前最直接的强 baseline。本工程要胜过它，不能只换一个负载指标，而应加入 **KV 层级/传输时间、状态置信度、预测误差反馈和动作切换成本**。

---

### 4.6 Preble：全局 prefix tree 与热点复制

**主要解决的问题**

多 GPU 上，最大化 prefix locality 会形成热点；单纯均衡负载又会丢失复用。Preble 将这个冲突作为核心问题，而不是在通用负载均衡器上附加缓存分数。[Preble, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/hash/5bc342f48de8264779952fac378f96dc-Abstract-Conference.html)

**系统设计**

- 全局 prefix tree 记录不同 GPU 上的前缀分布；
- 对高复用、低分散需求尽量维持 locality；
- 对热点前缀主动复制，使其能被多个 GPU 服务；
- 分层调度将全局放置和本地执行拆开。

**效果与优势**

论文报告相对当时 SOTA，平均延迟改善 1.5–14.5×，P99 改善 2–10×。其真正贡献不是倍数，而是明确指出 **复制是解决热点与局部性矛盾的一个动作**，路由器不应只有“选现有副本”这一种自由度。

**局限与退化条件**

- 热点复制消耗额外显存/计算，错误预测会浪费容量；
- 对 CPU/SSD/远端层和传输中状态覆盖不够；
- prefix tree 的规模、更新与淘汰一致性在更大集群中仍是工程难点。

**对本项目的启示**

首版不必实现 KV 主动复制，但接口上应允许未来输出 `route`、`replicate/prefetch`、`bypass/recompute` 等不同动作，否则系统会被限制为“更聪明的负载均衡器”。

---

### 4.7 LMetric：简单策略也能有效，关键是度量对应真实执行成本

**主要解决的问题**

请求数、序列数或总 token 数都不能准确表示某个 continuous-batching worker 接收新请求后的增量 prefill 开销。LMetric 用“未命中的新 prefill tokens × 当前 batch 相关量”等更贴近执行过程的指标做调度。[LMetric, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/zhang-dingyan)

**优势来自哪里**

它没有依靠庞大的学习模型，而是从 GPU batching 机制出发重新定义 load。论文报告 TTFT 相对 vLLM 最多下降 92%，相对生产调度器最多下降 39%，并有实际部署经验。

**局限与启示**

LMetric 不解决跨层 KV 移动，也无法消除输出长度未知。但它提醒我们：创新不等于复杂。只要指标能解释真实瓶颈，并在多类负载上稳定优于旧指标，简单模型同样有工程价值。它应成为本项目必须纳入的 baseline，而不是被“智能算法”绕开。

---

## 5. KV 数据面与分层管理系统

### 5.1 LMCache：用外部 KV 层消除副本缓存孤岛

**主要解决的问题**

多个 vLLM 实例各自只拥有本地 GPU/CPU cache。即使机器总内存很大，请求落到另一个实例也无法直接使用已有 KV，形成 cache silo。LMCache 通过 vLLM connector 把 KV 保存到 CPU、远端存储或其他 peer，使跨请求、跨实例恢复成为可能。[LMCache 技术报告](https://lmcache.ai/tech_report.pdf)

**系统设计**

- engine 侧 connector 负责按 chunk 保存/加载 KV；
- 控制器维护 instance、worker、KV chunk location 元数据；
- P2P backend 允许直接从其他实例的 CPU 内存拉取；
- 可对接 Redis、S3、自定义远端存储，也能与 P/D 系统组合；
- 数据移动使用批处理、流水化和异步路径减少对推理主线程的影响。

**效果与优势**

LMCache 的 P2P 博客在两实例、完全复用的 PoC 中报告 mean TTFT 从 2.028s 降到 0.490s，总轮次时间从 38.323s 降到 7.964s。作者明确说明这是**人为构造的 full-reuse benchmark**，因此更适合证明链路可用，不适合预测生产收益。[P2P 设计与 PoC](https://blog.lmcache.ai/en/2026/01/21/p2p-1/)

其工程价值主要在接口和容量扩展：它让上层路由器不必把“目标节点本地无 KV”视为绝对 miss。

**局限与退化条件**

- 拉取 KV 的字节量很大，网络/PCIe 慢时可能不如重算；
- controller 元数据、full sync、节点离线和过期状态都需要谨慎处理；
- P2P 提高可达性，但不自动决定“从哪个 peer 拉、是否值得拉、会不会堵塞链路”；
- 外部存储命中率高也可能因恢复时间过长而恶化 TTFT。

**对本项目的启示**

应把 LMCache 当作可选数据面，而非强制依赖。控制器需要比较：`local reuse`、`remote load`、`recompute` 三条路径的预测完成时间。

---

### 5.2 Mooncake：高性能全局 KV 池与 RDMA 数据路径

**主要解决的问题**

Mooncake 的核心场景是大规模、长上下文推理中的 KV 容量和传输问题。FAST 2025 论文将 prefill/decode 分离，并构造全局分布式 KV cache，使不同请求和 worker 能跨节点复用。[Mooncake, FAST 2025](https://www.usenix.org/conference/fast25/presentation/qin)

**系统设计**

- master 管理全局 block hash、大小、service discovery、节点健康和失效清理；
- 每个 GPU 节点运行 client，贡献本地 DRAM/SSD；
- 通过 RDMA/GPUDirect 在 HBM 与分布式池之间直接移动 KV，尽量避免 CPU staging 和 GPU SM 参与；
- 专用后台 I/O thread 异步准备和提交 RDMA 操作；
- 通过 vLLM `KVConnector` / `MultiConnector` 同时支持外部 KV pool 和 P/D 连接。

**效果与优势**

论文在 trace evaluation 中报告有效服务容量提升 59%–498%，生产 A800/H800 集群可多处理约 115%/107% 请求。近期 vLLM×Mooncake 在 610 条 Codex/SWE-bench Pro agent traces 上报告：1P1D、12×GB200 时，吞吐 3.8×、P50 TTFT 46×、E2E latency 8.6×，cache hit 从 1.7% 升至 92.2%；扩展到 60 GPU 时保持 >95% hit 并近线性扩展。[vLLM×Mooncake 工程博客与 artifact](https://vllm.ai/blog/2026-05-06-mooncake-store)

后组数字是项目方结果，但其负载说明较完整：agent session 中 context 从约 12K 增长到 80K，平均 input/output 比约 131:1，正是分布式 KV pool 最有利的 write-once-read-many 场景。

**局限与退化条件**

- RDMA、RNIC 拓扑、注册内存和 metadata service 提高了部署门槛；
- 低复用或短提示下，存储/查询/传输开销可能不划算；
- master 的扩展性和失败恢复是控制面风险；
- 值得注意的是，Mooncake 博客把“与 cache-aware routing 联合设计”列为后续工作，说明强数据面并未自动解决最优选点问题。

**对本项目的启示**

Mooncake 可以作为未来高性能 data-plane backend；本工程的价值应体现在决定何时优先本地、何时跨节点取、何时重算，而不是重写 RDMA 引擎。

---

### 5.3 Strata：把“命中”细化为“可用时间”

**主要解决的问题**

传统调度器把 KV cache 看成命中/未命中二值状态，但在 HBM、CPU、SSD 分层系统中，命中的 KV 可能还在加载；如果请求立即排队等待，GPU 可能空转或阻塞。Strata 将 loading state 和 I/O 进度纳入调度。[Strata, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang)

**系统设计**

- HBM、CPU、SSD 多层管理；
- GPU-assisted I/O 降低搬运开销；
- delayed-hit / loading-aware scheduler：KV 尚未就绪时，不把“逻辑命中”误当作“立即可执行”，而是协调其他可运行工作；
- 与 SGLang 等执行引擎集成，而非单独做离线缓存。

**效果与优势**

论文报告相对 vLLM+LMCache 最多 5×、相对 TensorRT-LLM 最多 3.75×，并描述了生产部署。其最值得借鉴的不是具体存储层，而是新状态：`resident`、`loading`、`ready_at`。这正是多数路由插件缺失的信息。

**局限与退化条件**

- 需要较深地改动 I/O 与 scheduler；
- 本地 NVMe 性能差、KV 工作集较小或复用低时，收益有限；
- 若上层路由器完全不知道加载进度，仍可能破坏 Strata 的局部调度优势。

**对本项目的启示**

控制器不应只有 cache hit bitmap，至少要允许 backend 报告 `tier`、`bytes`、`transfer/loading ETA` 和 `confidence`。

---

### 5.4 Bidaw：计算队列与存储队列必须协同

**主要解决的问题**

外部 KV 系统中，计算资源和存储/传输资源各有队列。只优化 GPU queue 会把大量请求同时送去拉 KV；只优化 cache reuse 又可能让 GPU 等待 I/O。Bidaw 使用 compute-storage bidirectional awareness 协调两侧。[Bidaw, FAST 2026](https://www.usenix.org/conference/fast26/presentation/hu-shipeng)

**系统设计与优势**

- 显式维护计算侧与存储侧压力；
- 使用双队列/协同调度减少 head-of-line blocking；
- 利用响应长度或复用预测提高高价值 KV 的保留概率；
- 论文报告最高 3.58× 延迟改善、1.83× 吞吐提升。

**局限与启示**

其风险是预测错误和组件耦合增加。但它指出一个经常被忽略的工程事实：**网络和存储队列也是路由负载**。本项目若只采集 GPU active requests，就无法在启用 LMCache/Mooncake 后做正确决策。

---

### 5.5 CacheGen：压缩会改变“远端加载 vs 重算”的决策边界

**主要解决的问题**

远端 KV cache 很大，传输时间可能抵消复用收益。CacheGen 对 KV 张量做适合网络传输的编码和流式恢复，在网络变化时选择不同码率。[CacheGen, SIGCOMM 2024](https://cs.stanford.edu/~keithw/sigcomm2024/sigcomm24-final1571-acmpaginated.pdf)

**效果与优势**

论文报告 KV 表示缩小 3.5–4.3×，总 context fetch/process delay 改善 3.2–3.7×。这意味着路由控制器的成本模型不能把 `KV bytes` 直接等同于固定传输时间；编码格式、网络带宽和是否可边传边算都会改变选择。

**局限与启示**

CacheGen 本身不是路由器，编码也带来 CPU/GPU 开销和实现兼容问题。但它适合作为未来 data-plane capability：控制器只需消费 backend 给出的预计恢复时间，不应绑定具体压缩算法。

---

### 5.6 第三方长上下文实测：SSD 不只是“更慢的 DRAM”

StorageReview 使用 4×RTX PRO 6000、MiniMax-M2.7，对比纯 VRAM、LMCache+512GB DRAM、LMCache+本地 RAID10 NVMe；workload 模拟多轮 coding agent，请求逐渐增长到 64K，上下文复用高且 working set 最终超过 DRAM。[完整测试](https://www.storagereview.com/review/the-token-efficient-path-for-long-context-inference-kv-cache-offload-to-flash)

其有价值的观察不是某个峰值倍数，而是：DRAM 层容量耗尽并开始淘汰后，TB 级 flash 因避免重算可以同时获得更高持续吞吐和更低单位容量成本。也就是说，tier preference 不是永远 `HBM > DRAM > SSD`；当容量压力和未来复用进入模型后，较慢但不淘汰的层可能提供更高长期 goodput。

这属于第三方特定硬件测试，不应外推到所有 SSD，但可以用来设计本工程的 tier-aware benchmark。

---

## 6. 联合优化研究

### 6.1 CacheRoute：路由与 cache placement 不能各自独立优化

CacheRoute 直接讨论一个反馈环：路由决定每个节点会看到什么前缀，进而决定其缓存内容；缓存内容又影响后续路由。如果二者各自贪心，系统可能锁定在不理想的局部状态。预印本报告部分 workload 的 cache hit 从 64.1% 提高到 93.2%，但也给出了两个 32B、低复用 workload 中收益不明显的反例。[CacheRoute 预印本](https://arxiv.org/abs/2608.19677)

对本项目更重要的是它保留了反例：低复用时，不应为了“智能”而强制启用复杂策略。系统必须有 workload classifier、置信度或在线回退机制。

### 6.2 随机化联合 eviction/routing：理论上也不应拆开

ICLR 2026 的相关工作对 eviction 与 routing 建立统一模型，说明两者的联合决策可以避免确定性策略在对抗性请求序列上的坏情况。[论文](https://openreview.net/pdf?id=R7fv5NWfMm)

这对首版工程的意义有限：无需立刻实现复杂随机算法。但插件接口不应假定 eviction 完全不可控；至少要接收 eviction feedback，并为未来的 admission/retention hint 留出通道。

---

## 7. 横向判断：这些系统为什么有效

### 7.1 真正带来收益的六种机制

| 机制 | 代表系统 | 它消除的浪费 | 代价 |
|---|---|---|---|
| Prefix locality | SGLang、Preble、llm-d | 重复 prefill | 热点、状态陈旧 |
| 增量负载估计 | LMetric、Ray、Dynamo | 请求数无法反映真实 GPU 工作 | 需要生命周期和 token 状态 |
| 全局 KV 索引 | Dynamo、llm-d、Mooncake | 不知道 KV 在哪 | 元数据规模、一致性、事件延迟 |
| KV 跨节点恢复 | LMCache、Mooncake | 副本 cache silo | 网络/PCIe/I/O 竞争 |
| 多层与 loading awareness | Strata、Bidaw | 把慢命中当成立即可用 | 更深的引擎/存储集成 |
| 复制、预取、保留提示 | Preble、Dynamo agent hints | 只能被动选择现有状态 | 错误预测会浪费资源 |

### 7.2 公开系统仍普遍缺失的部分

1. **跨动作统一比较**：多数系统只在“选哪个 worker”或“从哪里取 KV”中的一侧优化，缺少 `local reuse / remote fetch / recompute / wait / replicate` 的统一完成时间比较。
2. **状态可信度**：精确事件也有延迟，近似 prefix tree 更会陈旧；很少有系统把状态年龄与置信区间显式纳入决策。
3. **预测后的闭环校准**：公开实现通常暴露指标，却很少把“预测 TTFT 与实际 TTFT 的残差”用于在线修正不同硬件、模型和负载下的参数。
4. **SLO 风险而非平均分数**：静态加权通常优化期望值，不能表达某次跨节点拉取堵塞后造成 P99 违约的风险。
5. **动作切换的稳定性**：策略频繁在 cache-first 和 load-first 间切换会导致抖动，缓存分布也会被自身决策持续改变。
6. **兼容模式分级**：不是所有 engine 都能发完整 KV events。缺少一个从 session ID、近似 prefix、真实 block event 到多层 transfer event 的渐进式能力模型。

---

## 8. 对当前工程的基本判断

### 8.1 这个系统值得做，但目标不能定义为“提高 KV 命中率”

建议把问题定义为：

> 在同构 GPU、单模型或少量模型的多副本 vLLM 集群中，请求具有变化的前缀复用、输入/输出长度与并发度，KV 可能位于本地 HBM、host/offload 层或其他节点。系统在状态不完整且会过期的条件下，为每个请求选择低风险的执行/恢复路径，使 TTFT、TPOT 与 SLO goodput 优于固定的 least-load、session-affinity 和 cache-first 策略，同时避免任何单一 workload 上的严重退化。

它比“热点感知路由”更完整，也没有扩张到异构 GPU、模型放置、自动扩缩容等当前不熟悉的方向。

### 8.2 实现目的

- 让部署者不必根据 workload 手工选择 `round-robin / least-loaded / session affinity / prefix-aware`；
- 在启用外部 KV backend 后，自动判断远端恢复是否比重算有利；
- 对低复用、状态不可靠或网络拥塞场景自动回退，控制最坏退化；
- 用统一观测接口解释一次路由为什么发生，方便定位“缓存没命中、负载估错、传输太慢还是输出成为 straggler”；
- 作为 vLLM 外部插件独立发布，后续再通过 adapter 支持其他 engine，而不是合并它们的代码。

### 8.3 与现有插件相比应有的特色

建议特色不是“支持更多策略”，而是以下三项组合：

1. **路径级成本模型**：估计每个 worker 上的 `queue + remaining prefill + decode pressure + KV recovery`，而非把几个归一化指标随意相加。
2. **置信度与安全回退**：真实 KV events、近似 prefix、session hint 分级使用；状态过旧或预测不确定时回退到经过验证的 LMetric/least-load。
3. **在线反馈校准**：记录预测与实际 TTFT/queue/transfer time，用轻量校准器适应模型、硬件和并发变化；先用可解释的分段/EMA/在线回归，不必一开始上强化学习。

这三项同时存在，才能形成与 vLLM Router、SGLang Gateway、Ray KVAwareRouter 的清晰区别。

---

## 9. 建议的首版边界与组件

### 9.1 首版应做

```text
Client
  │
  ▼
OpenAI-compatible Router
  ├── Request Analyzer: tokens / prefix / session / SLA
  ├── State Adapters
  │     ├── vLLM metrics + request lifecycle
  │     ├── KV events（可用时）
  │     └── approximate prefix index（兼容模式）
  ├── Cost Estimator
  │     ├── queue / prefill / decode pressure
  │     └── local hit / remote load / recompute ETA
  ├── Decision Policy
  │     ├── predicted completion time
  │     ├── confidence + risk guard
  │     └── fallback / hysteresis
  └── Feedback & Trace Store
        └── prediction residual / actual TTFT / hit / transfer
  │
  ▼
Homogeneous vLLM replicas
  └── optional LMCache or Mooncake adapter
```

### 9.2 首版不应做

- 不重写 vLLM scheduler 或 KV allocator；
- 不同时实现 vLLM、SGLang、TensorRT-LLM 的全功能支持；
- 不先做异构 GPU、跨模型放置或自动扩缩容；
- 不自己实现 RDMA/SSD KV store；
- 不以“命中率最高”作为唯一成功标准；
- 不把复杂学习算法当作项目卖点，除非它在跨 workload 的回退和 P99 上确实优于简单模型。

---

## 10. 最值得复现的基线与评测负载

### 10.1 基线

1. Round-robin；
2. Least active requests / power-of-two；
3. Session consistent hashing；
4. SGLang/Ray 风格近似 prefix affinity；
5. vLLM load-aware 线性评分（命中比例与相对负载）；
6. LMetric 风格 remaining-prefill/token-load；
7. 本项目：tier/transfer/confidence-aware completion-time routing。

[vLLM production stack 的 load-aware 公式](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/loadaware-routing.html)可直接作为强基线，而不是只与 round-robin 比。

### 10.2 必测负载

| 负载 | 目的 | 预期风险 |
|---|---|---|
| 共享 system prompt、短分支 | 测跨 session prefix reuse | cache-first 容易扎堆 |
| 长多轮 coding agent | 测 session 增长和高复用 | KV 容量、跨节点恢复成为瓶颈 |
| RAG：共享模板、文档不同 | 测中等 prefix 复用 | 字符近似与 token block 可能不一致 |
| 低复用随机 prompt | 测安全退化 | 复杂索引和查询成为纯开销 |
| decode-heavy、输出长尾 | 测未知输出和 straggler | admission 时的负载估计失真 |
| 热点 prefix 突发 | 测 herding 与复制需求 | 单节点队列迅速增长 |
| KV 容量压力/淘汰 | 测状态陈旧 | 近似索引错误率上升 |
| 网络限速/抖动/peer 故障 | 测远端加载回退 | remote hit 比 recompute 更慢 |
| 扩缩容与节点重启 | 测元数据收敛 | 幽灵 KV、路由失败和冷启动 |

### 10.3 主要指标

- TTFT、TPOT、E2E latency 的 P50/P95/P99；
- 在 TTFT/TPOT SLO 下的 goodput，而不只是 raw throughput；
- local HBM hit、host hit、remote hit、recompute token 数；
- 每次决策的 predicted vs actual completion time；
- router CPU、内存、决策延迟和状态事件带宽；
- worker token-load CV、网络/存储队列长度；
- 相对最强 baseline 的最坏退化，而不只是最好负载的提升。

---

## 11. 最终结论

这个方向实现上合理，也有实际价值，但前提是把项目从“KV-aware 路由策略集合”提升为“**不确定状态下的执行路径选择与闭环校准器**”。

现有系统已经很好地解决了若干局部问题：

- SGLang/vLLM Router 给出轻量插件形态；
- Preble 处理 locality 与热点冲突；
- LMetric/Ray 让负载指标更接近真实 token work；
- Dynamo/llm-d 提供真实 KV 事件与分布式编排；
- LMCache/Mooncake 打通跨节点 KV 数据面；
- Strata/Bidaw 把层级与传输中状态纳入调度。

仍稳定存在的工程缺口是：这些信息通常分散在不同组件里，系统缺少一个轻量、可插拔、能比较本地复用/远端恢复/重算，并根据预测误差自动回退的控制闭环。这个缺口足以形成项目特色，也不要求把 vLLM 与 SGLang 合并，更不需要在第一阶段进入异构硬件。

下一步最合理的工作不是马上设计复杂算法，而是做一个**可复现实验台**：先在同构 vLLM 多副本上复现上述 6 类 baseline 和 8 类负载，确认哪些状态对预测 completion time 真有增益，再据此冻结首版 API 与组件边界。

---

## 12. 主要资料索引

### 顶会论文

- [Preble: Efficient Distributed Prompt Scheduling for LLM Serving, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/hash/5bc342f48de8264779952fac378f96dc-Abstract-Conference.html)
- [Mooncake: A KVCache-centric Disaggregated Architecture for LLM Serving, FAST 2025](https://www.usenix.org/conference/fast25/presentation/qin)
- [CacheGen: KV Cache Compression and Streaming, SIGCOMM 2024](https://cs.stanford.edu/~keithw/sigcomm2024/sigcomm24-final1571-acmpaginated.pdf)
- [LMetric, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/zhang-dingyan)
- [Strata, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang)
- [Bidaw, FAST 2026](https://www.usenix.org/conference/fast26/presentation/hu-shipeng)

### 官方系统与设计文档

- [vLLM Router](https://vllm.ai/blog/2025-12-13-vllm-router-release)
- [vLLM Prefix Caching](https://docs.vllm.ai/en/latest/design/prefix_caching/)
- [SGLang Model Gateway](https://github.com/sgl-project/sglang/blob/main/sgl-model-gateway/README.md)
- [NVIDIA Dynamo KV-aware Routing](https://docs.nvidia.com/dynamo/knowledge-base/concepts/system-architecture/kv-aware-routing)
- [llm-d KV Management](https://github.com/llm-d/llm-d/blob/main/docs/architecture/advanced/kv-management/README.md)
- [LMCache Technical Report](https://lmcache.ai/tech_report.pdf)

### 工程博客与实测

- [Ray Serve: Prefix-aware Request Routing](https://www.anyscale.com/blog/ray-serve-faster-first-token-custom-routing)
- [Ray Serve: Beyond KV Reuse to Token-load Awareness](https://www.anyscale.com/blog/llm-kv-token-aware-routing)
- [Baseten: Dynamo KV Routing Production Benchmark](https://www.baseten.co/blog/how-baseten-achieved-2x-faster-inference-with-nvidia-dynamo/)
- [vLLM × Mooncake: Agentic Workload Benchmark](https://vllm.ai/blog/2026-05-06-mooncake-store)
- [LMCache P2P Architecture and PoC](https://blog.lmcache.ai/en/2026/01/21/p2p-1/)
- [llm-d KV-aware Routing Benchmark](https://markellr-redhat.github.io/llm-d-benchmark/)
- [StorageReview: Long-context KV Offload to Flash](https://www.storagereview.com/review/the-token-efficient-path-for-long-context-inference-kv-cache-offload-to-flash)
