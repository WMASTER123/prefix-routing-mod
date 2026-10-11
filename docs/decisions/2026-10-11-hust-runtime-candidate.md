# HUST 配套源码候选与隔离验证

状态：候选评估，不是运行基线。用户已授权新服务器执行，且明确要求以指定 HUST 仓库为准。

执行结果：[两个 venv、Qwen14B eager、真实 IPC 存储事件与全新 import cache 对照通过](../../evidence/hust-runtime-candidate-2026-10-11.md)。可继续局部验证，尚未冻结全部服务制品或放行图/恢复/性能。

## 事实与选择

新机器为 4×910B2，物理设备 0/1/6/7；预装源码的 origin 经核验指向上游，不是 HUST。既有预装功能成绩仅属那套环境，不能继承给新候选。

优先原样构建以下归档 main，不因元数据版本线不同直接否定兼容，也不混用最新主干或官方 wheel：

- core：`intellistream/vllm-hust-legacy-20260831@13f11da44455d2f85f00f810a04e1bd2eaf30ba6`，发行包仍名为 `vllm`。
- Ascend：`intellistream/vllm-ascend-hust-legacy-20260831@3a61053d8b4133eae7c468c1567648048750be21`，发行包名为 `vllm-ascend-hust`，导入名为 `vllm_ascend`。

按该 Ascend 仓库 README 的联合解析及双 editable 顺序验证。其 requirements 指定 torch/torch-npu 2.10.0、Triton Ascend 3.2.1、Transformers 5.5.4；core 的 aarch64 OpenCV 为 4.11.0.86。不能继续套用上游 0.23.0 的 OpenCV/NumPy 冲突结论。

## 边界与风险

所有源、依赖锁、构建、服务 venv 和原始证据留在 `/root/work/prefix-routing-mod/artifacts/hust-runtime-20261011`；不继承系统 site-packages。保留全球环境和公共模型不动，不改引擎源文件。通过完整 Git bundle 固定提交和历史，避免浅历史改变 HUST 版本计算。

仓库安装资料以 CANN 9.0 为参考，新服务器实际为 CANN 9.1.0，不能声称官方配套支持；必须依次验证依赖一致性、导入/动态库、真实 TP=1 Qwen2.5-14B、原生 KV 事件。首次 `COMPILE_CUSTOM_KERNELS=0` 只用于可行性检查，不是性能最优构建。若原样环境失败，保存首个根因再决定，不自动改 host CANN/驱动或修改包约束。

审计显式选择 `vllm-ascend-hust`；同时装入上游 `vllm-ascend` 一律失败，避免同一导入树被覆盖。继承的 CANN Python 路径和本地自带 Python 包须单独审计；干净 Python 依赖锁不覆盖 CANN 制品。

HUST KV payload 含 tagged map，以及 group 和 replay/epoch 扩展；先补私有 IPC 正向探测，不复用旧 array-only 解码器，也不直接采用其旧 Router 设计。完整恢复、图模式、并发取消和性能仍按 G2–G4 独立验收。

## 退出与冻结

联合解析失败或设备 ABI/真实推理失败即停止该候选的后续路由接入；不通过 `--no-deps` 掩盖冲突，不伪造版本。提出仓库内历史配套或平台匹配镜像的证据后再调整。二次干净重建及 G1 交付物完成前，不冻结服务基线；人工审查 PR，禁止自动合并。
