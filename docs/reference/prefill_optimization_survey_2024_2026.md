# 近三年 LLM Prefill 阶段优化研究调研（2024–2026）

> 调研范围：主要覆盖 2024–2026 年发表于 OSDI、SOSP、EuroSys、FAST、ISCA、ASPLOS、SIGCOMM、SIGMOD、MLSys、NeurIPS、ICLR、USENIX ATC 等会议的代表性工作。
>
> 调研目标：厘清 Prefill 优化中哪些问题已经被较好解决、已有工作的核心假设与边界，以及仍需进一步用真实数据验证的研究空间。

## 1. 如何理解“Prefill 优化”

现有工作所说的 Prefill 优化并不是同一个问题，大致可以分为五类：

1. **调度 Prefill**：计算量不变，但减少长 Prefill 对其他请求的阻塞。
2. **拆分 Prefill**：把 Prefill 和 Decode 放在不同设备或资源池中。
3. **加速 Prefill**：通过并行化、稀疏化和优化 kernel 加速必须执行的计算。
4. **消除 Prefill**：复用已有 KV Cache，避免重新计算相同上下文。
5. **搬运 Prefill 结果**：从内存、SSD 或远端加载 KV Cache，同时解决 I/O 可能比重算更慢的问题。

判断一个研究问题是否有价值时，不能只看“缓存命中率”或“减少了多少 Prefill token”，还必须考虑：

- 节省的 GPU 计算是否被网络或存储 I/O 抵消；
- 优化的是 TTFT、TPOT、吞吐、goodput、成本还是能耗；
- 算法是否要求严格相同的前缀；
- 是否改变模型精度或输出结果；
- 是否只在合成 workload 上成立；
- 在高并发和尾延迟条件下是否仍然有效。

---

## 2. Prefill 调度与 Prefill–Decode 干扰

### 2.1 Sarathi-Serve — OSDI 2024

论文：[Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve](https://www.usenix.org/conference/osdi24/presentation/agrawal)

**解决了什么问题**

长 Prefill 与正在执行的 Decode 混跑时，会造成明显的 generation stall。在流水线并行中，不同长度的 Prefill 还会导致各 micro-batch 执行时间差异过大，形成 pipeline bubble。

**为什么重要**

优先 Prefill 可以提高吞吐，却会恶化正在生成请求的 TPOT；优先 Decode 可以改善交互体验，却会让新请求的 Prefill 等待。传统调度器难以同时兼顾吞吐和尾延迟。

**核心思想**

不要把整个 Prompt 当作不可分割的计算。将 Prefill 切分成计算量近似相同的小块，再把 Prefill chunk 与 Decode token 混合到同一个 batch 中。

**如何设计**

- 使用 chunked prefill 将长 Prompt 分多轮执行；
- 使用 stall-free scheduling，将正在执行的 Decode 与一个或多个 Prefill chunk 合并；
- 通过 token budget 控制每轮 batch 的计算规模；
- 降低 Prefill 对 Decode 的阻塞，并减少流水线气泡。

论文在不同模型和并行配置下报告约 2.6–5.6 倍的 serving capacity 提升。

**边界与成熟度**

Chunked prefill 已成为现代推理框架的基础能力。单纯继续研究“如何切 chunk”通常创新空间有限，除非面对 MoE、稀疏注意力、异构 GPU 或新的 SLO 场景。

### 2.2 DistServe — OSDI 2024

论文：[DistServe: Disaggregating Prefill and Decoding for Goodput-optimized LLM Serving](https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin)

**解决了什么问题**

Prefill 通常是计算密集型，Decode 通常是显存带宽密集型。两者共置在相同 GPU 上会产生资源干扰，并被迫使用相同的资源分配与并行策略。

**为什么重要**

在线应用通常同时约束 TTFT 和 TPOT。共置系统往往只能优先满足其中一个指标，或者通过严重过配资源同时满足二者。

**核心思想**

把 Prefill 和 Decode 分配到不同 GPU，并分别选择适合各阶段的副本数量和并行策略。

**如何设计**

- 根据应用的 TTFT 与 TPOT SLO 分别规划两个阶段；
- 联合优化 Prefill/Decode 的资源数量和并行方式；
- 根据集群带宽和拓扑放置两个阶段，降低 KV Cache 传输成本；
- 使用 goodput，即满足两个延迟约束的请求速率，作为主要优化目标。

论文报告可支持 7.4 倍更多请求，或者支持 12.6 倍更严格的 SLO。

**边界与成熟度**

P/D 解耦的基本体系结构已经确立。后续研究主要转向异构资源规划、KV 传输、弹性伸缩和多阶段调度，而不是重新论证是否应该解耦。

### 2.3 Splitwise — ISCA 2024

论文：[Splitwise: Efficient Generative LLM Inference Using Phase Splitting](https://www.microsoft.com/en-us/research/wp-content/uploads/2023/12/Splitwise_ISCA24.pdf)

**解决了什么问题**

不同 GPU 的计算能力、显存带宽、功耗和价格比例不同，但传统系统要求一种机器同时承担 Prompt 和 Generation 两个阶段。

**为什么重要**

适合 Prefill 的 GPU 未必适合 Decode。共置会让高算力 GPU 在 Decode 阶段浪费计算能力，也可能让低成本 GPU 无法高效执行 Prefill。

**核心思想**

根据两个阶段的硬件需求构建异构推理集群。

**如何设计**

- 分别建立 Prompt 和 Token 阶段的性能模型；
- 决定两个资源池的机器类型、实例数量和批处理策略；
- Prefill 完成后把 KV Cache 传输到 Decode 节点；
- 将吞吐、延迟、价格与功耗纳入集群设计。

论文报告最高约 1.4 倍吞吐提升，同时降低约 20% 成本。

**边界与成熟度**

Splitwise 更偏异构硬件、成本和功耗，DistServe 更偏双 SLO 下的 goodput。两者共同建立了 P/D 解耦范式。

### 2.4 POD-Attention — ASPLOS 2025

论文：[POD-Attention: Unlocking Full Prefill-Decode Overlap for Faster LLM Inference](https://akkamath.github.io/publication/ASPLOS25-POD-Attention)

**解决了什么问题**

Sarathi 一类系统可以把 Prefill 和 Decode 放进同一个 hybrid batch，但已有 attention kernel 仍然分别执行两种 attention，GPU 计算单元和显存带宽不能真正互补。

**为什么重要**

调度器虽然组合了两种负载，kernel 层仍顺序执行，因此 chunked-prefill 的潜在收益没有完全实现。

**核心思想**

让 Prefill attention 和 Decode attention 在同一个 GPU SM 上并发执行，而不是先后调用两个独立 kernel。

**如何设计**

- 根据 Prefill 和 Decode 工作量划分 thread blocks；
- 分配寄存器、shared memory 和 SM 资源；
- 将计算密集的 Prefill 与带宽密集的 Decode 重叠执行；
- 针对 hybrid batch 调整资源分配，而不是分别优化两类 kernel。

Attention 部分平均加速约 28%，最高约 59%。

**边界与成熟度**

Hybrid batch 已经有较强的 kernel 基线。新工作需要面对新 GPU、MoE、GQA/MLA 或稀疏注意力等新结构，才能避免退化为普通 kernel tuning。

### 2.5 NanoFlow — OSDI 2025

论文：[NanoFlow: Towards Optimal Large Language Model Serving Throughput](https://www.usenix.org/conference/osdi25/presentation/zhu-kan)

**解决了什么问题**

推理过程包含计算、显存访问和通信等不同类型的操作，但传统引擎通常在单个 GPU 内将它们串行执行。

**为什么重要**

论文指出，许多常见负载下的端到端 LLM serving 仍然是 compute-bound，而已有系统没有充分利用设备内部的异构资源。

**核心思想**

把 batch 进一步切成 nano-batch，在单个 GPU 内细粒度重叠计算、内存访问和通信。

**如何设计**

- 将输入拆成可独立执行的 nano-batch；
- 复制部分算子，使不同 nano-batch 可以并行推进；
- 自动搜索 nano-batch 数量、大小、顺序和资源分配；
- 在优化中显式考虑并发操作之间的干扰。

论文报告约 1.91 倍吞吐提升，并达到理论最优吞吐的 50%–72%。

**边界与成熟度**

NanoFlow 不是专门消除 Prefill，而是提高整个推理流水线的设备利用率。它说明局部 kernel 加速不一定能转化为端到端收益。

### 2.6 PLA-Serve — MLSys 2026

论文：[PLA-Serve: A Prefill-Length-Aware LLM Serving System](https://proceedings.mlsys.org/paper_files/paper/2026/hash/bbb7506579431a85861a05fff048d3e1-Abstract-Conference.html)

**解决了什么问题**

P/D 解耦之后，Prefill 实例内部仍可能混合短 Prompt、长 Prompt 和多轮增量 Prompt，而统一调度策略不能适应这些差异。

**为什么重要**

长 Prefill 会阻塞短请求；短 Prefill 若等待合适 batch，又可能产生额外排队延迟。

**核心思想**

不仅区分 Prefill 和 Decode，还要根据 Prefill 长度进一步解耦请求。

**如何设计**

- 为长短 Prefill 建立双队列；
- 支持同一实例上的时间解耦；
- 支持多个 Prefill 实例之间的空间解耦；
- 对短 Prefill 使用 batch waiting window 与 CUDA Graph 聚类。

论文报告 Prefill latency 降低 30% 以上，吞吐提高约 35%。

**边界与成熟度**

该工作表明 P/D 解耦并未解决 Prefill 池内部的异质性。但单纯按长度分桶已成为较直接的调度优化，进一步工作需要更强的场景或机制。

---

## 3. 长上下文 Prefill 的计算与并行

### 3.1 LoongServe — SOSP 2024

论文：[LoongServe: Efficiently Serving Long-Context LLMs with Elastic Sequence Parallelism](https://arxiv.org/abs/2404.09526)

**解决了什么问题**

长上下文请求在不同长度、不同阶段和不同负载下需要的 GPU 数量差异很大，而传统 sequence parallelism 在整个生命周期使用固定并行度。

**为什么重要**

短请求使用过多 GPU 会浪费资源；超长 Prefill 使用过少 GPU 则会产生不可接受的 TTFT。

**核心思想**

并行度应当随请求、阶段和在线负载动态变化。

**如何设计**

- 提出 Elastic Sequence Parallelism；
- 运行时改变单个请求占用的 GPU 数量；
- 减少并行度变化时的 KV Cache 迁移；
- 重叠部分 Decode 通信与计算；
- 降低跨实例 KV Cache 碎片。

论文报告吞吐相对 chunked prefill 最高提高 3.85 倍。

**边界与成熟度**

长上下文动态并行的核心机制已经建立，但异构网络、在线迁移成本、多租户公平性和跨请求联合并行仍没有完全解决。

### 3.2 Context Parallelism — MLSys 2025

论文：[Context Parallelism for Scalable Million-Token Inference](https://proceedings.mlsys.org/paper_files/paper/2025/hash/78834433edc3291f4c6cbbd2759324db-Abstract-Conference.html)

**解决了什么问题**

单 GPU 无法以合理时间和内存完成百万 token 的精确 full-attention Prefill。

**为什么重要**

如果不修改模型且不接受精度损失，就必须把 sequence 和 attention 分布到大量 GPU 上。

**核心思想**

针对不同阶段选择传递 Q 或传递 KV 的 ring-attention 方案，以适应不同通信与内存条件。

**如何设计**

- 提供 pass-KV 与 pass-Q 两种精确算法；
- 覆盖 full prefill、persistent-KV prefill 和 Decode；
- 支持 RDMA 与普通 TCP 数据中心网络；
- 在多节点上扩展 sequence/context parallelism。

论文在 128 张 H100 上完成 Llama-3 405B 的 1M context Prefill，耗时约 77 秒，报告 93% 并行效率。

**边界与成熟度**

精确百万上下文已经可以扩展执行，但需要大量 GPU。“能否完成计算”基本解决，“能否经济、低延迟地完成”仍未解决。

### 3.3 LServe — MLSys 2025

论文：[LServe: Efficient Long-sequence LLM Serving with Unified Sparse Attention](https://proceedings.mlsys.org/paper_files/paper/2025/hash/cc8c6b9d89f7a898a29f58869b238e46-Abstract-Conference.html)

**解决了什么问题**

Full attention 的 Prefill 计算随上下文长度平方增长，Decode 阶段的 KV 容量和读取开销则线性增长。

**为什么重要**

系统调度只能改变执行顺序，不能消除 full attention 的算法复杂度。上下文继续增长后，Prefill 最终仍会被 attention FLOPs 限制。

**核心思想**

统一 Prefill 和 Decode 的结构化稀疏模式，在 block/page 粒度跳过不重要的 token。

**如何设计**

- 将部分 attention head 转换为 streaming head；
- 组合静态稀疏与动态稀疏；
- 使用 query-aware 的分层 KV page selection；
- 为 Prefill 和 Decode 提供统一的稀疏 attention 系统。

对 Llama-3-8B，论文报告 Prefill 平均加速约 2.4 倍，Decode 最高加速约 3.3 倍。

**边界与成熟度**

稀疏 Prefill 能改变复杂度，但通常依赖模型结构、attention pattern 和精度验证，尚不能为任意黑盒模型提供透明加速。

### 3.4 FlashInfer — MLSys 2025

论文：[FlashInfer: Efficient and Customizable Attention Engine for LLM Inference Serving](https://proceedings.mlsys.org/paper_files/paper/2025/hash/dbf02b21d77409a2db30e56866a8ab3a-Abstract-Conference.html)

**解决了什么问题**

Prefill、Decode、paged KV、ragged batch、不同位置编码和不同模型结构需要不同 attention kernel，固定 kernel 难以兼顾性能与灵活性。

**为什么重要**

上层调度、缓存和复用系统最终都依赖 attention kernel。缺少灵活的高性能基础层，会限制新系统机制的实际收益。

**核心思想**

用可组合 KV 布局和 JIT 模板生成适合具体运行场景的 attention kernel。

**如何设计**

- 使用 block-sparse 与 composable format 表达不同 KV 布局；
- 通过 JIT 生成定制 attention 变体；
- 使用 load-balanced scheduling 处理动态请求；
- 同时保持与 CUDA Graph 的兼容性。

论文报告长上下文推理延迟降低约 28%–30%。

**边界与成熟度**

通用 attention kernel 基础设施已经较为成熟。新的研究通常需要新的计算结构或跨层信息，而不是重新实现普通 paged-attention。

---

## 4. 精确前缀和多轮上下文复用

### 4.1 Prompt Cache — MLSys 2024

论文：[Prompt Cache: Modular Attention Reuse for Low-Latency Inference](https://proceedings.mlsys.org/paper_files/paper/2024/hash/a66caa1703fe34705a4368c3014c1966-Abstract-Conference.html)

**解决了什么问题**

系统消息、Prompt 模板、few-shot 示例和固定文档在不同请求中反复出现，却每次都重新 Prefill。

**为什么重要**

固定上下文可能远长于用户动态输入，其重复计算直接增加 TTFT 和 GPU 成本。

**核心思想**

让应用显式声明可复用的 prompt module，并提前计算其 attention state。

**如何设计**

- 使用 schema 描述 prompt module；
- 显式声明模块的位置与组合方式；
- 保证复用后的 positional encoding 正确；
- 请求到达时只计算没有缓存的模块。

论文报告 GPU 推理最高约 8 倍、CPU 推理最高约 60 倍 TTFT 改善。

**边界与成熟度**

该方法需要应用显式提供 schema，对任意运行时 Prompt 和任意位置拼接的支持有限。

### 4.2 SGLang / RadixAttention — NeurIPS 2024

论文：[SGLang: Efficient Execution of Structured Language Model Programs](https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html)

**解决了什么问题**

Agent、Tree-of-Thought、few-shot、RAG 和多轮对话会产生大量共享前缀，但普通请求级执行引擎无法跨请求、分支和回溯复用 KV Cache。

**为什么重要**

复杂 LLM 应用包含多个相关调用。若每个调用独立管理 KV，就会重复 Prefill 相同的系统指令、示例和中间结果。

**核心思想**

将 KV block 组织为 radix tree，自动匹配新请求的最长共享前缀。

**如何设计**

- RadixAttention 把 token sequence 映射到 radix tree；
- 树节点保存共享 KV block；
- 使用引用计数管理正在使用的节点；
- 使用 LRU 驱逐不活跃节点；
- 支持自动 prefix matching、分支共享和回溯复用。

**边界与成熟度**

单实例上的精确前缀匹配和复用已经基本解决。只实现“前缀树＋LRU”很难形成新的研究贡献。

### 4.3 CachedAttention — USENIX ATC 2024

论文：[Cost-Efficient LLM Serving for Multi-turn Conversations with CachedAttention](https://www.usenix.org/conference/atc24/presentation/gao-bin-cost)

**解决了什么问题**

对话暂停后，其 KV Cache 通常被 GPU 驱逐；下一轮恢复时，系统需要重新 Prefill 整个历史对话。

**为什么重要**

论文基于 ShareGPT 的分析显示，长对话中最高 99% 的 Prefill 成本可能来自历史内容的重复计算。

**核心思想**

把休眠会话的 KV 保存到层次化 AttentionStore，而不是删除并在下一轮重新计算。

**如何设计**

- 使用 GPU、CPU 和存储组成 KV 缓存层次；
- 使用逐层预取将 KV 加载与 GPU 计算重叠；
- 异步保存已生成的 KV；
- 由 scheduler 提供未来访问提示；
- 通过位置编码解耦支持历史截断。

论文报告 TTFT 最高降低约 87%，Prefill 吞吐最高提高约 7.8 倍。

**边界与成熟度**

多轮会话 KV 持久化已有直接且完整的代表工作。后续研究需要面对新的硬件层次、工作负载预测或上下文变换，而不能只做“把 KV 写入 SSD”。

### 4.4 Parrot — OSDI 2024

论文：[Parrot: Efficient Serving of LLM-based Applications with Semantic Variable](https://www.usenix.org/conference/osdi24/presentation/lin-chaofan)

**解决了什么问题**

复杂 Agent 工作流包含多个有依赖关系的 LLM 调用，但服务端只看到彼此独立的请求，不知道它们的数据依赖和共享语义。

**为什么重要**

服务端无法提前调度、识别共享变量、复用中间状态，也无法围绕整个应用的端到端 SLO 优化。

**核心思想**

把应用级数据流和变量语义暴露给 serving system。

**如何设计**

- 使用 Semantic Variable 标注 Prompt 中的输入与输出；
- 将多个 LLM 请求组织为数据流 DAG；
- 对调用关系执行 data-flow analysis；
- 联合进行共享前缀识别、依赖调度和应用级性能优化。

**边界与成熟度**

Parrot 的主要贡献是接口和跨请求语义，而不是单一 KV 策略。它说明 Agent Prefill 优化可以利用工作流结构，而不仅是 token 字符串。

### 4.5 Preble — ICLR 2025

论文：[Preble: Efficient Distributed Prompt Scheduling for LLM Serving](https://arxiv.org/abs/2407.00023)

**解决了什么问题**

Prefix caching 在单 GPU 上有效，但生产系统存在多个模型副本。把共享前缀请求集中到同一 GPU 会形成热点，均匀分配请求又会破坏缓存局部性。

**为什么重要**

单机 prefix caching 的高命中率不会自动扩展到分布式集群。

**核心思想**

路由决策必须同时考虑 KV 复用收益和目标 GPU 的实际执行负载。

**如何设计**

- 使用全局请求级和本地迭代级两层调度；
- 估算 prefix hit 带来的计算节省；
- 同时考虑 Prefill 成本、Decode 负载和排队成本；
- 热点变化时重定向请求或复制重要前缀；
- 使用优先级和配额降低饥饿问题。

论文报告平均延迟改善约 1.5–14.5 倍。

**边界与成熟度**

前缀局部性和负载均衡已经有直接强工作，不适合作为缺少新场景和新机制时的主要研究切入点。

### 4.6 Mooncake — FAST 2025 Best Paper

论文：[Mooncake: Trading More Storage for Less Computation](https://www.usenix.org/conference/fast25/presentation/qin)

**解决了什么问题**

长上下文 Prefill 成本高，单 GPU 显存又无法保留足够多的历史 KV；P/D 解耦后还需要在节点之间高效传输 KV Cache。

**为什么重要**

这是 Kimi 的真实生产问题。论文系统部署在数千节点，每天处理超过千亿 token，代表大规模长上下文服务中的实际矛盾。

**核心思想**

把 KV Cache 提升为集群级资源，用闲置 CPU DRAM、SSD、NIC 和远端内存换取更少的 GPU Prefill 计算。

**如何设计**

- 分离 Prefill 与 Decode 集群；
- 使用 Transfer Engine 在网络和多种存储介质间移动 KV；
- 建立分布式全局 KV Cache；
- KV-aware scheduler 联合考虑缓存命中、实例负载和 SLO；
- 过载时提前拒绝无法满足 SLO 的请求，避免浪费计算。

真实 trace 下，论文报告 effective capacity 相对基线提高约 59%–498%。

**边界与成熟度**

分布式 KV Cache 的基本体系结构已经有生产级代表。新的工作若只是“全局 KV 池＋调度器”，必须明确与 Mooncake 的本质差异。

---

## 5. KV 加载、压缩与层次化存储

### 5.1 CacheGen — SIGCOMM 2024

论文：[CacheGen: KV Cache Compression and Streaming for Fast LLM Serving](https://cs.stanford.edu/~keithw/sigcomm2024/sigcomm24-final1571-acmpaginated.pdf)

**解决了什么问题**

加载缓存 KV 可以避免 Prefill，但 KV tensor 通常比原始文本大得多，网络传输时间可能超过直接重新 Prefill 的时间。

**为什么重要**

若忽略网络和 I/O，KV caching 只是把计算瓶颈转化为传输瓶颈，甚至可能产生负收益。

**核心思想**

压缩 KV Cache，并根据实时条件在“加载压缩 KV”和“传输文本后重算”之间动态选择。

**如何设计**

- 利用 KV 在相邻 token 之间的分布局部性；
- 组合自适应量化与 arithmetic coding；
- 根据上下文长度、带宽和计算速度选择压缩级别；
- 当加载 KV 不划算时退回文本 Prefill。

论文报告 KV 大小降低约 3.5–4.3 倍，总加载和处理延迟降低约 3.2–3.7 倍。

**边界与成熟度**

KV 压缩和流式传输已有强基线，但不同模型、低比特精度、RDMA/CXL 和动态网络拥塞仍会改变最优决策。

### 5.2 HCache — EuroSys 2025

论文：[Fast State Restoration in LLM Serving with HCache](https://chenyoumin1993.github.io/papers/eurosys25-hcache.pdf)

**解决了什么问题**

恢复被驱逐上下文时，加载完整 KV 是 I/O 密集型，重算原始 token 是计算密集型，固定选择任何一种都可能很慢。

**为什么重要**

GPU 计算资源和 I/O 资源可能具有互补性。只使用一种恢复方法会浪费另一类资源。

**核心思想**

保存比完整 KV 更紧凑的中间 activation，恢复时从中间层继续计算，在存储空间和重算成本之间提供第三种选择。

**如何设计**

- Bubble-free restoration scheduler 混合 KV 加载、activation 恢复和 token 重算；
- 利用计算和 I/O 的资源互补性；
- Chunk-based storage manager 解决保存与恢复时的数据布局冲突；
- 在不同系统状态下选择合适的恢复方式。

相比 KV offload，论文报告 TTFT 最高降低约 1.93 倍，并减少约 1.92–2.40 倍存储空间。

**边界与成熟度**

HCache 说明状态表示不只有“原始 token”和“完整 KV”两种，中间 activation 是有效的设计空间。

### 5.3 Strata — OSDI 2026

论文：[Strata: Hierarchical Context Caching for Long Context LLM Serving](https://www.usenix.org/conference/osdi26/technical-sessions)

**解决了什么问题**

GPU–CPU–SSD 层次化 KV Cache 容量很大，但实际系统会因碎片化小 I/O、Prefill 等待加载，以及多个请求同时等待相同 miss 的 delay hit 而变成 I/O-bound。

**为什么重要**

Cache hit 不等于请求一定变快。如果命中项需要经过缓慢、碎片化的加载，反而可能拖慢整个 batch。

**核心思想**

联合设计 GPU/主机数据布局、I/O 粒度、KV 加载和请求调度。

**如何设计**

- GPU-assisted I/O 解耦 GPU block layout 与主机/SSD layout；
- 将碎片化 KV 合并为大粒度传输；
- Scheduler 识别和缓解 delay hit；
- 通过组合互补工作隐藏 cache-loading latency；
- 在不伤害短上下文请求的条件下支持层次化缓存。

Strata 集成进 SGLang 并在生产中部署，论文报告吞吐最高优于 vLLM-LMCache 约 5 倍。

**边界与成熟度**

层次化 KV caching 尚未完全收敛，但通用 I/O 聚合、delay-hit 处理和 loading-aware scheduling 已经出现最新强工作。

### 5.4 Bidaw — FAST 2026

论文：[Bidaw: Enhancing KV Caching via Bidirectional Computation–Storage Awareness](https://www.usenix.org/conference/fast26/presentation/hu-shipeng)

**解决了什么问题**

低成本部署使用 host memory 和 SSD 保存多轮对话 KV，但不同层的加载延迟差异会造成 head-of-line blocking。

**为什么重要**

论文中的交互 workload 平均有 22.4 轮，历史重复计算最高占总计算的 93.1%；但直接加载两级存储又会使延迟增加最高 3.8 倍。

**核心思想**

计算引擎应感知 KV 所在存储层；存储系统也应利用对话内容预测下一次访问。

**如何设计**

- 按 KV 所在存储层分离请求；
- 按 KV 大小重排请求以降低阻塞；
- 利用 LLM 生成的回复预测用户未来访问；
- 只保存存储成本相对计算收益较高的历史状态。

论文报告响应延迟最高降低约 3.58 倍，吞吐最高提高约 1.83 倍。

**边界与成熟度**

Bidaw 面向低成本、本地两级存储环境，与 Mooncake 面向大规模 RDMA 集群的假设不同。

---

## 6. 非严格前缀与 RAG/Agent 中的 Prefill 复用

### 6.1 CacheBlend — EuroSys 2025

论文：[CacheBlend: Fast LLM Serving for RAG with Cached Knowledge Fusion](https://arxiv.org/abs/2405.16444)

**解决了什么问题**

RAG 文档块会反复出现，但它们通常处在不同位置，且前面存在不同内容。直接复用独立计算的 KV 会丢失文档块与前文之间的 attention。

**为什么重要**

严格 prefix caching 只能命中位于开头、顺序和内容完全相同的部分，大量重复 RAG 文档仍需重新 Prefill。

**核心思想**

不重算整个文档块，只重新计算少量对跨块 attention 敏感的 token，以修正独立缓存产生的误差。

**如何设计**

- 为 document chunk 预计算并存储 KV；
- 识别 KV 偏差较大的 token；
- 对这些 token 执行 selective recomputation；
- 将少量修正计算与 KV 加载流水化。

相对完全重算，论文报告 TTFT 改善约 2.2–3.3 倍，吞吐提高约 2.8–5 倍。

**边界与成熟度**

CacheBlend 并不是任意上下文下的一般性精确复用。它依赖选择性重算，并通过任务指标验证输出质量近似保持。

### 6.2 Cache-Craft — SIGMOD 2025

论文：[Cache-Craft: Managing Chunk-Caches for Efficient Retrieval-Augmented Generation](https://dl.acm.org/doi/10.1145/3725273)

**解决了什么问题**

RAG 请求会以任意顺序和上下文检索相同 chunk。系统需要同时判断哪些 chunk 可以复用、需要修复多少 token，以及缓存和驱逐哪些 chunk。

**为什么重要**

只提出 attention 修正方法不足以形成可用系统，还必须处理 admission、eviction、连续 batching 和硬件存储管理。

**核心思想**

根据 chunk 的上下文依赖和 attention 差异评估复用风险，只修复必要部分。

**如何设计**

- 建立 chunk-level KV cache；
- 识别可以直接复用和需要修复的 chunk；
- 对必要 token 执行少量重算；
- 联合管理存储、eviction 和连续 batching；
- 尽可能隐藏加载与修复开销。

生产 workload 上，论文报告相对 prefix caching 减少约 51% 冗余计算，吞吐提高约 1.6 倍。

**边界与成熟度**

CacheBlend 更偏 KV 融合方法，Cache-Craft 更偏完整 RAG cache manager。RAG chunk reuse 已经不是无人研究的空白方向。

### 6.3 CacheSlide — FAST 2026

论文：[CacheSlide: Unlocking Cross Position-Aware KV Cache Reuse](https://www.usenix.org/conference/fast26/presentation/liu-yang)

**解决了什么问题**

Agent Prompt 中的固定段落绝对位置经常改变，但段落之间的相对顺序可能保持稳定。严格位置相关缓存不能复用，完全位置无关方法又需要大量修正。

**为什么重要**

Tool definition、system instruction、历史轨迹和动态 observation 交织时，很少存在完整、稳定的相同前缀。

**核心思想**

识别 Relative-Position-Dependent Caching 模式，利用相对结构比绝对 token position 更稳定的特点。

**如何设计**

- Chunked Contextual Position Encoding 提高位置移动后的表示相似度；
- 只为少量 token 重新计算 attention；
- Weighted Correction Attention 融合缓存 KV 和新计算 KV；
- 使用 layer-wise 和 spill-aware KV 管理降低额外开销。

论文报告延迟改善约 3.11–4.3 倍，吞吐提高约 3.5–5.8 倍。

**边界与成熟度**

CacheSlide 将非前缀复用扩展到 Agent Prompt，但仍针对特定相对位置模式，并依赖 KV 修正机制，不能说明任意上下文重排都可以无损复用。

---

## 7. 当前问题的成熟度判断

| 问题 | 当前成熟度 | 代表工作 | 判断 |
|---|---:|---|---|
| 长 Prefill 阻塞 Decode | 高 | Sarathi-Serve、POD-Attention | 调度和 kernel 已有强基线 |
| Prefill/Decode 资源特性不同 | 高 | DistServe、Splitwise、Mooncake | P/D 解耦范式已经建立 |
| 单实例精确前缀复用 | 很高 | SGLang、Prompt Cache | 普通前缀树与 LRU 创新不足 |
| 多轮对话历史重复 Prefill | 较高 | CachedAttention、Mooncake、HCache、Bidaw | 已有完整系统链路 |
| 集群 prefix-aware 路由 | 较高 | Preble、Mooncake | 局部性和负载平衡已有直接工作 |
| 百万 token 精确 Prefill 并行 | 中高 | LoongServe、Context Parallelism | 可以扩展，但资源成本很高 |
| 长上下文稀疏 Prefill | 中 | LServe | 依赖模型和精度假设 |
| KV 远端加载与层次化存储 | 中 | CacheGen、HCache、Strata、Bidaw | 持续推进，尚未完全收敛 |
| RAG 非前缀 KV 复用 | 中 | CacheBlend、Cache-Craft | 已有多篇直接工作 |
| Agent 跨位置 KV 复用 | 中低 | CacheSlide | 仍是结构特定、带修正的方案 |
| 任意上下文编辑后的精确 KV 复用 | 低 | 尚无通用方案 | 受因果 attention 依赖限制 |

---

## 8. 初步结论

### 8.1 已经比较拥挤的方向

以下问题已经有较完整的研究链路，不适合在缺少新场景、新数据和新机制时作为主要切入点：

- Prefill 与 Decode 的基本干扰；
- Chunked prefill；
- P/D 解耦本身；
- 单机严格前缀缓存；
- 普通 prefix-aware 路由与负载均衡；
- 将多轮会话 KV 简单保存到 CPU 或 SSD；
- 普通 RAG chunk KV 复用。

### 8.2 现有工作的共同强假设

已有方案通常依赖以下一个或多个假设：

- Prompt 存在完全相同的长前缀；
- 相同文档块会频繁重复出现；
- 应用愿意暴露 DAG、module 或语义结构；
- KV 加载带宽足够高，或者可以被计算隐藏；
- 工作负载的到达分布和复用规律相对稳定；
- 少量选择性重算不会显著损害输出质量；
- 模型结构允许稀疏化或特定的位置编码修正。

这些假设不成立时，已有系统的收益可能迅速下降。

### 8.3 不能只比较 Cache Hit Rate

后续研究至少应同时报告：

- TTFT 平均值与 P95/P99；
- TPOT 或 inter-token latency；
- 请求吞吐和 SLO goodput；
- 实际减少的 Prefill FLOPs/token；
- KV 加载和迁移字节数；
- GPU、CPU、网络和 SSD 利用率；
- 缓存占用与存储成本；
- 命中后真正获得正收益的比例；
- 输出质量及其置信区间；
- 在负载突变、缓存冷启动和热点变化下的表现。

---

## 9. 后续调研建议：建立 Claim–Evidence–Limitation 矩阵

后续可以对每篇论文继续补充以下信息：

| 论文 | 主要 Claim | Workload/Trace | 是否公开 | 模型与硬件 | 对比基线 | 核心假设 | 未覆盖场景 |
|---|---|---|---|---|---|---|---|
| Sarathi-Serve |  |  |  |  |  |  |  |
| DistServe |  |  |  |  |  |  |  |
| Splitwise |  |  |  |  |  |  |  |
| LoongServe |  |  |  |  |  |  |  |
| SGLang |  |  |  |  |  |  |  |
| CachedAttention |  |  |  |  |  |  |  |
| Preble |  |  |  |  |  |  |  |
| Mooncake |  |  |  |  |  |  |  |
| CacheGen |  |  |  |  |  |  |  |
| HCache |  |  |  |  |  |  |  |
| CacheBlend |  |  |  |  |  |  |  |
| Cache-Craft |  |  |  |  |  |  |  |
| Strata |  |  |  |  |  |  |  |
| CacheSlide |  |  |  |  |  |  |  |
| Bidaw |  |  |  |  |  |  |  |

建立该矩阵的目的不是继续罗列论文，而是寻找以下交叉区域：

1. 多篇论文都观察到，但没有被系统解决的真实现象；
2. 仅在某一类 trace 中成立、需要更广泛验证的结论；
3. 现有方法收益会突然失效的 workload 边界；
4. 计算、网络、存储和调度之间尚未被联合优化的矛盾；
5. 有公开数据集、可复现实验，同时尚未形成成熟解决方案的场景。

只有在这些证据基础上，才适合进一步提出能够回答以下三个问题的研究课题：

- **What is the problem?** 问题和触发条件是什么？
- **Why does it matter?** 它在什么真实 workload 中造成多大损失？
- **Why does existing work fail?** 现有方案依赖的哪个假设在该场景中不成立？

