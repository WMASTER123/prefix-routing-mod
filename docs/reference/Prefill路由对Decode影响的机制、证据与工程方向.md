# Prefill 路由对 Decode 的影响：机制、证据与工程方向

> 2026-10-09 状态更新：当前产品方向为性能优先的前缀路由综合套件，最新范围见[总体架构与组件规划](./前缀路由综合套件_总体架构与组件规划.md)。本文保留为专项分析；生成停顿仍是核心服务约束，cohort、本地 batch/kernel 协同等不自动成为首版必做功能。

日期：2026-10-05  
定位：对《分布式KV智能路由系统_混合负载干扰治理与工程规划.md》的专项补充。本文只澄清问题、证据和待验证假设，不预设最终算法，也不把尚未实测的判断写成项目贡献。

工程范围更新（2026-10-05）：以主规划的“工程确认台账与 NPU 验证基线”为当前实施依据。decode 负载、输出增长和生成停顿属于核心观测与服务约束；本文提出的 cohort 半衰期、本地 batching 协同和专门 kernel 优化仍是候选增强，不自动升级为首版主线。华为 NPU 资源尚未申请，相关后端能力须申请后核验。

## 1. 结论先行

“KV-aware 路由主要服务 prefill，所以只影响 TTFT”只对了一半。

- 对 vLLM Automatic Prefix Caching 这类普通机制，**直接作用**确实是跳过重复 prefill；vLLM 官方也明确说明 APC 本身不会缩短新 token 的生成阶段。
- 但分布式路由会改变请求落入哪个实例、何时进入 decode、与哪些请求组成 batch、该实例保留多少共享和私有 KV、是否发生抢占，以及 P/D 分离时向哪个 decode worker 搬运 KV。因此它会通过资源竞争和 batch 构成**间接改变 TPOT/TBT**。
- 如果执行层支持 Hydragen、CoDec、Feather 一类 shared-prefix-aware decode，前缀聚集还能成为**直接的 decode 优化**：同一份共享前缀不再被批内每条序列重复读取，或小而同质的 batch 反而比更大的异质 batch 更快。
- 相反，若只按 prefix hit 聚集流量，热点 prefix 会把大量请求同时推入同一 decode 实例；输出 KV 又是每条请求独有且持续增长，可能出现 TTFT 下降而 TPOT、尾部 TBT、公平性和完成时间恶化。

因此，当前工程不应再把问题理解成“将请求送到已有 KV 的节点”，而应进一步检验：

> **入口路由形成的前缀请求群组，能否在整个请求生命周期内转化为有益的 decode batch；何时这种聚集只节省 prefill，何时能降低 decode KV 访问，何时反而形成 decode 热点、长度拖尾和公平性问题？**

这个问题比“再加一个 decode 指标”更实质。它要求把全局 placement 与本地 batch formation 之间的脱节当作研究对象。

---

## 2. Prefill 与 Decode 的真实关系

### 2.1 两个阶段不是互不相关的两个服务

Prefill 对整个输入并行计算并产生初始 KV，通常计算密集；decode 每轮为每个活跃请求生成一个或少量 token，每轮都要访问该请求此前的 KV，通常受显存带宽和批处理效率限制。

一个请求的 prefill 结果正是其 decode 的初始工作集。路由在 prefill 入口做出的选择会留下四种后果：

1. **计算后果**：是否要重算未命中的 prompt；
2. **驻留后果**：共享 prefix 和独有 suffix 占用哪个实例的 HBM；
3. **排队后果**：新请求在哪个实例加入 active decode；
4. **组成后果**：它会与哪些上下文长度、prefix 身份和剩余输出长度的请求共同执行。

因此，TTFT 和 TPOT 不是两个独立目标。降低某个请求的 prefill 成本，可能保护已有 decode，也可能更快地向一个热点实例注入更多 decode 工作。

### 2.2 三类不同的“复用”

必须区分：

| 复用层次 | 节省什么 | 普通 vLLM/SGLang 前缀缓存是否自然获得 |
|---|---|---|
| Prefill 计算复用 | 不重新计算共享 prompt 的 K/V | 是，这是 APC/RadixAttention 的主要收益 |
| KV 存储复用 | 多个请求引用一份物理 prefix blocks | 通常可以，取决于引擎布局和引用管理 |
| Decode 访问复用 | 一批请求对共享 prefix 的 attention 合并读取/计算 | 不一定；需要 batch 构成和 attention kernel 显式利用 |

vLLM 当前官方文档明确把 APC 的直接边界写为“只减少 prefill，不减少生成新 token 的时间”。这意味着“缓存命中高”不能自动推出“decode 更快”。[vLLM APC 文档](https://docs.vllm.ai/en/v0.30.0/features/automatic_prefix_caching/)

Hydragen 和 CoDec 说明第三层复用是可以实现的，但它不是第一层的自然副产物。Hydragen 将共享 prefix 与独有 suffix 的 attention 分开，再跨序列批处理共享部分；CoDec 对 prefix tree 中共享节点合并 KV 访问。它们解决的是 decode 阶段重复读共享 KV，而不仅是是否已缓存。[Hydragen](https://arxiv.org/abs/2402.05099) [CoDec](https://arxiv.org/html/2505.17694)

### 2.3 一个有用但不作为精确性能模型的表达

设一个 batch 有 `B` 个请求，共享 `P` 个 prefix tokens；第 `i` 个请求有独有输入后缀 `S_i` 和已生成输出 `O_i`。

共享物理块时，活跃 KV 容量近似为：

```text
M_active ≈ KV(P) + Σ KV(S_i + O_i)
```

这比为每个请求复制一份 `P` 节省容量。但普通 decode attention 的访存工作仍近似随下式增长：

```text
Traffic_decode ∝ Σ (P + S_i + O_i)
```

只有 shared-prefix-aware kernel 或足够好的缓存局部性，才能把共享项 `B × P` 的重复访问显著摊薄。这里的公式用于说明机制，不是 GPU 执行时间的精确预测式；kernel、GQA/MLA、并行方式和硬件缓存都会改变常数及瓶颈。

另一个重要现象是：`O_i` 会随时间增长，而且每个请求的 `O_i` 不同。于是 prefix 的相对占比和群组同质性会衰减。可以把这种现象称为 **decode cohort 的半衰期**：共享 prefix 越长、请求越同步、输出越相近，有效群组维持得越久；输出长尾越重，群组越快分裂。

---

## 3. 当前热点与新策略如何影响 Decode

## 3.1 Cache-first / 最长前缀命中路由

### 可能改善

- 避免长 prefill 与已有 decode 同批执行，减少 generation stall；
- 共享物理 prefix blocks，降低 HBM 占用，可能容纳更多 active sequences 并减少抢占；
- 将同 prefix 请求送往同一实例，为 prefix-homogeneous batching 和 shared-prefix decode kernel 创造条件。

### 可能恶化

- 请求 herd 到少数 cache-rich worker，decode batch、等待队列和输出 KV 增长集中；
- 新请求因为 prefill 很快完成，会在短时间内同步进入 decode，形成 decode burst；
- 普通 attention kernel 没有合并共享 KV 读取时，只获得容量收益，没有对应的带宽收益；
- 若只用 in-flight request count，长输出和长上下文请求与短请求被当成相同负载。

Preble 已经证明纯 affinity 会造成负载失衡，并用重定向和 prefix replication 缓解；但其主要 workload 的 prompt 比输出长 37–2494 倍，论文明确说不把 TPOT 作为主要指标，编程负载中因为 decode 占比变大且系统“不改进 decode performance”，相对收益变小。这说明它不能直接回答长输出场景。[Preble, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/5bc342f48de8264779952fac378f96dc-Paper-Conference.pdf)

## 3.2 Load-aware + KV-aware 路由

vLLM Production Stack 的公开策略使用：

```text
matched_tokens / prompt_tokens - beta × relative_load
```

其中 load 是 in-flight requests。它能抑制最明显的 herd，但无法区分相同请求数下的上下文长度、剩余输出、batch 组成和共享前缀是否能被 decode kernel 利用。[vLLM load-aware routing](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/loadaware-routing.html)

LMetric 用 `new prefill tokens × current batch size`，比随意加权更简洁，且实验显示相对 vLLM 和生产调度器分别降低 TPOT 21% 和 20%。这证明入口路由即使以 prefill 与 batch 为指标，也能通过负载平衡间接改善 decode。论文同时发现一个真实失败窗口：一批共享同一 prefix 的长请求突发时，乘法分数持续选择少数命中实例，形成失衡；其补救是热点检测后回退到负载优先。[LMetric, OSDI 2026](https://arxiv.org/abs/2603.15202)

这仍留下两个空白：

1. `batch size` 是粗粒度代理，相同 batch size 的 decode 代价可以因当前 KV 长度和前缀同质性而显著不同；
2. `new prefill tokens` 描述 admission 时的工作，却不描述未来输出 KV 增长和 decode cohort 的寿命。

## 3.3 热点 prefix 复制

复制不是只为 TTFT 服务。它对 decode 有相反的两面：

- **副本太少**：单个实例承接热点的全部 decode，造成排队、HBM 压力和 TPOT 尾部恶化；
- **副本太多**：同 prefix 请求被分散，每个 worker 难以形成足够大的同质 batch，重复存储更多共享 KV，也削弱 shared-prefix kernel 的合并机会；
- **复制时机过晚**：请求已经集中并进入 decode，入口路由只能影响后来者，不能修复运行中 herd；
- **复制时机过早**：为短暂热点建立多份 KV，消耗显存与 prefill/传输资源，反过来干扰 decode。

CacheRoute 用请求率和单 destination 容量决定副本数，主实验优化并只报告 p99 latency/KV hit/SLO capacity，没有报告 TPOT，也没有在正文 workload 中建模输出长度；其所有主 workload 热 key 实际只有一个目的地。它很好地证明了稳定 affinity 与负载规划的价值，也留下了“副本数是否应由 decode demand 决定”的开放问题。[CacheRoute](https://arxiv.org/html/2608.19677)

因此可检验的新假设不是“热点需要复制”，这已经不是新观点；而是：

> 热点副本需求应由 `到达率 × 生命周期内 decode 工作量`、共享前缀可直接利用程度和群组半衰期共同决定，而不只由请求 QPS 或 prefill 节省量决定。

## 3.4 Chunked prefill 与混合 batch

Sarathi-Serve 的实验证明 prefill 策略会直接影响正在生成请求的 token 间停顿：未经切分的 hybrid batching 相比 decode-only batch，TBT 最坏增加 28.3 倍。其 Table 4 中，openchat workload 的 `hybrid-only` P99 TBT 为 0.68 s，加入 chunked prefill 后的完整方案为 0.14 s；对应 P50 TTFT 从 0.53 s 增至 0.76 s。这是明确的 TTFT–TBT 取舍，而不是单一 TTFT 优化。[Sarathi-Serve, OSDI 2024](https://www.usenix.org/system/files/osdi24-agrawal.pdf)

但 chunk 越小并非越好。论文报告 512-token chunk 的 prefill 开销最多约 25%，并指出 token budget 依赖硬件、并行方式和 TBT SLO。POD-Attention 从 kernel 层进一步说明，将 prefill 和 decode 放在同一 hybrid batch 时，分别优化的 attention kernel 仍不能充分利用计算和带宽；其混合 attention kernel 相比独立 kernel 最多加速 59%、平均 28%。[POD-Attention, ASPLOS 2025](https://doi.org/10.1145/3676641.3715996)

对本工程的含义：外部 router 仅知道“这个 worker 有多少请求”不够；它至少需要知道 active decode、待执行/正在执行的 prefill token budget，以及该后端是否采用 chunked/hybrid 策略。否则路由器可能把一个长 miss 送到看似空闲、实则会伤害 decode 的实例。

## 3.5 P/D 解耦与 KV 传输

DistServe 将两个阶段分开，正是因为共置会使二者相互干扰。其 13B/A100 示例中，共置系统在 TTFT/TPOT 双 SLO 下约为 1.6 req/s；单独测得 prefill 和 decode 能力后，按 2P:1D 组合的理想总体为 10 req/s，即约 3.3 req/s/GPU，约为共置的 2.1 倍。论文也报告在 ShareGPT 上比 vLLM 支撑 2.0–4.6 倍请求率。[DistServe, OSDI 2024](https://www.usenix.org/system/files/osdi24-zhong-yinmin.pdf)

P/D 分离消除了同 GPU 上的 prefill interference，却没有消除路由对 decode 的影响，而是把问题改写为：

```text
prefill worker 选择
    → 产生/复用哪些 KV
    → KV 传输源、路径、大小和完成时间
    → decode worker 选择
    → decode batch 与 HBM 工作集
```

若 prefill 路由只追求 hit，随后却把大 KV 传给已拥塞的 decode worker，TTFT 与 TPOT 都可能恶化；如果所有请求从同一 prefill/cache source 发往多个 D，也可能产生链路或源端热点。Mooncake 的 scheduler 已经考虑 prefix hit、排队和传输，因此“加入网络指标”本身不是新贡献。[Mooncake, FAST 2025](https://www.usenix.org/system/files/fast25-qin.pdf)

PPD 进一步给出很有用的反例：在 H100/Llama-3.1-8B 上，同样处理 1,024 tokens，full prefill 与 decode 共置在 batch size 200 时使 TPOT 下降约 48%，而复用历史 KV 的 append-prefill 只下降约 2%；4 个并发 prefill 时约为 57% 对 21%。这说明不能只按“prefill tokens 数”估计干扰，cache state 会改变 prefill 的执行形态。[PPD](https://arxiv.org/html/2603.13358)

## 3.6 Prefix-homogeneous batching 与 shared-prefix decode

这一类工作直接否定“prefix 只对 prefill 有用”。

- Hydragen 将共享 prefix attention 与独有 suffix attention 分解并合并结果。在 CodeLlama-13B 的特定批量共享前缀实验中，端到端吞吐最高提高 32 倍；大 batch 下把共享 prefix 从 1K 增至 16K，吞吐下降不足 15%，而基线下降超过 90%。这是强机制证据，但不是通用在线服务结果。
- CoDec（SIGMOD/PACMMOD 2026）在 shared-prefix decode 场景中，相对 FlashDecoding 的 attention 平均加速 1.9 倍，global-memory access 平均减少 120.9 倍；其 PyTorch 原型在特定 LooGLE/A100 实验中相对 vLLM 0.6.6 的 TPOT 平均改善约 3.75 倍。结果来自高共享、长上下文条件，不能直接外推到混合在线流量。
- Feather（2026 预印本）报告较小的 prefix-homogeneous batch 可能比更大的异质 batch 有更高 decode throughput，并在 vLLM/SGLang 集成中报告 2–10 倍端到端吞吐。它还指出 radix tree 遍历的 CPU 开销可能接近 GPU 执行时间。该结果很重要，但目前应按预印本和特定共享 workload 看待。[Feather](https://arxiv.org/abs/2605.06046)

这些工作产生一个关键工程条件：**路由形成的同 prefix 流量，只有被本地 scheduler 保留为可利用的 cohort，并由相应 kernel 执行时，才会产生直接 decode 收益。** 如果 gateway 把请求聚到同一 worker，但本地 FCFS/continuous batching 将其拆散到不同迭代，或者当前 kernel 仍逐序列读取，共享机会就只停留在 prefill/HBM 层。

## 3.7 KV 长度对齐：与 prefix 身份不同的另一维

AlignedServe（SIGMOD/PACMMOD 2026）研究的是每个 decode iteration 内的长度不齐：一个长 KV 请求会成为整轮瓶颈。其 H100/Llama-7B 实验中，在 batch size 64、已生成 600 tokens 时，64 个短请求的 iteration latency 为 13.49 ms；混入 4 个 4K 长 prompt 后增至 21.73 ms，约增加 61%。另一个实验中，按相近长度分组的平均 TPOT 约 200 ms，混合 64 种长度的 batch 为 233.43 ms；完整系统最高提高 decode throughput 1.98 倍、延迟最多降低 7.4 倍。[AlignedServe](https://arxiv.org/html/2605.23389)

这与 prefix-homogeneous batching 并不等价：

- 同 prefix 请求刚开始通常长度接近；
- 输出进行后，长度和剩余时间会分化；
- 不同 prefix 的请求也可能恰好有相近长度；
- 为保持同 prefix 而等待会伤害延迟和公平性，为保持长度对齐同样如此。

因此路由/调度至少存在两个 batch-quality 轴：`prefix identity overlap` 与 `current KV-length dispersion`。只优化任意一个都可能损害另一个。

---

## 4. 已有实验到底证明了什么

| 工作 | 直接测到的 decode 现象 | 能支持的结论 | 不能支持的结论 |
|---|---|---|---|
| vLLM APC 文档 | 明确说明 APC 不直接减少 decode 时间 | 普通前缀缓存首先是 prefill 优化 | 高 hit 一定降低 TPOT |
| Sarathi-Serve OSDI'24 | naive hybrid TBT 最坏为 decode-only 的 28.3×；Table 4 显示 chunking 显著降低 P99 TBT | prefill 策略能严重干扰已有 decode | 任何 chunk 大小都适合所有硬件/负载 |
| DistServe OSDI'24 | 共置显著拉高 TPOT；双 SLO goodput 受干扰限制 | P/D interference 是系统级问题 | 分离永远优于共置；网络代价永远可忽略 |
| Preble ICLR'25 | mixed trace TPOT 改善；长 decode workload 收益变小，且主评测不以 TPOT 为核心 | cache/load 路由可间接改善 decode | 已解决长输出、decode-heavy 路由 |
| POD-Attention ASPLOS'25 | hybrid attention kernel 平均加速 28%、最高 59% | 干扰也存在于 kernel 资源分配 | 外部 router 单独即可获得该收益 |
| LMetric OSDI'26 | 相对两类基线 TPOT 分别降 21%/20%；真实 prefix burst 出现失效窗口 | 简单指标可以很强，hotspot 仍可能失衡 | `prefill tokens × batch size` 精确表示 decode 成本 |
| CoDec SIGMOD'26 | 高共享场景下 TPOT 平均约 3.75× 改善，attention 访存大幅下降 | 共享 prefix 可被直接用于 decode | 普通 PagedAttention 已自动获得同等收益 |
| AlignedServe SIGMOD'26 | 少量长 KV 使 iteration latency 增 61%；长度分组改善 TPOT | batch 内长度方差是 decode 成本变量 | 同 prefix 就始终长度对齐 |
| CacheRoute 2026 | 主要报告 p99、capacity、KV hit；不报告 TPOT | 稳定 affinity/规划在其 workload 有效 | 已回答 decode-heavy 和输出增长问题 |
| PPD 2026 预印本 | full 与 append prefill 对 TPOT 干扰约 48% vs 2% | prefill 的缓存状态改变干扰强度 | 所有多轮请求都应在 D 节点做 append-prefill |
| Feather 2026 预印本 | 同质小 batch 可胜过异质大 batch；端到端吞吐 2–10× | 最大 batch 不是普适最优 | 该 RL 策略已在广泛生产流量成立 |

上述结果跨模型、版本、硬件和 workload，不应横向比较倍数。它们的共同价值是确认了多条机制，而不是给本项目承诺一个性能数字。

---

## 5. 最容易被现有路由遗漏的 Decode 问题

### 5.1 Admission-time load 与 lifetime load 不同

一个请求到达时可观测的是 prompt、cache overlap 和当前 worker 状态；真正占用 decode 的量还依赖输出长度、取消、EOS 和 speculative acceptance。相同 prefix hit 的两个请求，一个输出 32 tokens、另一个输出 4K tokens，对热点副本的压力完全不同。

Dynamo 当前开发文档已经比多数路由完整：它能计 active blocks、prefill tokens、potential decode blocks，并提供可选的 output-block tracking、OSL hint 和 active-request weight。但 `track-output-blocks` 默认关闭，`decode-active-request-weight` 是实验项且默认 0。这说明工业系统已经意识到问题，也说明项目不能把“考虑 decode blocks”本身当创新。[Dynamo 配置文档](https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/router/configuration-and-tuning)

剩余问题在于：即便精确知道总 blocks，也仍不知道这些请求进入本地 scheduler 后能否形成高效 batch。

### 5.2 全局路由与本地 scheduler 各自优化

全局 router 可以知道 prefix→worker、粗粒度 load；本地 scheduler 才知道下一轮 batch、chunk budget、抢占和实际序列集合。两者缺少契约时会出现：

- router 为 prefix locality 聚集请求，本地 scheduler 没保留 cohort；
- router 用 batch size 估负载，本地 batch 因长短混合而慢；
- router 认为共享 prefix 能改善 decode，实际 kernel 只提供存储共享；
- scheduler 为局部吞吐延迟某些请求，破坏全局公平或 SLO。

### 5.3 热 prefix 的“成功”会制造 decode 同步洪峰

大量命中请求的 prefill 很短，可能在相近时间开始 decode。若热点副本数依据历史 QPS 而非输出 token rate，系统会低估后续压力。这是一种正反馈：缓存越热，进入该 worker 的请求越快；进入越快，active decode 越多；active decode 增加又使缓存命中节点的 TPOT 和队列变差。

### 5.4 输出 KV 会侵蚀共享收益

物理共享的 prefix 只存一份，但每条请求的输出 KV 都独有。随着生成推进：

- 活跃内存从“共享 prefix 主导”变为“unique output 主导”；
- HBM 可容纳的并发数下降；
- 新输出 blocks 可能迫使空闲 prefix blocks 被淘汰；
- 之前合理的 prefix affinity 可能不再是好的 load placement。

入口插件不能随意迁移运行中 decode，因此必须承认控制能力边界：它主要能影响未来 admission，不能承诺消除已经形成的热点。

### 5.5 同质性收益与公平性冲突

等待同 prefix 或相近长度请求可以提高吞吐，但低频 prefix 可能一直凑不出 batch。Locality-aware Fair Scheduling 的实验显示，面向效率的 prefix 调度在存在“行为异常”的客户端时，会让正常客户端延迟比其公平方案高最多 7.18 倍（Preble）和 9.55 倍（RR+LPM）。这不是纯 decode 实验，但明确表明 locality 不能脱离租户隔离和 starvation bound。[Locality-aware Fair Scheduling](https://arxiv.org/html/2501.14312)

### 5.6 P/D 分离把 interference 换成 transfer coupling

解耦后，prefill 不再占用 D 的算力，但 transfer completion 会影响请求何时加入 D；并发 transfer 还可能竞争 NIC、PCIe、HBM 写入或源端资源。decode router 若只看 active requests，不看即将到达的 KV 和 in-flight reservations，会在 KV 真正到达前重复选择同一 D。

---

## 6. 对当前工程问题定义的修正建议

当前最后一版把项目定义为“混合负载下的 KV 干扰感知路由与弹性请求放置插件”。这个方向可以保留，但“干扰”需要从缓存淘汰扩展到请求生命周期：

> **在同构、多副本 LLM 服务中，入口路由会同时形成 KV 分布和未来 decode 批次。系统应在不接管推理引擎的前提下，识别何时 prefix affinity 只节省 prefill，何时还能形成有益的 decode cohort，何时会造成输出增长、长度拖尾或热点失衡；据此控制请求落点和热点副本，使 TTFT 与 TPOT/TBT 双 SLO 下的 goodput 稳定提高。**

这个表述的特色不是“同时看 TTFT 和 TPOT”，因为 DistServe、Dynamo、LMetric 等都已这样做；区别应落在：

1. **把 routing 产生的 decode batch composition 视为一等结果**，而不是只把 batch size/blocks 当 load；
2. **区分三层 prefix benefit**：prefill compute、physical residency、decode access；
3. **把 cohort 的形成与衰减纳入热点副本和 spill 决策**；
4. **按后端能力分级**：普通 kernel 时只计间接收益，shared-prefix kernel 时才计直接 decode 收益；
5. **用 no-regression guardrail 约束低复用、长输出和低频租户**。

这是一个候选工程问题，尚不能称为已证实空白。CoDec、Feather、AlignedServe 已分别触及 kernel 或单系统 batch；需要实验确认“外部跨实例路由与本地 batch 的协同”仍有稳定、可插拔的增量价值。

---

## 7. 不应采用的简单方案

### 7.1 把 decode 指标直接加进线性 score

例如：

```text
score = a × cache_hit - b × queue - c × decode_blocks
```

这可以作为 baseline，但不能成为项目核心。原因是：

- 各项不是独立的；cache hit 会改变 prefill 形式和 decode 到达速度；
- decode blocks 只表示工作集大小，不表示 prefix identity、长度方差和 kernel 能力；
- 权重随模型、硬件、chunk budget、kernel 与负载变化；
- LMetric 已经专门展示了线性加权的调参问题，并给出更强的简单基线。

### 7.2 把所有同 prefix 请求始终绑在一个实例

这会把缓存局部性直接转成队列偏斜。正确对照至少要包含 Preble 式 spill/replication、LMetric 热点回退、Dynamo decode tracking 和 CacheRoute 规划 affinity。

### 7.3 只报告平均 TPOT

平均 TPOT 会掩盖长停顿。至少同时报告：

- per-request TPOT；
- per-token TBT 的 p50/p95/p99 和最大 gap；
- decode iteration latency；
- active decode 数、当前 KV length 分布及 batch prefix purity；
- 各租户/前缀族的等待、完成率与 starvation；
- TTFT/TPOT 双 SLO goodput。

### 7.4 用高 cache hit 推断 decode 改善

必须分别测：

- 实际跳过的 prefill tokens；
- 物理唯一 KV blocks 与引用数；
- decode attention 读取字节或可得的 kernel counters；
- batch 内共享前缀比例；
- TPOT/TBT。

否则无法判断收益来自少算 prefill、少占容量、改善 batch，还是只是负载变化。

---

## 8. 建议优先做的验证实验

当前最合理的下一步不是立即写新策略，而是做一个能否立项的机制实验矩阵。

### 8.1 实验 A：缓存命中是否只改善 TTFT

固定模型、GPU、输入/输出和到达过程，对比：

1. cache off + least-load；
2. cache on + least-load；
3. cache on + cache-first；
4. cache on + 强 KV/load router。

扫 prefix sharing、输入长度、输出长度和并发。若 hit 上升而 TPOT 不变，说明只有直接 prefill 收益；若 TPOT 改变，再用 active decode、prefill interference 和 batch composition 归因。

### 8.2 实验 B：Decode herd

制造同一长 prefix 的突发流量，分别设置短输出、固定长输出和重尾输出。记录：

- 命中实例的 active decode arrival curve；
- output blocks 增长；
- HBM watermark、抢占/重算；
- TTFT、TPOT/TBT 与完成时间；
- spill 或新增 prefix 副本发生前后的变化。

目标是确认“快速命中导致同步 decode 洪峰”是否真实且足够大。

### 8.3 实验 C：Prefix purity 与 KV length dispersion 的二维扫描

构造相同 batch size，但改变：

- 同 prefix 比例；
- 当前 KV 长度方差；
- shared-prefix-aware kernel 是否启用。

输出每轮 latency、attention bytes、SM/HBM 利用、吞吐。该实验能回答 batch size 为什么不足，并给出路由需要的最小状态。

### 8.4 实验 D：副本数不只由 QPS 决定

固定相同 prefix 到达率，分别改变 OSL 分布和共享前缀长度，扫描 1、2、4…个目的地。若最优副本数随输出工作量明显变化，就支持 decode-demand-aware replication 假设。

### 8.5 实验 E：外部 router 与本地 scheduler 的协同价值

同一全局 placement 下比较：

- 原生 FCFS/默认 continuous batching；
- LPM/prefix-aware local scheduling；
- 长度对齐；
- 可用时的 shared-prefix decode kernel。

反过来，在同一本地 scheduler 下比较不同全局路由。只有存在显著交互项，才值得把“跨层 cohort 协同”作为项目核心；如果本地 scheduler 已吸收全部收益，入口插件应保持简单。

### 8.6 Workload 必须有负对照

至少覆盖：

| 负载 | 用途 |
|---|---|
| 长输入、短输出、高共享 | prefill 主导正例 |
| 长输入、长输出、高共享 | 检验直接 decode 收益和 output growth |
| 短输入、长输出、低共享 | decode-heavy 负对照 |
| 热 prefix 突发 | 检验 herd 与副本形成 |
| 多轮 append-prefill | 检验 full/append 差异 |
| 多 prefix、KV 长度重尾 | 检验 iteration straggler |
| 低频租户混入热点租户 | 检验公平与 starvation |

所有策略应使用相同 offered-load ladder 和 SLO，报告置信区间；不能用每个方案各自最有利的并发或 batch 参数比较。

---

## 9. 当前可形成的研究判断

### 已被文献明确支持

1. Prefill 会显著干扰 decode，chunking 或 P/D 分离可以缓解；
2. prefix caching 本身主要跳过 prefill，不等于 decode 加速；
3. cache-aware routing 可通过负载改变间接改善或恶化 TPOT；
4. shared-prefix-aware batching/kernel 能直接减少 decode KV 访问；
5. batch size 不足以表达 decode 成本，KV 长度方差和 prefix homogeneity 都重要；
6. 强 locality 会产生热点和公平性风险。

### 由现有证据推导、但仍需本项目实验确认

1. cache hit 后快速进入 decode 会造成可观测的同步 herd；
2. 热点 prefix 的最佳副本数随输出 token demand 变化；
3. decode cohort 具有可预测的半衰期，可用于决定继续聚集还是 spill；
4. gateway 向本地 scheduler 传递 cohort hint，能在不重写引擎的情况下保留足够收益；
5. 这套机制在普通 kernel 与 shared-prefix kernel 两种能力层级上都能 no-regression。

### 目前不能宣称

- 已找到比 vLLM/SGLang/Dynamo 普遍更好的策略；
- 只靠入口插件就能获得 CoDec/Hydragen 的 kernel 收益；
- TPOT 改善一定来自 decode locality，而不是减少 prefill interference；
- 所有热点都应复制或所有同 prefix 请求都应聚集；
- 一个统一解析式 cost model 能跨模型和硬件准确工作。

---

## 10. 建议的阶段性决策

在当前信息下，我建议把“缓存干扰治理”主线升级成更完整、也更有辨识度的候选方向：

> **生命周期感知的 prefix-cohort 路由：让分布式 KV 路由不仅决定哪里复用 prefill，还要约束它将形成怎样的 decode 工作集与 batch；在 cohort 收益、输出增长、长度拖尾、热点容量和公平性之间进行有边界的 placement。**

但暂时不要马上设计复杂算法。先完成实验 A–E，重点看两个量：

1. 在强基线下，`prefix identity + KV length dispersion + output growth` 是否比 `new prefill tokens + batch size/blocks` 提供稳定的额外预测能力；
2. 外部路由是否真的能改变本地 batch composition，还是这些信息最后被本地 scheduler 打散。

若两项都成立，项目核心就不再是又一个热点路由 score，而是**跨全局 placement 与本地 decode batching 的可插拔协同层**。若不成立，则应回到最后一版的缓存干扰/恢复治理，或直接贡献现有路由器的观测与调参插件，而不强行制造新算法。

---

## 11. 主要资料

- [vLLM Automatic Prefix Caching](https://docs.vllm.ai/en/v0.30.0/features/automatic_prefix_caching/)
- [vLLM Production Stack: Load Aware Routing](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/loadaware-routing.html)
- [NVIDIA Dynamo: Router Configuration and Tuning](https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/router/configuration-and-tuning)
- [Sarathi-Serve, OSDI 2024](https://www.usenix.org/system/files/osdi24-agrawal.pdf)
- [DistServe, OSDI 2024](https://www.usenix.org/system/files/osdi24-zhong-yinmin.pdf)
- [Preble, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/5bc342f48de8264779952fac378f96dc-Paper-Conference.pdf)
- [POD-Attention, ASPLOS 2025](https://doi.org/10.1145/3676641.3715996)
- [Mooncake, FAST 2025](https://www.usenix.org/system/files/fast25-qin.pdf)
- [LMetric, OSDI 2026](https://arxiv.org/abs/2603.15202)
- [Hydragen](https://arxiv.org/abs/2402.05099)
- [CoDec, SIGMOD/PACMMOD 2026](https://arxiv.org/html/2505.17694)
- [AlignedServe, SIGMOD/PACMMOD 2026](https://arxiv.org/html/2605.23389)
- [Locality-aware Fair Scheduling](https://arxiv.org/html/2501.14312)
- [CacheRoute](https://arxiv.org/html/2608.19677)
- [PPD](https://arxiv.org/html/2603.13358)
- [Feather](https://arxiv.org/abs/2605.06046)
