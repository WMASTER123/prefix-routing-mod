# 服务运行环境：准入与重建边界

更新：2026-10-11。**尚无通过准入的服务锁文件；不要把候选输入当作可安装基线。**

## 最新执行：指定 HUST 隔离候选

用户纠正后，目标改为两份指定 HUST 归档 main，不再盲选官方版本。新服务器预装 origin 已核实指向上游。HUST 的联合依赖可以满足，两个无 system-site-packages 的服务 venv 均通过窄 CANN 路径下的 Python 依赖审计；真实 Qwen2.5-14B eager、IPC BlockStored 和干净 venv 重建复验均通过。私有全新 import cache 对照也通过；[逐次证据](../evidence/hust-runtime-candidate-2026-10-11.md)保留此前失败。已可用于下一步局部验证，不等于整栈制品冻结、图/完整恢复/性能放行。

源码与边界见[ADR](decisions/2026-10-11-hust-runtime-candidate.md)，[候选锁目录](../runtime/candidates/README.md)含 186 包完整 hash 锁与 Ascend 隔离构建工具锁，不是最终发布基线。core 发行名仍为 vllm；Ascend 发行名是 vllm-ascend-hust，导入 vllm_ascend。不混装上游 Ascend。

必须显式审计 HUST：

```bash
PYTHONPATH=/usr/local/Ascend/cann-9.1.0/opp/built-in/op_impl/ai_core/tbe \
  artifacts/hust-runtime-20261011/service-venv/bin/python tools/audit_runtime.py \
  --ascend-distribution vllm-ascend-hust --output artifacts/hust-audit-new.json
```

默认 CANN `python/site-packages` 会暴露可选 ms-service-profiler 等包，产生 pandas/OpenTelemetry 缺失或约束冲突；不能为未使用的 profiler 盲目下调服务依赖。本次只保留 OPP/TBE 路径、继续继承必要的 CANN 动态库环境，再实际验证模型。该设置只限进程，不重写宿主环境；图或其他算子需要额外 Python provider 时先核验，不保证窄路径适用所有模型。

联合安装后还需重新安装锁定的 Triton Ascend，检查共用文件实际符合它的 RECORD；普通 pip check 不检查文件覆盖。第一次确认 35/35 共用文件一致，运行 backend 为 ascend；导入显示 triton.__version__=3.2.0，是该制品源码版本，不伪造为发行元数据 3.2.1 或通用发行元数据 3.5.0。

构建步骤来自 HUST README：core 用 `VLLM_TARGET_DEVICE=empty`、`--no-build-isolation --no-deps`，Ascend 用 `COMPILE_CUSTOM_KERNELS=0 --no-deps`；前提是完整联合依赖已装入且安装后再次审计。只构建默认关闭自定义 kernel 的可行性版本，不冒称最佳性能。构建隔离工具也必须锁定，避免每次恢复选择最新 setuptools。

HUST CLI 的 Torch/NPU preflight 固定 20 秒。重建首次启动在这里超时，单独分段诊断总计 19.46 秒，其中导入约 17.86 秒，设备可用且零张量分配成功；这是一次测量，不足以排除所有 ABI 风险。原探测器设置 `PYTHONDONTWRITEBYTECODE=1` 会使新 venv 无法生成 import cache，现改为私有 `PYTHONPYCACHEPREFIX` 并保留预检启用；全新 cache 复验通过。不能直接关闭 preflight、加大外部 startup timeout 或改引擎常量后称为修复。Host 配额/存储变化后仍需复验，不能保证 20 秒适用于所有机器。

以下是预装上游的历史阻塞，不是 HUST 当前结论。

项目 `.venv` / `uv.lock` 用于 MOD 的 CPU 开发与 CI，不承载推理栈。预装系统 Python 保留作为历史功能对照。新的服务环境必须位于项目私有目录，不继承系统 site-packages，不修改驱动、CANN、公共模型或全局 Python。

## 当前阻塞与可重复检查

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

1. 固定 Python/aarch64、驱动/固件、CANN/NNAL、核心包与源码提交；固件等未知信息明确保留待核验。
2. 完整服务依赖解析、锁文件、下载来源与制品 SHA256；本地构建还需源码提交、构建依赖和构建日志。只有包版本列表不能叫可复现锁。
3. 全新隔离环境安装并 `pip check` 通过；不得忽略冲突、伪造版本或默改 wheel 元数据后称为官方制品。
4. 核验 Triton/Ascend 共享文件的实际归属和安装顺序、动态库加载来源、CANN 环境路径；不是仅检查 Python 包名。构建环境继承系统包时必须单独标明，不得充当隔离服务环境。
5. 同一环境完成单卡 Qwen2.5-14B 健康、非流式、重复前缀、SSE 与原生事件基础探测；保留启动参数和插件 allowlist。图与完整恢复另有门槛。
6. 第二次干净恢复可复现上述结果，再冻结基线。任何候选更换均不继承旧 NPU 测试成绩。

用户已允许评估官方配套候选。已查的 0.23.0.post1、0.26.0rc1、0.27.1rc1 仍固定 Triton Ascend 3.2.2，不能仅通过更换这些 Ascend 版本宣告修复。尚未评估所有历史版本或所有镜像，不声称所有官方组合都不可用。

如果下一步需要自行放宽打包约束、使用开发主干构建或接受已知冲突，先做范围决策与 ADR，说明官方支持边界、风险、测试及回退，再实施；不得把 `--no-deps` 成功当作兼容验证。
