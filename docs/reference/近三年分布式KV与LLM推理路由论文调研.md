# 近三年分布式 KV 与 LLM 推理路由论文调研

更新时间：2026-09-30

配套分析：[系统能力矩阵与工程缺口](./系统能力矩阵与工程缺口.md)

## 0. 调研口径

这份调研面向“可作为 vLLM/SGLang 外部插件部署的分布式 KV 智能路由与管理系统”，不是单纯的 KV 压缩算法列表。

- 时间范围：以 2023–2026 为主；FlexGen 等略超出严格三年边界的工作，仅因其奠定了后续分层存储设计而保留。
- 来源优先级：SOSP、OSDI、NSDI、FAST、USENIX ATC、SIGCOMM、ISCA、MLSys、ICLR、ICML 等正式会议；少量未正式发表但直接影响 vLLM/SGLang/Dynamo 设计的工作单独标记为“预印本/工业系统”。
- 阅读口径：逐篇核对了原文摘要、问题定义、主要设计、实现路径和核心评测；这里是面向工程选型的压缩总结，不代替逐段复现论文。
- 相关性：A 表示直接决定路由器/分布式 KV 管理设计；B 表示可复用其机制；C 表示背景或未来扩展。

## 1. 先看结论：这批论文共同说明了什么

1. KV 路由不是一个孤立的 `argmin(score)`。它至少与请求排队、batch 形成、KV 驱逐、KV 传输、迁移、PD 解耦和 SLO admission 联动。
2. “缓存命中率最高”不等于“效果最好”。传输可能比重算慢，缓存丰富的实例可能已经过载，盲目等待缓存加载还可能造成 convoy。
3. 真正能跨负载取得稳定效果的系统通常不是堆指标，而是先找出新的控制变量：PagedAttention 的块、Sarathi 的 chunk、Llumnix 的 migration、Mooncake 的全局 KV 池、Preble 的全局 prefix tree、Libra 的 micro-request。
4. 工程插件最有价值的位置，是成为“跨引擎控制面”：统一收集缓存、队列、传输和生命周期事件，允许替换策略，并在 vLLM/SGLang 上提供一致的回退语义。
5. 评测不能只用 ShareGPT 和平均延迟。必须包含 prefix sharing、multi-turn、agent fan-out、长短混合、突发、低复用负对照，并同时报告 TTFT、TPOT、SLO goodput、重复 prefill、KV 网络流量和控制开销。

---

## 2. 第一组：基础引擎与单实例 KV 管理

### 1. vLLM / PagedAttention — SOSP 2023，相关性 A

论文：[Efficient Memory Management for Large Language Model Serving with PagedAttention](https://doi.org/10.1145/3600006.3613165)

- **解决什么问题**：每个请求的 KV 长度动态增长，连续内存预留会产生内部碎片、外部碎片和过度预留，限制 batch size。
- **为什么重要**：KV 内存利用率直接决定单实例并发度；后续绝大多数 KV 路由、迁移和 offload 都建立在 block 化 KV 之上。
- **核心思想**：借鉴虚拟内存，把每个序列的逻辑 KV 空间映射到固定大小的非连续物理块；通过 block table 寻址，并支持 copy-on-write 和跨请求共享。
- **如何实现**：自定义 PagedAttention kernel 读取 block table；集中式 block allocator 按需分配/回收；scheduler 结合 continuous batching 管理请求生命周期。
- **对本工程的启发**：路由层的最小资源单位应该尽可能对齐引擎 block，而不是只保存“某请求在某卡上”；但插件不应复制 vLLM 的 allocator，而应消费标准化 block/prefix 事件。

### 2. FastServe — 2023，分布式推理调度代表工作，相关性 B

论文：[Fast Distributed Inference Serving for Large Language Models](https://arxiv.org/abs/2305.05920)

- **解决什么问题**：run-to-completion 使短请求被长请求阻塞，导致严重的平均和尾延迟。
- **为什么重要**：路由只能决定初始放置，运行时负载仍可能变化；若不能抢占或迁移，初始路由错误会一直放大。
- **核心思想**：利用逐 token 自回归特性做 token 粒度抢占；提出 skip-join MLFQ，根据输入长度选择初始队列，随后依据服务时间调整优先级。
- **如何实现**：抢占请求时把中间状态在 GPU 与主机内存之间主动换出/换入；分布式 scheduler 管理执行和恢复。
- **对本工程的启发**：需要区分“placement policy”和“recovery/rescheduling policy”；仅有入口负载均衡不足以应对长尾。

### 3. FlexGen — ICML 2023，相关性 B

论文：[FlexGen: High-Throughput Generative Inference of Large Language Models with a Single GPU](https://proceedings.mlr.press/v202/sheng23a.html)

- **解决什么问题**：在 GPU 内存不足时，模型权重、激活和 KV 无法全部驻留 GPU。
- **为什么重要**：它较早系统化展示了 GPU/CPU/磁盘三层之间的张量放置与 I/O 编排，影响后续 KV 分层存储系统。
- **核心思想**：将权重、激活、KV 的放置比例与流水策略建模为线性规划，最大化离线吞吐；配合 4-bit 权重和 KV 压缩。
- **如何实现**：固定执行计划，在 GPU、CPU、磁盘间流水搬运张量，以大 batch 摊销 I/O。
- **对本工程的启发**：缓存层级选择应比较“加载时间与重算时间”，但 FlexGen 面向 latency-insensitive 离线场景，其静态计划不能直接用于在线路由。

### 4. SGLang / RadixAttention — 2024 技术报告与开源系统，相关性 A

论文/系统：[SGLang: Efficient Execution of Structured Language Model Programs](https://arxiv.org/abs/2312.07104)，[官方设计介绍](https://www.lmsys.org/blog/2024-01-17-sglang/)

- **解决什么问题**：复杂 LLM 程序包含共享系统提示、few-shot 示例、分支和多轮调用，而普通 serving API 把每次调用当作独立请求。
- **为什么重要**：严格前缀复用是当前 SGLang 及其 router 的基础，也是本项目最直接的兼容目标之一。
- **核心思想**：RadixAttention 用 radix tree 同时表达 prefix 索引、KV 缓存和 LRU 驱逐；frontend DSL 暴露程序结构，backend 自动复用 KV。
- **如何实现**：token 序列按 radix tree 插入与匹配；节点引用计数保护运行中 KV；空闲叶节点按 LRU 回收；运行时执行 continuous batching。
- **对本工程的启发**：可以复用 radix/prefix tree 作为统一逻辑索引，但集群级索引必须解决多副本、事件延迟、容量与故障，而不能简单复制单机 tree。

### 5. Sarathi-Serve — OSDI 2024，相关性 A

论文：[Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve](https://www.usenix.org/conference/osdi24/presentation/agrawal)

- **解决什么问题**：大 prefill 会阻塞 decode，continuous batching 又容易形成 decode-maximal batch，造成 TPOT 抖动和 pipeline bubble。
- **为什么重要**：路由层估算实例负载时，不能只数请求；一个 64K prefill 和一个 decode 请求的干扰完全不同。
- **核心思想**：把 prefill 切成固定 token budget 的 chunks，并让 decode piggyback 到 chunked prefill batch 上，形成 stall-free scheduling。
- **如何实现**：scheduler 每轮先安排 decode，再用剩余 token budget 填充 prefill chunks；流水并行时构造更均匀的 microbatch。
- **对本工程的启发**：负载接口至少需要 active prefill tokens、chunk 进度和 decode batch 状态；否则外部 router 的负载估计会系统性失真。

### 6. Llumnix — OSDI 2024，相关性 A

论文：[Llumnix: Dynamic Scheduling for Large Language Model Serving](https://www.usenix.org/conference/osdi24/presentation/sun-biao)

- **解决什么问题**：请求长度和到达不可预测，静态入口路由会导致实例间负载失衡、优先级倒置和扩缩容困难。
- **为什么重要**：它证明了“初始路由之后仍可纠错”能显著降低尾延迟；这是一个工程级管理器应具备的能力方向。
- **核心思想**：利用 KV 只追加的特点实现在线请求 live migration；以 virtual usage 统一表达负载均衡、优先级和内存效率目标。
- **如何实现**：在源 GPU 继续 decode 时流水复制已有 KV，再追赶增量 KV，最后短暂停顿完成切换；采用全局与本地两级调度架构。
- **对本工程的启发**：路由器可先做非侵入式 placement，后续通过可选 migration adapter 增强；不能把迁移当免费动作，必须建模 KV 大小和链路拥塞。

---

## 3. 第二组：分布式路由、PD 解耦与集群调度

### 7. Splitwise — ISCA 2024，相关性 B

论文：[Splitwise: Efficient Generative LLM Inference Using Phase Splitting](https://www.microsoft.com/en-us/research/publication/splitwise-efficient-generative-llm-inference-using-phase-splitting/)

- **解决什么问题**：prefill 计算密集，decode 内存带宽密集，把两者放在同类机器上会产生资源错配。
- **为什么重要**：它奠定了 PD disaggregation 的资源配置逻辑，KV 传输成为路由必须显式考虑的成本。
- **核心思想**：把 prompt 和 token generation 放到不同机器池，分别配置硬件、容量和并行策略。
- **如何实现**：prefill 完成后通过高速集群互连传输 KV；用 trace-driven simulator 搜索吞吐、成本和功耗配置。
- **对本工程的启发**：即使当前不做异构，也应保留 phase/worker-role 抽象；但第一版不必绑定 PD，避免把部署复杂性变成前提。

### 8. DistServe — OSDI 2024，相关性 A

论文：[DistServe: Disaggregating Prefill and Decoding for Goodput-optimized LLM Serving](https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin)

- **解决什么问题**：prefill 与 decode colocate 会相互干扰，并把 TTFT 与 TPOT 两种 SLO、两阶段并行策略绑死。
- **为什么重要**：它把优化目标从纯 throughput 转向同时满足 TTFT/TPOT 的 per-GPU goodput，是工程评测的重要范式。
- **核心思想**：物理解耦 P/D，分别搜索资源数量和 parallelism；依据集群带宽做阶段 colocating，控制 KV 传输开销。
- **如何实现**：离线 profiling 建性能模型，再搜索满足两个 SLO 的部署计划；在线分别在 prefill/decode 池调度。
- **对本工程的启发**：路由策略应输出可解释的 predicted TTFT/TPOT 或 SLO risk，而不是只优化平均分数。

### 9. Preble — ICLR 2025，相关性 A

论文：[Preble: Efficient Distributed Prompt Scheduling for LLM Serving](https://proceedings.iclr.cc/paper_files/paper/2025/hash/5bc342f48de8264779952fac378f96dc-Abstract-Conference.html)

- **解决什么问题**：前缀缓存只在单 GPU 内有效；集群中负载均衡会破坏 affinity，而强 affinity 又会制造热点。
- **为什么重要**：这是与“分布式 KV 智能路由”最接近的公开系统之一，必须作为主要基线和排重对象。
- **核心思想**：全局 prefix tree 跟踪各 GPU 上的共享前缀及活跃请求；用 E2 调度在复用收益和执行负载之间决策，并按需复制热点前缀。
- **如何实现**：global scheduler 做跨 GPU placement，local scheduler 形成 batch；prefix 节点记录位置、访问和 eviction cost；依据负载与 prefix 热度扩缩副本。
- **对本工程的启发**：不能只复刻 cache/load balance；应在它之上加入更完整的生命周期、数据移动、SLO 或多策略自适应能力。

### 10. Mooncake — FAST 2025，相关性 A

论文：[Mooncake: Trading More Storage for Less Computation — A KVCache-centric Architecture for Serving LLM Chatbot](https://www.usenix.org/conference/fast25/presentation/qin)

- **解决什么问题**：本地 HBM/DRAM 容量不足以保留大量多轮会话 KV，重算成本高，P/D 干扰又限制 goodput。
- **为什么重要**：它展示了生产规模下把 KV 当作集群一等资源、用分布式存储换计算的完整架构。
- **核心思想**：将 KV 存储、prefill 和 decode 解耦；使用全局 KV 池与 transfer engine；scheduler 根据 SLO、负载和缓存位置进行路由与 admission。
- **如何实现**：分布式 memory pool 聚合 GPU/CPU/SSD；以 request ID/prefix 管理 KV；异步传输并在无法满足 SLO 时拒绝请求。
- **对本工程的启发**：控制面可以借鉴其 cache-centric 视角，但插件应避免强绑定专用存储，先通过 backend SPI 接入 LMCache/Mooncake/NIXL 等。

### 11. LMetric — OSDI 2026，相关性 A

论文：[Simple Is Better: Multiplication May Be All You Need for LLM Request Scheduling](https://www.usenix.org/conference/osdi26/presentation/zhang-dingyan)

- **解决什么问题**：缓存 affinity 与负载均衡常通过带超参数的线性组合，跨工作负载、模型和硬件难以调参。
- **为什么重要**：它是对“做一个复杂智能打分器”最直接的反例，并且已在生产中验证。
- **核心思想**：使用“路由后新增 prefill token 数 × 当前 batch size”作为 score；乘法比较中公共系数抵消，避免超参数。
- **如何实现**：从缓存索引得到新增 token，从实例状态得到 batch size，对候选实例计算乘积并选最小；论文还给出失效条件和检测方式。
- **对本工程的启发**：LMetric 应成为强默认基线。新系统的价值必须来自更准确的状态、更多动作或闭环控制，而不是换一种权重组合。

### 12. EcoServe — OSDI 2026，相关性 B

论文：[Efficient LLM Serving on Commodity GPU Clusters with Data-Reduced Cross-Instance Orchestration](https://www.usenix.org/conference/osdi26/presentation/du)

- **解决什么问题**：colocate 有 P/D 干扰，完全 PD 解耦又依赖昂贵高速网络，不适合普通以太网 GPU 集群。
- **为什么重要**：工程用户并不总有 NVLink/InfiniBand；系统必须根据真实网络条件选择协作强度。
- **核心思想**：PaDG 在单实例内按时间切分 P/D，并让多个实例周期协作，减少跨实例数据量；macro-instance 作为调度单元。
- **如何实现**：循环激活多个实例保持 prefill 可用，adaptive scheduler 在 macro-instance 内路由，mitosis 机制做细粒度扩缩容。
- **对本工程的启发**：数据移动策略必须 topology/bandwidth-aware；“不传 KV、少传 KV、全量传 KV”应是可选动作而非固定架构。

### 13. Libra — NSDI 2026，相关性 A

论文：[Libra: Flexible Request Partitioning and Scheduling for Serving Unbalanced and Dynamic LLM Workloads](https://www.usenix.org/conference/nsdi26/presentation/ruan-libra)

- **解决什么问题**：请求输入/输出长度高度不平衡，固定 colocated 或固定 PD 都难以稳定兼顾尾延迟与吞吐。
- **为什么重要**：它说明路由单位未必必须是整个请求，长请求可以被拆分后跨实例执行。
- **核心思想**：micro-request abstraction 可在任意 token 边界切分一个请求；两级 scheduler 决定切分点和本地 SLO-aware batch。
- **如何实现**：全局 scheduler 选择 segment 分配，本地 scheduler 形成 batch；通过 chunked KV transfer 让后续 segment 在其他实例继续。
- **对本工程的启发**：第一版可只做 whole-request 路由，但内部数据模型应允许 segment/chunk 级成本和位置，避免未来重构。

### 14. SYMPHONY — NSDI 2026，相关性 A

论文：[SYMPHONY: Enabling Compute-Memory Disaggregation in LLM Serving Systems](https://www.usenix.org/conference/nsdi26/presentation/agarwal)

- **解决什么问题**：多轮会话 KV 要么被重算，要么被被动 offload；恢复时的数据移动落在关键路径上并造成负载失衡。
- **为什么重要**：它展示了应用层弱提示如何帮助 KV 管理，但同时承认预测信号不可靠。
- **核心思想**：用来自用户交互或工作流结构的 advisory requests 提前预取；通过 priority-based 与 cooperative memory management 处理误预测和 GPU 内存竞争。
- **如何实现**：应用发出未来访问提示；memory layer 优先放置高价值 KV；与 serving engine 协作动态回收/预取。
- **对本工程的启发**：插件可提供 optional hints API，但正确性和基本性能不能依赖调用方一定提供准确提示。

---

## 4. 第三组：KV 分层存储、压缩、传输与复用

### 15. CacheGen — SIGCOMM 2024，相关性 A

论文：[CacheGen: KV Cache Compression and Streaming for Fast Large Language Model Serving](https://cs.stanford.edu/~keithw/sigcomm2024/sigcomm24-final1571-acmpaginated.pdf)

- **解决什么问题**：远端复用长上下文能节省 prefill，但完整 KV 很大，网络获取可能比本地重算更慢。
- **为什么重要**：它明确打破“只要有远端 KV 就应该加载”的假设，为路由器提供了 transfer/recompute 决策基础。
- **核心思想**：利用 KV 分布特性编码为紧凑 bitstream；根据实时带宽为不同 chunks 选择压缩等级，必要时发送文本并本地重算。
- **如何实现**：离线/在线生成多档编码；chunk 流式传输；用上一 chunk 的吞吐估计带宽并调整后续动作。
- **对本工程的启发**：候选动作应该同时包括 fetch 和 recompute；网络预测、分块传输、渐进式降级值得直接吸收。

### 16. CachedAttention — USENIX ATC 2024，相关性 A

论文：[Cost-Efficient Large Language Model Serving for Multi-turn Conversations with CachedAttention](https://www.usenix.org/conference/atc24/presentation/gao-bin-cost)

- **解决什么问题**：多轮会话反复把历史拼回 prompt，导致重复 prefill；KV 放在慢速介质后又引入访问延迟。
- **为什么重要**：multi-turn 是最稳定、最常见的 KV 重用来源，不能只依赖“热门公共前缀”。
- **核心思想**：跨轮持久化历史 KV；用 layer-wise preload 和异步保存把 I/O 与 GPU 计算重叠；queue-aware 策略改善缓存管理。
- **如何实现**：按层加载即将计算的 KV，当前层计算时预取后续层；请求结束异步保存；缓存策略结合等待队列信息。
- **对本工程的启发**：router 应区分 conversation continuation 与偶然前缀命中，并允许 session hint；但不能用硬 sticky session 代替负载判断。

### 17. InfiniGen — OSDI 2024，相关性 B

论文：[InfiniGen: Efficient Generative Inference of Large Language Models with Dynamic KV Cache Management](https://www.usenix.org/conference/osdi24/presentation/lee)

- **解决什么问题**：长文本 decode 的完整 KV offload/prefetch 流量过大，主机链路成为瓶颈。
- **为什么重要**：并非所有 KV token 在每一步注意力中都同等重要；路由与存储层未来可利用“有效工作集”而非原始字节数。
- **核心思想**：用当前层输入、部分 query 权重和下一层 key cache 做轻量 rehearsal，预测下一层的重要 KV，只预取必要部分。
- **如何实现**：层间流水预测重要 token index，再从 CPU 拉取稀疏 KV；与 offloading engine 集成。
- **对本工程的启发**：第一版应保持 exact reuse，不引入质量风险；但容量/传输估计接口应允许 backend 报告压缩或稀疏后的实际成本。

### 18. IMPRESS — FAST 2025，相关性 B

论文：[IMPRESS: An Importance-Informed Multi-Tier Prefix KV Storage System](https://www.usenix.org/conference/fast25/presentation/chen-weijian-impress)

- **解决什么问题**：从 SSD 加载整个 prefix KV 会夹带大量不重要 token，浪费 I/O 和上层缓存容量。
- **为什么重要**：多层 KV 系统的关键不是逻辑 hit rate，而是每次 hit 实际产生多少有价值字节。
- **核心思想**：利用同层不同 attention heads 的重要 token index 高相似性，以少量 heads 快速识别重要 KV；按重要性重排、打包和放置。
- **如何实现**：重要性识别后将高价值 KV 聚集成 I/O-friendly chunks，并在 GPU/CPU/SSD 层级中采用 importance-aware caching。
- **对本工程的启发**：缓存元数据可以携带 value density/estimated saved compute，而不仅是长度与最近访问时间。

### 19. Mooncake Store — FAST 2025，相关性 A

与第 10 项同一论文，这里强调存储实现：

- **核心实现点**：全局内存池、RDMA/NVLink 等多传输后端、KV 对象生命周期、异步传输、P/D 数据路径解耦。
- **借鉴重点**：插件的 storage backend 和 transport backend 应解耦；逻辑 router 不应直接依赖某一种 RDMA 库。

### 20. KVCache Cache in the Wild — USENIX ATC 2025，相关性 A

论文：[KVCache Cache in the Wild](https://www.usenix.org/conference/atc25/presentation/wang-jiahao)

- **解决什么问题**：此前系统大量依赖 synthetic traces，对真实云端 KV reuse 的来源、间隔和容量需求缺乏认识。
- **为什么重要**：这是定义默认策略、trace generator 和工程 benchmark 的关键实证依据。
- **核心发现**：复用高度倾斜；single-turn 间复用与 multi-turn 同样重要；全局复用间隔差异大，但按请求类别后更可预测；接近理想 hit ratio 所需容量并非无限大。
- **如何实现**：分析大型云厂商两段生产请求轨迹，建立分类感知的 reuse prediction 与 workload-aware eviction。
- **对本工程的启发**：需要按 workload class 学习策略，而不是全局一个阈值；分类必须可插拔且未知类别要安全回退。

### 21. Strata — OSDI 2026，相关性 A

论文：[Strata: Hierarchical Context Caching for Long Context Language Model Serving](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang)

- **解决什么问题**：分层 KV 中碎片化布局造成小 I/O，加载阻塞 prefill，多个相同请求在 miss 期间形成 delayed hits，scheduler 又忽略加载时延。
- **为什么重要**：它表明 KV 存储布局、I/O 和 request scheduling 必须协同，单纯增加缓存层可能反而变慢。
- **核心思想**：GPU-assisted I/O 解耦 GPU 与 host layout，形成大粒度高带宽传输；cache-aware scheduler 隐藏加载并处理 delayed hit。
- **如何实现**：GPU 参与 gather/scatter；scheduler 按加载延迟平衡 batch，重叠互补计算；集成进 SGLang 并生产部署。
- **对本工程的启发**：Strata 是分层缓存场景的强基线；外部插件必须读取 loading 状态和 ETA，不能把低层命中当成立即可用的 GPU 命中。

### 22. Bidaw — FAST 2026，相关性 A

论文：[Bidaw: Enhancing Key-Value Caching via Bidirectional Computation–Storage Awareness](https://www.usenix.org/conference/fast26/presentation/hu-shipeng)

- **解决什么问题**：CPU/SSD 两层历史 KV 缓存与 compute scheduler 各自优化，导致加载阻塞、host hit 率低和无效 KV 占用。
- **为什么重要**：它展示了真正的闭环：计算层知道数据位置，存储层也利用生成结果预测后续访问。
- **核心思想**：按 KV 所在层分队列并按 KV 大小重排，降低 blocking；用回答长度等生成信息预测再访问，指导 eviction；选择性保存 storage-efficient histories。
- **如何实现**：compute/storage 双向暴露元数据；dual queues 分离不同加载延迟请求；prediction 驱动 host/SSD 放置。
- **对本工程的启发**：非常适合作为工程架构参考：router 不仅消费 cache 状态，还应把路由、完成和生成统计反馈给 cache manager。

### 23. CacheSlide — FAST 2026，相关性 B

论文：[CacheSlide: Unlocking Cross Position-Aware KV Cache Reuse](https://www.usenix.org/conference/fast26/presentation/liu-yang)

- **解决什么问题**：agent prompt 中固定片段的绝对位置会随动态片段改变，严格 prefix cache 无法复用，完全位置无关复用又有较大精度/计算代价。
- **为什么重要**：它扩展了“可复用”的定义，说明未来 KV manager 不应永久局限于从 token 0 开始的相同前缀。
- **核心思想**：识别相对次序稳定但绝对位置变化的 RPDC；通过位置编码修正、少量重算和加权校正复用 KV。
- **如何实现**：在 vLLM 上加入 Chunked Contextual Position Encoding、Weighted Correction Attention、逐层及 spill-aware 管理。
- **对本工程的启发**：第一版仍应坚持 exact prefix 以保证通用兼容；non-prefix/position-shifted reuse 作为 capability backend，而不是写死在核心 router。

### 24. ECHO — OSDI 2026，相关性 B

论文：[ECHO: Efficient KV Cache Offloading with Lossless Prefetching for Native Sparse Attention LLMs](https://www.usenix.org/conference/osdi26/presentation/liu-guangda)

- **解决什么问题**：native sparse attention 减少 attention 计算与访问，却仍产生巨大 KV，HBM 限制并发；普通 offload 的 recall 延迟又高。
- **为什么重要**：模型 attention 机制会改变 KV 工作集和管理粒度，插件需要能力协商，不能假设所有模型都使用相同 dense attention 行为。
- **核心思想**：graph-friendly cache manager 在 CUDA Graph 内完成动态 eviction/recall；利用 index score 可预测性做无损 prefetch。
- **如何实现**：decode 做 intra-query prefetch，prefill 做 inter-query prefetch；融合 GPU kernel 将 recall 与 indexer 计算流水重叠。
- **对本工程的启发**：把 backend 能力、prefetch 支持和实际 materialization latency 纳入统一接口，而不是由 router 猜测。

### 25. DroidSpeak — NSDI 2026，相关性 C（当前不做异构模型，但值得知道）

论文：[DroidSpeak: KV Cache Sharing Across Fine-tuned Model Variants](https://www.usenix.org/conference/nsdi26/presentation/liu-yuhan)

- **解决什么问题**：相同架构的多个微调模型处理同一上下文时，传统系统不能跨模型复用 KV。
- **为什么重要**：它展示了 KV identity 不一定等同于 model ID；但跨模型复用涉及质量边界，当前工程可暂不进入。
- **核心思想**：实证分析哪些层可安全复用，只重算少量敏感层；流水重算层与加载复用层。
- **如何实现**：按层选择 reuse/recompute，并跨节点传输；用任务指标约束质量损失。
- **对本工程的启发**：元数据 schema 应带 model fingerprint 和兼容域；当前默认要求完全一致，未来再扩展 compatibility plugin。

---

## 5. 第四组：应用结构、agent 与真实工作负载

### 26. Parrot — OSDI 2024，相关性 B

论文：[Parrot: Efficient Serving of LLM-based Applications with Semantic Variable](https://www.usenix.org/conference/osdi24/presentation/lin-chaofan)

- **解决什么问题**：服务端只看到孤立 API 请求，看不到 LLM 应用的变量依赖、并行关系和共享输入。
- **为什么重要**：很多可优化信息根本无法从 token prefix 和当前负载推断出来；应用结构是一种额外但可选的信号。
- **核心思想**：Semantic Variable 显式连接多次 LLM 调用，形成数据流图；服务端据此做并行、共享与调度优化。
- **如何实现**：frontend API 标注 semantic variables，compiler/runtime 构造 DAG，backend 跨请求优化执行。
- **对本工程的启发**：提供 hints/relationship API，但保持 OpenAI-compatible 无提示路径；插件不能要求用户改写所有应用。

### 27. Agentix（早期名 Autellix）— NSDI 2026，相关性 B

论文：[Agentix: An Efficient Serving Engine for LLM Agents as General Programs](https://www.usenix.org/conference/nsdi26/presentation/luo)

- **解决什么问题**：传统 scheduler 只优化单次调用，agent 程序的多次依赖调用累计排队，产生 call-level 和 program-level HoL blocking。
- **为什么重要**：工程上的“整体效果”未来越来越由 agent 完成时间而不是单请求 TTFT 决定。
- **核心思想**：把 program 作为一等调度实体；根据同一程序已获得的累计服务量进行抢占和优先级分配，同时利用 program locality 路由。
- **如何实现**：代理层拦截请求并维护 global process table；单线程程序采用 PLAS，分布式程序采用 critical-path-aware ATLAS；与 vLLM 等后端结合。
- **对本工程的启发**：核心 router 可保留 tenant/session/program 三种 scope；第一版只消费可选 ID，不必立即实现完整 agent scheduler。

### 28. ServeGen — NSDI 2026，相关性 A

论文：[ServeGen: Workload Characterization and Generation of LLM Serving in Production](https://www.usenix.org/conference/nsdi26/presentation/xiang-servegen)

- **解决什么问题**：公开 workload 对生产语言、多模态和推理服务的 client-level burst、长度与相关性刻画不足，导致系统评测结论不可靠。
- **为什么重要**：如果 benchmark 不真实，再复杂的路由策略也可能只对合成分布有效。
- **核心思想**：基于全球云服务真实轨迹，按 client 建模并组合生成工作负载，而不是独立采样 prompt/output 长度。
- **如何实现**：提取每 client 的到达、长度、模型类型与相关性分布，组合成可重放 workload generator，并验证其性能曲线接近真实轨迹。
- **对本工程的启发**：评测框架应该成为项目的一等组件；至少接入 ServeGen、真实 conversation traces 与可控 prefix-sharing synthetic generator。

### 29. KVCache Cache in the Wild — ATC 2025，相关性 A

见第 20 项。应用侧额外启发是：single-turn 复用不可忽略，不能把 session affinity 当作 KV reuse 的全部来源。

### 30. Seer — OSDI 2026，相关性 C

论文：[Seer: Online Context Learning for Fast Synchronous LLM Reinforcement Learning](https://www.usenix.org/conference/osdi26/presentation/qin)

- **解决什么问题**：同步 RL rollout 存在重尾长度和 straggler，整个 iteration 被尾部请求拖慢。
- **为什么重要**：它提供了一种从“同 prompt 历史行为”学习输出长度与响应模式的方式，可迁移到路由成本预测。
- **核心思想**：利用共享 prompt 请求的长度/模式相关性，组合 divided rollout、context-aware scheduling 和 adaptive grouped speculative decoding。
- **如何实现**：在线维护 context statistics，动态拆分/重分配 rollout，并为相似请求选择成组推测策略。
- **对本工程的启发**：预测器可按 prefix/workload class 建模，而不是只按全局模型平均值；预测必须有置信度与无预测回退。

---

## 6. 第五组：KV 压缩与稀疏化——不应成为第一版核心，但接口要兼容

### 31. Rethinking KV Cache Compression — MLSys 2025，相关性 B

论文：[Rethinking Key-Value Cache Compression Techniques for LLM Serving](https://proceedings.mlsys.org/paper_files/paper/2025/hash/26289c647c6828e862e271ca3c490486-Abstract-Conference.html)

- **解决什么问题**：大量 KV 压缩论文只报告模型质量或 attention microbenchmark，未必转化为真实 serving throughput。
- **为什么重要**：提醒工程系统不能把“压缩率”直接当作“容量提升”；索引、选择、重排和不规则访问都有成本。
- **核心思想**：从端到端 serving 角度系统复核主流 token eviction/quantization 方法，分析理论节省与实际性能之间的差距。
- **如何实现**：在统一运行时和 workload 下比较内存、kernel、batch size、吞吐与质量。
- **对本工程的启发**：router 只信 backend 报告的 measured effective capacity/latency，不根据名义压缩比自行推算。

### 32. ShadowKV — ICML 2025，相关性 B

论文：[ShadowKV: KV Cache in Shadows for High-Throughput Long-Context LLM Inference](https://proceedings.mlr.press/v267/sun25b.html)

- **解决什么问题**：长上下文 KV 占用大且 decode 读取带宽高，简单 offload 受 PCIe 限制。
- **为什么重要**：展示了低秩/稀疏结构与 host-GPU 协同可改变 KV 的实际存储和传输成本。
- **核心思想**：保留紧凑低秩“shadow”表示，并按需重构关键 KV；结合稀疏选择减少 GPU resident 数据。
- **如何实现**：CPU 保存压缩状态，GPU 根据 query 选择并重构活跃 token；优化 kernel 与数据流水。
- **对本工程的启发**：将压缩实现视为 storage capability；路由层使用其真实 fetch/reconstruct cost。

### 33. KVQuant / MiniCache / Coupled Quantization — NeurIPS 2024，相关性 C

论文：[KVQuant](https://proceedings.neurips.cc/paper_files/paper/2024/file/028fcbcf85435d39a40c4d61b42c99a4-Paper-Conference.pdf)、[MiniCache](https://proceedings.neurips.cc/paper_files/paper/2024/file/fd0705710bf01b88a60a3d479ea341d9-Paper-Conference.pdf)、[Coupled Quantization](https://proceedings.neurips.cc/paper_files/paper/2024/hash/05d6b5b6901fb57d2c287e1d3ce6d63c-Abstract-Conference.html)

- **解决什么问题**：KV 随 batch 和上下文线性增长，限制并发并提高 HBM 读取开销。
- **核心思想**：KVQuant 做低比特量化；MiniCache 利用相邻层相似性做 depth 维压缩；CQ 利用 channel 间依赖进行联合编码。
- **如何实现**：量化/合并规则与定制 attention/dequant kernel 协同，平衡质量和吞吐。
- **对本工程的启发**：不要第一版自研近似压缩；通过 capability flags 支持 backend 返回 `physical_bytes`、`quality_mode` 和可用 kernel。

---

## 7. 最新预印本和工业系统：用于趋势判断与排重

这些工作与方向高度相关，但不应冒充已经过顶会评审的结论。

### 34. CacheRoute — 2026 预印本，相关性 A

论文：[CacheRoute: Planned Prefix-Affinity Routing for Large-Scale LLM Serving](https://arxiv.org/abs/2608.19677)

- **问题**：纯响应式路由使 prefix 在实例间漂移，热点副本数不稳定。
- **思想/实现**：周期性估计高频 key，建立稳定 warm set；按访问率规划副本数，用 LPT placement 放置，并通过 shadow replay 平滑切换。
- **启发**：长期 cache placement 与逐请求 routing 应是两个控制环；但论文也显示低复用 workload 可能明显输给 flat load balance，因此必须有自动退化机制。

### 35. DualMap — 2026 预印本，相关性 A

论文：[DualMap: SLO-aware Distributed KV Cache Routing](https://arxiv.org/abs/2602.06502)

- **问题**：单一 prefix→worker 映射在热点与负载变化时容易失衡。
- **思想/实现**：维护两个独立映射，用 power-of-two choices 在 cache locality 与负载之间选择；配合 SLO routing、热点迁移和扩缩容。
- **启发**：多候选副本比单 affinity 更鲁棒；本工程可把候选生成与最终决策分离。

### 36. Feather — 2026 预印本，相关性 B

论文：[Requests of a Feather Must Flock Together](https://arxiv.org/abs/2605.06046)

- **问题**：最大 batch 不一定最快；共享前缀的同质小 batch 可能因 KV locality 获得更高 decode throughput，radix tree 遍历本身还有 CPU 开销。
- **思想/实现**：RL 学习 batch size 与 prefix homogeneity 的权衡；用 Chunked Hash Tree 加速 prefix 检测和请求选择。
- **启发**：外部 router 的放置会改变本地可形成的 batch，cluster routing 与 local batching 不应完全割裂。

### 37. LMCache — 工业开源系统/技术报告，相关性 A

系统：[LMCache](https://docs.lmcache.ai/)

- **问题**：引擎结束请求或换实例后，HBM 中 KV 很快丢失，历史上下文必须重算。
- **思想/实现**：提供独立 KV storage/transfer layer，将 KV offload 到 CPU、磁盘或远端；通过 connector 与 vLLM 等引擎集成，并支持 controller 与跨实例查找。
- **启发**：它适合作为本工程的一个存储 backend，而不是竞争对象；你的价值应放在统一路由、策略、状态闭环和多 backend 编排上。

### 38. NVIDIA Dynamo Router / llm-d — 工业开源系统，相关性 A

系统：[Dynamo Router](https://github.com/ai-dynamo/dynamo/blob/main/components/src/dynamo/router/README.md)、[Dynamo Routing Tuning](https://github.com/ai-dynamo/dynamo/blob/main/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md)、[llm-d KV Management](https://github.com/llm-d/llm-d/blob/main/docs/architecture/advanced/kv-management/README.md)

- **问题**：生产集群需要引擎无关的缓存索引、负载跟踪、KV-aware routing、P2P sharing 和 PD 编排。
- **思想/实现**：Dynamo 消费 KV events，维护 exact/approx index，支持预测 TTL、队列、策略插件和多级缓存权重；llm-d 通过 EPP、KV indexer、offloader 和 NIXL P2P 组成控制面。
- **启发**：这是工程上必须超越或差异化的直接参照。兼容性本身已不是创新；真正空缺更可能在统一 lifecycle state、自动策略选择、可验证的 SLO cost model、跨层闭环和故障语义。

---

## 8. 从论文中抽取的“可复用方法库”

| 方法 | 来源 | 可放入本工程的位置 |
|---|---|---|
| block/prefix 级统一资源抽象 | vLLM、SGLang | Cache index schema |
| 全局 prefix tree + 两级调度 | Preble | Global controller + local adapter |
| 无参数强默认 score | LMetric | 默认 routing baseline |
| chunked prefill 状态 | Sarathi | Load estimator |
| online request migration | Llumnix | 可选 correction executor |
| fetch 与 recompute 动态选择 | CacheGen | Action planner |
| storage/computation 双向反馈 | Bidaw | Closed-loop telemetry |
| loading-aware batch | Strata | Local scheduling hint |
| 全局 KV 池与异步 transfer | Mooncake | Storage/transport SPI |
| SLO goodput 建模 | DistServe | Objective/evaluator |
| request segmentation | Libra | Future segment-level routing |
| advisory hints + safe fallback | SYMPHONY、Parrot | Optional application API |
| workload-class prediction | KVCache in the Wild、Seer | Predictor plugin |
| realistic client-level trace | ServeGen | Benchmark suite |
| 长期副本规划 + 短期请求路由 | CacheRoute | 双时间尺度 controller |

## 9. 下一阶段不应立即“想新算法”，而应先完成的工程调研

1. **复现基线**：vLLM Production Stack load-aware、SGLang cache-aware、Dynamo KV Router、Preble/LMetric 中至少三个。
2. **统一观测接口**：把 vLLM KVEvent、SGLang radix cache、队列、active tokens、batch、transfer 状态映射成同一 schema。
3. **建立 workload matrix**：低复用、稳定热点、突发共享前缀、多轮会话、长短混合、agent fan-out、网络受限。
4. **做 oracle 分析**：离线重放时让 oracle 知道真实服务时间、KV 位置与未来请求，测出现有策略与理论上限的 gap。
5. **只针对稳定 gap 设计机制**：如果差距来自状态缺失，就补状态；来自动作不足，就加迁移/复制/等待；来自估计错误，就做在线校准。不要先决定使用 RL、复杂权重或预测模型。

## 10. 当前最值得深读和复现的优先顺序

如果只先投入两周，我建议按下面顺序，而不是按发表年份阅读：

1. vLLM、SGLang：理解两个目标引擎的 KV/block/radix 生命周期。
2. Preble、LMetric：掌握最接近本项目的两类路由基线。
3. Mooncake、CacheGen、Strata、Bidaw：理解 KV 存储与传输为什么会改变最优路由。
4. Sarathi-Serve、Llumnix、DistServe、Libra：理解路由、batch、迁移和 SLO 的耦合。
5. KVCache in the Wild、ServeGen：建立真实 workload 与评测方法。
6. Dynamo、llm-d、LMCache：确定开源工程已有能力和插件落点。
7. CacheRoute、DualMap、Feather：仅用于最新排重和启发，不先假设其结论普适。
