# HUST 隔离环境候选：执行与审查记录

日期：2026-10-11。类型：真实 NPU 局部功能、依赖解析/重建和代码自检；不是 Router 或性能验收。原始日志和失败报告均留在私有 artifacts，不以成功覆盖失败。

## 来源、设备与候选

最新分配为 aarch64 / Python 3.12.13 / 4×910B2，每卡 HBM 65536 MiB。npu-smi mapping：物理 0/1/6/7 对应逻辑 0/1/2/3；本轮只使用逻辑 0。16 核时间配额、128 GiB cgroup RAM、32 GiB shm。驱动 26.0.rc1、ascendhal 7.35.23；设备 board 报告 Firmware 9.0.0.0.205。CANN compiler/OPP 9.1.0、ATB 9.1.0.B150；镜像 digest 尚无证据，不推断平台固定官方配套。

预装两源码目录 origin 均为上游；未修改。真实候选是两个指定 HUST main，完整历史 bundle 校验后在 Linux 原样克隆：

| 对象 | 完整提交 / bundle SHA256 | 构建发行版本 |
|---|---|---|
| core | `13f11da44455d2f85f00f810a04e1bd2eaf30ba6`；bundle `25799a4e71bf550376e2d10628ca56d08698e0273d87d2500134da66e9ad8dd5` | `vllm==0.23.1.post1.dev733+g13f11da44.empty` |
| Ascend | `3a61053d8b4133eae7c468c1567648048750be21`；bundle `4db267dc65168aaf1cb5a6185af29d15a89788612b906e02023601541135c025` | `vllm-ascend-hust==0.19.1.post1.dev563+g3a61053d` |

不因上游元数据版本不同直接判不兼容，不混装官方 Ascend。构建后的源码 git status 无改动；没有修改 scheduler、算子、API 预检或引擎事件代码。Windows 原克隆因 long path 不完整，只取完整 Git bundle，实际 Linux checkout 完整且提交核验通过。

## 已解决的 Python 环境问题

1. HUST common/runtime/empty build 联合解析成功：186 包带 SHA256 的[候选锁](../runtime/candidates/README.md)。核心为 torch 2.10.0（导入版本 2.10.0+cpu）、torch-npu 2.10.0、Triton Ascend 3.2.1、NumPy 1.26.4、OpenCV 4.11.0.86、Transformers 5.5.4。不是改上游 wheel 元数据或忽略冲突。
2. 两个新服务 venv 的 `include-system-site-packages=false`，安装使用 `--require-hashes`；双 HUST editable 按仓库 README 构建，core empty/Ascend custom kernels 关闭。不把 `--no-deps` 当成依赖验证，安装后独立审计。
3. 默认 CANN Python path 会把可选 profiler 等工具暴露给服务解释器，审计报告包括 pandas/OpenTelemetry 约束及可选工具缺失。改为进程级仅保留 OPP/TBE 路径后，两个服务环境均通过依赖审计；未删系统工具或为 profiler 盲目降级。此结论只覆盖目前路径和模型。
4. Triton Ascend 声明依赖通用 Triton 3.5.0；两包共用 35 个文件。第一次安装全部匹配通用 Triton，仅 7 个也匹配 Ascend。最后重装带 hash 锁的 Ascend 后，两个环境共用文件均 35/35 匹配 Ascend RECORD，运行 backend 为 ascend。必须检查实际文件，不能只凭 pip check 或包版本列表。
5. Ascend isolated builder 最初选择 setuptools 84.0.0 等最新工具，已将实际观察版本单独锁定，并在重建环境通过 `--build-constraints` 重构建/复审；core 仍用主候选锁的 setuptools 80.10.2，不混用两类构建工具。

本轮 uv 下载/隔离构建复用了默认 `/root/.cache/uv` 私有公共依赖缓存；不是全局 Python 安装，也没有清理缓存。未来恢复可指定项目内 UV_CACHE_DIR。CANN、驱动、平台镜像和本地引擎制品不受 Python hash 锁覆盖，不能宣称整个环境制品锁已完成。

## 真机运行与失败保留

全部模型请求使用公共只读 Qwen2.5-14B-Instruct：TP=1、BF16、eager、APC、chunked prefill、8k context、8 seq、2048 token budget、0.70 memory、CPU binding 关闭、必要 Ascend 插件 allowlist、loopback HTTP、私有 IPC。它们是功能配置，不是性能最优值。

| 私有运行记录 | 结果与严格范围 |
|---|---|
| probe-01 / service-venv | pass：health、普通/重复/SSE 完成；真实 BlockStored；父进程 exit 0、无强制组清理 |
| probe-02 / rebuilt venv | fail：HTTP 端口预占检查 Errno 98，未启动后端；保留失败，换新端口，不清理未知服务。没有据此认定模型或依赖失败 |
| probe-03 / rebuilt venv | fail：HUST 自带 Torch/NPU preflight 固定 20 秒超时，模型未加载；父进程 exit 1、无强制组清理 |
| 独立分段预检 | torch 导入 17.86 秒，设备初始化累计 19.11 秒，分配累计 19.46 秒；NPU available=true、zero allocation 成功。这是一次诊断，不是微基准或性能成绩 |
| probe-04 / rebuilt venv | pass：分段预检后正常完成请求与真实事件，正常关闭；不能单独用于证明全新 import cache 冷启动修复 |
| probe-05 / constrained builder + 私有全新 pycache | pass：保留 preflight、全新私有字节码缓存目录，health/普通/重复/SSE/真实 BlockStored 全部通过；父进程 exit 0，无强制组清理 |

第一轮 prompt=648 tokens，每次生成 24 tokens；SSE 24 content chunks，普通和重复结果各 147 字符。事件 sequence=0、5 blocks、640 tokens、block_size=128；medium 为 GPU 是协议历史标签，不意味着设备为 NVIDIA。指标 prefix queries=1944、hits=1280，实际 KV 632 blocks/80896 tokens；只有功能观察，未测达标容量提升，也未完整验模型准确性。

原探测器为了避免写共享目录设置 `PYTHONDONTWRITEBYTECODE=1`，新 venv 因而无法缓存庞大 Torch/NPU imports，放大 20 秒预检风险。已改为允许字节码并重定向至每次私有 output/cache/pyc；不关闭 preflight、不修改引擎、不写公共模型或系统 Python bytecode。全新 cache 对照已通过，支持该设置是本次冷启动失败的影响因素；没有通过穷举或严格重复统计证明它是所有未来超时的唯一原因。

## 代码与自检

- audit_runtime 显式接受 vllm-ascend-hust；同导入树的两 Ascend 发行包并存直接失败，报告仍不自动置位环境/ABI兼容。
- smoke_backend 加入私有 IPC 接线、路径长度和互斥检查、HUST tagged map 摘要、异常类型拒绝和脱敏。旧 array/TCP 入口保留但不宣称跨节点 TCP 已通过。
- 存储样本不等于完整 G2；探测器仍不建立生产目录，完整事件/关闭/路由/性能标志保持 false。
- 本地 CPU 79 项、mock 3 项、ruff 检查/格式通过；末次 bytecode/异常 guard 修订后，sdist/wheel 重新构建及干净 wheel 检查通过。远端源码同步后的 CPU/mock 与 CI 结果还需补入，不沿用旧提交成绩。
- 已自检变更的发行身份、失败关闭、socket/进程所有权、缓存写入边界和性能声明；这是代码自检，不是第二成员或独立审查通过。PR 需人工审查、不自动合并。

## 下一步与限制

最终冷缓存复验已通过，可记录本机 HUST eager 的局部可用组合；完整 G1 仍需收齐制品/镜像来源与可重复恢复说明，再冻结。随后 G2 验真实自然淘汰、清空、replay、断连、重启/epoch 和覆盖起点。HUST 源码有扩展恢复接口，但需按实际语义验收，不重新实现其旧 Router。

G3 再验证目标图模式、并发、取消、重复启停与持续运行；G4 才准入成熟 Router、使用两张卡做两个 TP=1。现有四卡足够，不申请更多卡。没有生产 Router、收益对比或 Manager 激活；MOD 继续 import_only。
