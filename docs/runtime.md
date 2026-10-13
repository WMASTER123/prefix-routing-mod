# 服务运行环境：准入与重建边界

2026-10-13 当前迁移覆盖：用户选定 Core ced6857 / Ascend b47795c；新版本 profile、原生 KV 规范化和独立制品恢复见[版本适配入口](planning/hust-version-adapters.md)。以下固定旧源码、旧 publisher/终态 hook 和历史验收仅属于原提交；不能据此在新版本启用。新版可靠终态、共同水位恢复与在线目录接线仍须逐项验收。

更新：2026-10-11。**统一恢复入口已在全新目录通过，本机研发 G1 技术证据齐备，正式冻结待人工复核。** 见[本次恢复证据](../evidence/w1-runtime-restore-2026-10-11.md)。继续 G2 事件测试；可移植发布制品与完整兼容尚未验收。

## 当前执行：cedb477 wheel 候选

当前单后端研发目标为HUST Core ced6857 / Ascend b47795c；188包隔离环境、全新重建和两卡顺序单实例FP16/eager子集通过，见[本轮证据](../evidence/hust-version-adapters-2026-10-14.md)。推荐入口为 `python -m tools.restore_wheel_runtime`，依赖固定wheelhouse，见[迁移指南](planning/hust-version-adapters.md)。下方旧editable配方只用于旧提交；旧环境保留，未升级系统。

## 历史执行：归档 HUST 隔离候选（2026-10-11）

用户纠正后，目标改为两份指定 HUST 归档 main，不再盲选官方版本。新服务器预装 origin 已核实指向上游。HUST 的联合依赖可以满足，两个无 system-site-packages 的服务 venv 均通过窄 CANN 路径下的 Python 依赖审计；真实 Qwen2.5-14B eager、IPC BlockStored 和干净 venv 重建复验均通过。私有全新 import cache 对照也通过；[逐次证据](../evidence/hust-runtime-candidate-2026-10-11.md)保留此前失败。已可用于下一步局部验证，不等于整栈制品冻结、图/完整恢复/性能放行。

源码与边界见[ADR](decisions/2026-10-11-hust-runtime-candidate.md)，[候选锁目录](../runtime/candidates/README.md)含 186 包完整 hash 锁与 Ascend 隔离构建工具锁，不是最终发布基线。core 发行名仍为 vllm；Ascend 发行名是 vllm-ascend-hust，导入 vllm_ascend。不混装上游 Ascend。

W2 增量只把本项目无运行依赖的 wheel 安装到该服务 venv，用于可选 publisher 实验；未改引擎 editable 源码、全局依赖或原锁。实验 general plugin 默认关闭，仅支持固定整栈及 publisher 源文件指纹，不从 G1 结果推导任意版本的 hook 兼容。复用本项目内来源为本地 clone 的恢复源码时，身份工具逐级核对干净固定提交和 canonical origin；不重写 origin 或放宽到任意本地路径。

## 历史归档组合恢复入口（W1，固定旧提交）

在确认设备空闲、HTTP 端口可用后手动执行。输入为本项目内干净且完整历史的两份 HUST 源码；目标目录必须是新的、位于本项目 artifacts 下。执行会下载锁定依赖、构建 editable 引擎并启动临时单卡模型，不用于重建系统环境。

```bash
cd /root/work/prefix-routing-mod
.venv/bin/python tools/restore_runtime.py --execute \
  --core-source artifacts/hust-runtime-20261011/vllm-hust \
  --ascend-source artifacts/hust-runtime-20261011/vllm-ascend-hust \
  --python /usr/local/python3.12.13/bin/python3 \
  --uv .venv/bin/uv \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18220 --output artifacts/w1-restore-20261011
```

入口检查锁、源码、来源、完整历史、平台/版本及项目空间；新建环境不继承 system-site-packages，缓存留在目标目录。移除 UV/PIP 来源和构建覆盖变量并使用显式索引，窄 CANN PYTHONPATH 与必要动态库环境按已验证配方保留。最后覆盖 Triton Ascend，核验其 RECORD 与实际导入的 backend、引擎来源及加载库，再执行普通/重复/SSE/真实 IPC 存储探测。

安装阶段有超时及自有进程组清理，首个失败终止后续阶段，失败目录保留。模型探测使用已有工具的启动/请求/清理期限，不通过外层超时杀死其控制器而遗留独立引擎进程组。详细报告、版本文件和加载库路径仅留私有 artifacts；报告成功不自动将 environment_gate_passed 或完整 KV/性能标志置为 true，G1 的研发冻结需审查相应证据。

必须显式审计 HUST：

```bash
PYTHONPATH=/usr/local/Ascend/cann-9.1.0/opp/built-in/op_impl/ai_core/tbe \
  artifacts/hust-runtime-20261011/service-venv-rebuild/bin/python tools/audit_runtime.py \
  --ascend-distribution vllm-ascend-hust --output artifacts/hust-audit-new.json
```

默认 CANN `python/site-packages` 会暴露可选 ms-service-profiler 等包，产生 pandas/OpenTelemetry 缺失或约束冲突；不能为未使用的 profiler 盲目下调服务依赖。本次只保留 OPP/TBE 路径、继续继承必要的 CANN 动态库环境，再实际验证模型。该设置只限进程，不重写宿主环境；图或其他算子需要额外 Python provider 时先核验，不保证窄路径适用所有模型。

联合安装后还需重新安装锁定的 Triton Ascend，检查共用文件实际符合它的 RECORD；普通 pip check 不检查文件覆盖。第一次确认 35/35 共用文件一致，运行 backend 为 ascend；导入显示 triton.__version__=3.2.0，是该制品源码版本，不伪造为发行元数据 3.2.1 或通用发行元数据 3.5.0。

构建步骤来自 HUST README：core 用 `VLLM_TARGET_DEVICE=empty`、`--no-build-isolation --no-deps`，Ascend 用 `COMPILE_CUSTOM_KERNELS=0 --no-deps`；前提是完整联合依赖已装入且安装后再次审计。只构建默认关闭自定义 kernel 的可行性版本，不冒称最佳性能。构建隔离工具也必须锁定，避免每次恢复选择最新 setuptools。

HUST CLI 的 Torch/NPU preflight 固定 20 秒。重建首次启动在这里超时，单独分段诊断总计 19.46 秒，其中导入约 17.86 秒，设备可用且零张量分配成功；这是一次测量，不足以排除所有 ABI 风险。原探测器设置 `PYTHONDONTWRITEBYTECODE=1` 会使新 venv 无法生成 import cache，现改为私有 `PYTHONPYCACHEPREFIX` 并保留预检启用；全新 cache 复验通过。不能直接关闭 preflight、加大外部 startup timeout 或改引擎常量后称为修复。Host 配额/存储变化后仍需复验，不能保证 20 秒适用于所有机器。

以下是预装上游的历史阻塞，不是 HUST 当前结论。

项目 `.venv` / `uv.lock` 用于 MOD 的 CPU 开发与 CI，不承载推理栈。预装系统 Python 保留作为历史功能对照。新的服务环境必须位于项目私有目录，不继承系统 site-packages，不修改驱动、CANN、公共模型或全局 Python。

## 历史阻塞：预装上游与只读检查

固定 vLLM 0.23.0 + Ascend 0.23.0 的 Python 依赖有不可同时满足的 NumPy 约束，详见[审计证据](../evidence/runtime-gate-2026-10-11.md)。这是安装/依赖一致性问题，不等于已证明模型计算错误；之前 eager 功能成功也不能覆盖它。

在计划用于服务的解释器下运行：

```bash
python tools/audit_runtime.py --output artifacts/runtime-audit-new.json
```

该工具只读包元数据并调用同一解释器的 `pip check`，不导入 torch、初始化设备、安装包或执行源仓库代码。已有输出拒绝覆盖；缺少核心包、pip 不可用、依赖检查失败/超时都不通过。它保存全部包版本、声明依赖、安装位置、可获得的来源及解释器路径；普通安装的来源未知时保持 null。它不能证明 wheel 来源、ABI、执行正确性或环境隔离，相关标志始终 false。完整报告只保存到私有 artifacts；包含内部路径，不能直接上传公共仓库。

最小冲突重现（Linux aarch64 / Python 3.12 / uv 0.11.19，只解析，不安装）：

```bash
.venv/bin/uv pip compile --python /usr/local/python3.12.13/bin/python3 \
  --index-url https://pypi.org/simple \
  runtime/rejected/ascend-023-numpy-conflict.in
```

预期非零退出并报告 NumPy 约束冲突。文件路径中的 `rejected` 表示该输入被拒绝，不是推荐运行环境。URL hash 来自官方发布制品，固定的是本次检查的具体 wheel。

## 环境准入交付物

本轮新分配上的[启动诊断](../evidence/g2-recovery-regression-2026-10-11.md)复现冷 bytecode 的 torch 导入 22.36 秒超过原 20 秒 preflight。重复探测提供显式私有 bytecode 复用入口，先导入/来源审计，再启动；见[操作步骤](operations.md)。这属于环境准备条件，不能把缓存准备后的成绩称为原始冷启动，也不代表已经修改恢复入口或完成可移植镜像。

1. 固定 Python/aarch64、驱动/固件、CANN/NNAL、核心包与源码提交；本机已核验固件 9.0.0.0.205；镜像 digest 等未知来源明确记录。
2. 完整服务依赖解析、锁文件、下载来源与制品 SHA256；本地构建还需源码提交、构建依赖和构建日志。只有包版本列表不能叫可复现锁。
3. 全新隔离环境安装并 `pip check` 通过；不得忽略冲突、伪造版本或默改 wheel 元数据后称为官方制品。
4. 核验 Triton/Ascend 共享文件的实际归属和安装顺序、动态库加载来源、CANN 环境路径；不是仅检查 Python 包名。构建环境继承系统包时必须单独标明，不得充当隔离服务环境。
5. 同一环境完成单卡 Qwen2.5-14B 健康、非流式、重复前缀、SSE 与原生事件基础探测；保留启动参数和插件 allowlist。图与完整恢复另有门槛。
6. 第二次干净恢复可复现上述结果，再冻结基线。任何候选更换均不继承旧 NPU 测试成绩。

历史候选检查：此前用户允许评估官方配套；当时查到的 0.23.0.post1、0.26.0rc1、0.27.1rc1 仍固定 Triton Ascend 3.2.2。此记录不代表所有官方组合不可用，也不是下一步继续换官方版本的指令。当前采用指定 HUST 候选，按 W1 收尾，不混装这两条来源。

如果下一步需要自行放宽打包约束、使用开发主干构建或接受已知冲突，先做范围决策与 ADR，说明官方支持边界、风险、测试及回退，再实施；不得把 `--no-deps` 成功当作兼容验证。
