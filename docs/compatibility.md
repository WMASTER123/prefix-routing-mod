# 支持与候选矩阵

2026-10-11 补充：预装组合虽然有 eager 局部功能证据，但依赖一致性检查失败，**不能冻结为可复现运行基线**。Triton Ascend 3.2.2 与 OpenCV 存在 NumPy 约束冲突，已查官方候选尚未消除；详见[环境门槛证据](../evidence/runtime-gate-2026-10-11.md)。原生事件正向诊断已有成功样本，完整恢复仍未通过；不继承到尚未建成的隔离环境。

| 对象 | 固定版本 / 范围 | 当前状态 |
|---|---|---|
| 元数据工具 | Python >=3.11；CI 目标 3.11/3.12 | Windows 3.12、Linux x86_64 3.11/3.12、Linux aarch64 3.12 基础检查通过 |
| Extension template | 0d537f3193f217b21cf730a32f0a2629b631244b | 已用于生成，非完整发布认证 |
| Extension Manager | 00d1b940dc6ce8d60e2cd28708b80d80e9c33a09 | 仅静态发现、schema、拒绝启用检查 |
| vllm-hust-legacy-20260831 | 13f11da44455d2f85f00f810a04e1bd2eaf30ba6 / metadata 0.23.1rc0 | 用户指定候选，未验证运行 |
| vllm-ascend-hust-legacy-20260831 | 3a61053d8b4133eae7c468c1567648048750be21 / metadata 0.19.1rc1 | 用户指定候选，未验证与上行匹配 |
| 官方 vLLM + Ascend | 待冻结版本 | 产品兼容目标，不由 HUST 结果外推 |
| vLLM Router | 待 P1a 固定 commit | 第一评估候选，没有接入 |
| 新服务器 | aarch64 / 4×910B2 / Python 3.12.13 / 16 核配额 / 128 GiB RAM | 已盘点；空闲不证明独占 |
| 预装系统推理栈 | CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0 | 用户选择优先首验；Qwen2.5-14B TP=1 BF16 eager 功能探测通过；未覆盖图/完整事件恢复/路由性能 |

不同包的元数据版本号不能证明或否定兼容，必须核查依赖约束和实际调用。本轮不安装候选大规模引擎。运行声明需要：提交、制品 hash、环境指纹、配置、测试结果、局限和回退。版本变化重新验收。

不支持的产品能力：当前没有线上 Router、精确路由、生产 KV 事件订阅/恢复、引擎 observer、Manager 运行激活或性能声明。探测工具能采样原生事件，不等同生产订阅器。

首验源码提交：vLLM `0fc695fc6d1d82e9a5ac6835ac8e4e1c83703665`；vLLM-Ascend `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`。仅为本机固定源码/环境结果，不据包名外推官方发行制品认证。归档 HUST 组合后续独立验证；详见[决策](decisions/2026-10-11-qwen14b-first.md)和[证据](../evidence/qwen14b-preflight-2026-10-11.md)。

## Ascend 支持声明的粒度

每组声明必须绑定驱动/固件、CANN 及实际组件、torch/torch_npu、vLLM/Ascend 提交、模型/revision、dtype、Attention、APC、图模式、chunked prefill、并行与实际 KV 布局；只验收选定主配置和扩展测试组合，不用一行“支持 Ascend”概括全部。

2026-10-11 新增能力与执行档案仍是 [设计合同](contracts.md)。官方功能矩阵只用于筛选候选；归档元数据不同不能单独证明不兼容，也不能靠更换版本号宣布兼容。预装系统栈与指定归档候选分别隔离评估，运行基线用 ADR 冻结。CPU 工具测试通过不证明生产 Router 或引擎已支持 aarch64/NPU。
