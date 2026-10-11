# 支持与候选矩阵

最新方向：用户已纠正目标为指定 HUST 仓库。两份完整 Git 历史已校验并在新服务器隔离构建，见[候选决策](decisions/2026-10-11-hust-runtime-candidate.md)。HUST 的 NumPy 依赖联合解析成功；预装上游的冲突不能移植为 HUST 结论。下面早期预装记录保留历史范围，不代表新候选已验收。

历史上游复核：预装组合有 eager 局部功能证据，但依赖检查失败，不能冻结为一致基线；详见[旧环境证据](../evidence/runtime-gate-2026-10-11.md)。当前 HUST 已独立恢复并实际验证，旧冲突和旧事件成绩均不继承。恢复和功能子集通过仍不等于完整兼容或发布支持。

| 对象 | 固定版本 / 范围 | 当前状态 |
|---|---|---|
| 元数据工具 | Python >=3.11；CI 目标 3.11/3.12 | Windows 3.12、Linux x86_64 3.11/3.12、Linux aarch64 3.12 基础检查通过 |
| Extension template | 0d537f3193f217b21cf730a32f0a2629b631244b | 已用于生成，非完整发布认证 |
| Extension Manager | 00d1b940dc6ce8d60e2cd28708b80d80e9c33a09 | 仅静态发现、schema、拒绝启用检查 |
| vllm-hust-legacy-20260831 | 13f11da44455d2f85f00f810a04e1bd2eaf30ba6 / metadata 0.23.1rc0 | 与下行隔离构建；本机 TP=1 Qwen14B BF16 eager 局部验证及 venv 重建通过 |
| vllm-ascend-hust-legacy-20260831 | 3a61053d8b4133eae7c468c1567648048750be21 / metadata 0.19.1rc1 | 发行包 vllm-ascend-hust；真实请求/IPC 存储事件通过，custom kernels 关闭，图/完整恢复/性能未验收 |
| 官方 vLLM + Ascend | 待冻结版本 | 产品兼容目标，不由 HUST 结果外推 |
| vLLM Router | 91b65ee4bdfbe98863a32a2205dced5dbb249ffe | 原提交 Linux aarch64 check/release、启动、completion/chat JSON/SSE 与关闭重试单次 503 通过；仅 CPU/mock 准入，未接 HUST、索引未在在线策略接线，见[最新证据](../evidence/router-cpu-admission-2026-10-12.md) |
| 当前研发服务器 | aarch64 / Python 3.12.13 / 16 核配额 / 64 GiB RAM / 0 NPU | CPU 合同、隔离 Rust 构建及模拟；不能验收 NPU 能力 |
| 历史 NPU 分配 | aarch64 / 4×910B2 / Python 3.12.13 / 16 核配额 / 128 GiB RAM | 当时物理枚举 0/1/2/7，身份/hook 主回归仅逻辑 0；更早四卡结果分别记录，不继承到当前分配 |
| HUST 环境恢复入口 | 两个固定源码 + 186 包及 builder 锁 | 全新目录 13 步通过，本机研发证据齐备、正式冻结待人工复核；不等于可移植镜像 |
| 原生 KV 回归子集 | HUST IPC + 结构化 replay；1024 context / 1 GiB KV | 真实存储/自然移除/清空/重放子集通过；快照水位风险复现，完整恢复/身份未验收 |
| 原生 KV 恢复故障子集 | 同机 IPC / 受控空起点 / 4 批窗口 | 多批重放、缓冲去重、SUB 断连、控制面超时、同地址真实重启通过；旧连接注入单独标识，原生快照边界/完整恢复未过；身份新进展另列，见[既有证据](../evidence/g2-recovery-regression-2026-10-11.md) |
| 纯文本身份 | 固定 HUST/Qwen2.5-14B/完整块/group 0 | 分词、chat template、真实 token 块对应通过；整数 hash 为不透明锚点，native bytes 父链重算未验证，见[新证据](../evidence/g2-identity-publisher-2026-10-11.md) |
| 可选 publisher hook | 固定版本/文件守卫；TP=1、DP rank 0、纯文本/IPC/eager | 已热冷接入、超窗 token 快照、增量/清空/同地址重启及 wire epoch 子集通过；默认关闭、生产交付/压力/开销/图配置未验收 |
| 预装系统推理栈 | CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0 | 用户选择优先首验；Qwen2.5-14B TP=1 BF16 eager 功能探测通过；未覆盖图/完整事件恢复/路由性能 |

不同包的元数据版本号不能证明或否定兼容，必须核查依赖约束和实际调用。最新本轮仅在私有隔离环境构建 HUST 候选，没有改全局引擎。运行声明需要：提交、制品 hash、环境指纹、配置、测试结果、局限和回退。版本变化重新验收。

不支持的产品能力：当前没有线上 Router、精确路由、生产 KV 事件订阅/恢复、Manager 运行激活或性能声明。实验 publisher hook 与测试 oracle 不等同已交付的生产订阅器/目录；不得扩展到未经测试的模型、TP、图模式、LoRA/多模态或跨节点。

早期预装首验源码提交：vLLM `0fc695fc6d1d82e9a5ac6835ac8e4e1c83703665`；vLLM-Ascend `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`。仅为旧环境结果；HUST 已独立构建验证，见[最新证据](../evidence/hust-runtime-candidate-2026-10-11.md)，不据包名外推官方发行制品认证。

## Ascend 支持声明的粒度

每组声明必须绑定驱动/固件、CANN 及实际组件、torch/torch_npu、vLLM/Ascend 提交、模型/revision、dtype、Attention、APC、图模式、chunked prefill、并行与实际 KV 布局；只验收选定主配置和扩展测试组合，不用一行“支持 Ascend”概括全部。

2026-10-11 新增能力与执行档案仍是 [设计合同](contracts.md)。官方功能矩阵只用于筛选候选；归档元数据不同不能单独证明不兼容，也不能靠更换版本号宣布兼容。预装系统栈与指定归档候选分别隔离评估，运行基线用 ADR 冻结。CPU 工具测试通过不证明生产 Router 或引擎已支持 aarch64/NPU。
