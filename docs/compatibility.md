# 支持与候选矩阵

现行完成情况统一以[当前进度](progress.md)为准，近期接线见[在线目录计划](planning/online-kv-directory.md)。本页下方按日期追加的完成/未完成描述保留为历史；旧版publisher能力不外推到新版HUST，参考接口不等于生产ABI。新增双副本目录子集不表示公开HTTP全链路、长期恢复或容量验收通过。

2026-10-14 后续：原typed Router库的异步准入/自有载体/转发/生命周期已和真实HUST终态组合到同一实验账本，2 family/3 child及丢许可保债通过。原三文件宿主patch与HUST包装不变；新接收器映射显式实例，新增完整终态批次校验。当前仍不是生产HTTP入口、在线KV或性能验收，component-v0.1/生产ABI和MOD import_only不变。范围与下一步见[组合计划](planning/router-hust-terminal-chain.md)和[证据](../evidence/router-hust-terminal-chain-2026-10-14.md)，下面同日较早记录仅对应当时阶段。

2026-10-14 最新实机状态：新分配 32068 的 HUST ced6857/b47795c 已完成真实 HTTP→完整 family→核心逻辑终态→原账本的可选有限闭环。4 个 family/8 个 child 全部关联，stop string/断连核心 abort、重放、账本重开和自有包装恢复通过；不是原生 Router 已联通，也不是在线 KV 目录或性能验收。默认关闭、固定守卫、MOD import_only 不变。当前限制和下一步以[实机接线说明](planning/cedb477-live-terminal.md)及[证据](../evidence/cedb477-live-terminal-2026-10-14.md)为准，下方按日期保留历史。

2026-10-14补充：cedb477新版终态/入口方法与原账本的CPU组合通过，旧归档方法亦回归；不扩大安装/运行能力。32099分配当前拒绝SSH，本轮无新NPU资格或服务器同步。固定源码门槛与未完成项见[现场计划](planning/cedb477-terminal-boundaries.md)。

2026-10-13 当前迁移覆盖：用户选定 Core ced6857 / Ascend b47795c；新版本 profile、原生 KV 规范化和独立制品恢复见[版本适配入口](planning/hust-version-adapters.md)。以下固定旧源码、旧 publisher/终态 hook 和历史验收仅属于原提交；不能据此在新版本启用。新版可靠终态、共同水位恢复与在线目录接线仍须逐项验收。

最新方向：用户已纠正目标为指定 HUST 仓库。两份完整 Git 历史已校验并在新服务器隔离构建，见[候选决策](decisions/2026-10-11-hust-runtime-candidate.md)。HUST 的 NumPy 依赖联合解析成功；预装上游的冲突不能移植为 HUST 结论。下面早期预装记录保留历史范围，不代表新候选已验收。

历史上游复核：预装组合有 eager 局部功能证据，但依赖检查失败，不能冻结为一致基线；详见[旧环境证据](../evidence/runtime-gate-2026-10-11.md)。当前 HUST 已独立恢复并实际验证，旧冲突和旧事件成绩均不继承。恢复和功能子集通过仍不等于完整兼容或发布支持。

| 对象 | 固定版本 / 范围 | 当前状态 |
|---|---|---|
| 当前HUST迁移目标 | Core ced6857/vllm0.30.0+empty；Ascend b47795c/vllm-ascend0.25.1rc2.dev766+hust.20260903.4.gb47795c63 | 新环境/全新重建；Qwen14B FP16/eager、真实终态、原生事件在线目录及双TP1同时运行子集通过；已热接管/完整恢复/公开HTTP全链/MOD激活/容量性能未验收 |
| 当前研发分配 | aarch64/8核配额/64GiB/2×910B2 | 两个TP1副本同时运行、隔离、真实cached_tokens 0→384及单侧目录失效通过；实验后自有进程已清理 |
| 元数据工具 | Python >=3.11；CI 目标 3.11/3.12 | Windows 3.12、Linux x86_64 3.11/3.12、Linux aarch64 3.12 基础检查通过 |
| Extension template | 0d537f3193f217b21cf730a32f0a2629b631244b | 已用于生成，非完整发布认证 |
| Extension Manager | 00d1b940dc6ce8d60e2cd28708b80d80e9c33a09 | 仅静态发现、schema、拒绝启用检查 |
| vllm-hust-legacy-20260831 | 13f11da44455d2f85f00f810a04e1bd2eaf30ba6 / metadata 0.23.1rc0 | 与下行隔离构建；本机 TP=1 Qwen14B BF16 eager 局部验证及 venv 重建通过 |
| vllm-ascend-hust-legacy-20260831 | 3a61053d8b4133eae7c468c1567648048750be21 / metadata 0.19.1rc1 | 发行包 vllm-ascend-hust；真实请求/IPC 存储事件通过，custom kernels 关闭，图/完整恢复/性能未验收 |
| 官方 vLLM + Ascend | 待冻结版本 | 产品兼容目标，不由 HUST 结果外推 |
| vLLM Router | 91b65ee4bdfbe98863a32a2205dced5dbb249ffe | 原提交 Linux aarch64 构建及HTTP/SSE通过；typed Router→HUST真实终态有限闭环、原索引→HUST在线事件双副本子集分别通过。两条接缝尚未在公开HTTP入口合并为生产选点链，见[当前进度](progress.md) |
| 历史CPU研发分配 | aarch64 / Python 3.12.13 / 16 核配额 / 64 GiB RAM / 0 NPU | 当时用于CPU合同/隔离Rust构建/模拟，不代表当前分配 |
| 历史 NPU 分配 | aarch64 / 4×910B2 / Python 3.12.13 / 16 核配额 / 128 GiB RAM | 当时物理枚举 0/1/2/7，身份/hook 主回归仅逻辑 0；更早四卡结果分别记录，不继承到当前分配 |
| HUST 环境恢复入口 | 两个固定源码 + 186 包及 builder 锁 | 全新目录 13 步通过，本机研发证据齐备、正式冻结待人工复核；不等于可移植镜像 |
| 原生 KV 回归子集 | HUST IPC + 结构化 replay；1024 context / 1 GiB KV | 真实存储/自然移除/清空/重放子集通过；快照水位风险复现，完整恢复/身份未验收 |
| 原生 KV 恢复故障子集 | 同机 IPC / 受控空起点 / 4 批窗口 | 多批重放、缓冲去重、SUB 断连、控制面超时、同地址真实重启通过；旧连接注入单独标识，原生快照边界/完整恢复未过；身份新进展另列，见[既有证据](../evidence/g2-recovery-regression-2026-10-11.md) |
| 纯文本身份 | 固定 HUST/Qwen2.5-14B/完整块/group 0 | 分词、chat template、真实 token 块对应通过；整数 hash 为不透明锚点，native bytes 父链重算未验证，见[新证据](../evidence/g2-identity-publisher-2026-10-11.md) |
| 可选 publisher hook | 固定版本/文件守卫；TP=1、DP rank 0、纯文本/IPC/eager | 已热冷接入、超窗 token 快照、增量/清空/同地址重启及 wire epoch 子集通过；默认关闭、生产交付/压力/开销/图配置未验收 |
| 预装系统推理栈 | CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0 | 用户选择优先首验；Qwen2.5-14B TP=1 BF16 eager 功能探测通过；未覆盖图/完整事件恢复/路由性能 |

不同包的元数据版本号不能证明或否定兼容，必须核查依赖约束和实际调用。最新本轮仅在私有隔离环境构建 HUST 候选，没有改全局引擎。运行声明需要：提交、制品 hash、环境指纹、配置、测试结果、局限和回退。版本变化重新验收。

不支持的产品能力：当前没有可交付的公开HTTP精确路由全链、任意已热服务恢复、Manager运行激活或容量提升声明。已有受控空起点的真实在线KV订阅与目录资格，但实验owner/stdio和测试oracle不等同生产热路径；不得扩展到未经测试的模型、TP、图模式、LoRA/多模态或跨节点。

早期预装首验源码提交：vLLM `0fc695fc6d1d82e9a5ac6835ac8e4e1c83703665`；vLLM-Ascend `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`。仅为旧环境结果；HUST 已独立构建验证，见[最新证据](../evidence/hust-runtime-candidate-2026-10-11.md)，不据包名外推官方发行制品认证。

## Ascend 支持声明的粒度

每组声明必须绑定驱动/固件、CANN 及实际组件、torch/torch_npu、vLLM/Ascend 提交、模型/revision、dtype、Attention、APC、图模式、chunked prefill、并行与实际 KV 布局；只验收选定主配置和扩展测试组合，不用一行“支持 Ascend”概括全部。

2026-10-11 新增能力与执行档案仍是 [设计合同](contracts.md)。官方功能矩阵只用于筛选候选；归档元数据不同不能单独证明不兼容，也不能靠更换版本号宣布兼容。预装系统栈与指定归档候选分别隔离评估，运行基线用 ADR 冻结。CPU 工具测试通过不证明生产 Router 或引擎已支持 aarch64/NPU。
