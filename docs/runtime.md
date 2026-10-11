# 服务运行环境：准入与重建边界

更新：2026-10-11。**尚无通过准入的服务锁文件；不要把候选输入当作可安装基线。**

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
