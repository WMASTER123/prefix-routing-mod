# 支持与候选矩阵

| 对象 | 固定版本 / 范围 | 当前状态 |
|---|---|---|
| 元数据工具 | Python >=3.11；CI 目标 3.11/3.12 | Windows 3.12、Linux x86_64 3.11/3.12、Linux aarch64 3.12 基础检查通过 |
| Extension template | 0d537f3193f217b21cf730a32f0a2629b631244b | 已用于生成，非完整发布认证 |
| Extension Manager | 00d1b940dc6ce8d60e2cd28708b80d80e9c33a09 | 仅静态发现、schema、拒绝启用检查 |
| vllm-hust-legacy-20260831 | 13f11da44455d2f85f00f810a04e1bd2eaf30ba6 / metadata 0.23.1rc0 | 用户指定候选，未验证运行 |
| vllm-ascend-hust-legacy-20260831 | 3a61053d8b4133eae7c468c1567648048750be21 / metadata 0.19.1rc1 | 用户指定候选，未验证与上行匹配 |
| 官方 vLLM + Ascend | 待冻结版本 | 产品兼容目标，不由 HUST 结果外推 |
| vLLM Router | 待 P1a 固定 commit | 第一评估候选，没有接入 |
| 新服务器 | aarch64 / 2×910B2 / Python 3.12.13 | 硬件清单和 CPU 脚手架验证通过，不等于引擎测试通过 |
| 预装系统推理栈 | CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0 | 只读元数据；与候选不同，未运行、未覆盖 |

不同包的元数据版本号不能证明或否定兼容，必须核查依赖约束和实际调用。本轮不安装候选大规模引擎。运行声明需要：提交、制品 hash、环境指纹、配置、测试结果、局限和回退。版本变化重新验收。

不支持的能力：当前没有线上服务、精确路由、KV 事件订阅、引擎 observer、Manager 运行激活或性能声明。
