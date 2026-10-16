# 测试与 CI

当前真机工作遵循[四道顺序门槛](planning/runtime-event-gates.md)。只读 `tools/audit_runtime.py --output artifacts/runtime-audit-new.json` 检查实际调用解释器的包元数据和 `pip check`；未含必需核心包也失败。退出 0 仅表示 Python 依赖审计通过，不是 NPU/ABI/环境锁通过；详见[运行环境](runtime.md)。CPU 回归覆盖缺包、失败、超时、保留旧证据、来源 URL 脱敏和“依赖通过不冒充环境验收”。

在 uv sync --frozen --extra test 后运行：

```bash
uv run --frozen python tools/check.py --suite cpu
uv run --frozen python tools/check.py --suite integration
uv run --frozen python -m build
uv run --frozen python tools/verify_wheel.py
```

CPU 包含格式、静态检查、配置/CLI、真实 Manager 拒绝启用和未实现套件结果检查。integration 目前只测试合成 loopback 后端的健康、非流式响应、SSE 帧和 503；**尚未经过真实 Router，不能证明流式低延迟、取消传播或背压已正确**。

verify_wheel 在源码目录之外新建临时 venv，安装无运行依赖的 wheel，加载 entry point 与包内 manifest、运行 doctor；Manager schema/启用拒绝由 CPU 测试负责。发布前还需完整安装链、目标宿主及真实启停验收。

## 统一报告

tools/check.py 生成 artifacts/checks/<suite>.json，CPU / integration 同时生成 JUnit。退出码 0=通过，1=执行失败，2=参数错误，3=未实现/未运行。当前 npu-smoke、npu-routing、performance、release 是保留入口，返回 not-run 和 3，绝不静默成功。

在目标实现阶段补齐 pass/fail/skipped/not-run/inconclusive 的完整结果 schema；必须有真实测试才允许该套件通过。

`tools/smoke_backend.py` 是显式 `--execute` 的局部诊断入口，仅 Linux、指定本地模型/可执行文件、单卡 TP=1 eager、loopback、独立进程组、有限超时；可采样原生 KV 事件。使用方式见[运维](operations.md)。它还不包含图/取消/恢复/Router，不能替代完整 `npu-smoke`，保留入口仍返回 not-run。CPU 测试覆盖启动参数、SSE 完整性、事件脱敏和关闭边界，不启动设备。探测不加速包安装、不更改 MOD 的 import_only 状态。

## CI 策略

GitHub Actions 在 push main、PR 和手动触发时运行 Python 3.11/3.12、锁文件依赖、同一测试入口和干净 wheel 检查，保存证据 14 天。仅 contents:read，不发布、不部署、不占 NPU。锁定第三方 action 的不可变提交是后续供应链加固项，当前使用主版本标签，依赖版本由 uv.lock 固定。

NPU 先手动执行，后接受控 runner。不得将未审查 PR 放到有服务器凭据的 runner。真机测试必须预约/设备锁，清理只针对本次创建的资源。

## 待补覆盖（不能省略）

P1/P2：真实 Router 协议透传、即时 SSE、慢客户端、取消、发送前/后失败、排空与无可用副本。P3：身份隔离、事件乱序/缺口/恢复、代次失效、账本幂等和元数据上限。P4/P5：强基线、负对照、独立消融与过渡期成本。P6/P7：持续运行、跨节点、目标架构、版本拒绝、关闭回退和第二成员复现。

## Ascend 增量验收（计划，尚未实现）

CPU 契约新增能力/档案 schema、单位/unknown、指纹失配/过期/范围外回退、语义与性能失效范围、block/rank 映射。mock 只能验证合同逻辑，不能证明 NPU 图或事件能力。

硬件阶段按 [N0–N6](ascend-design.md) 实施：npu-smoke 验证整栈与单副本目标组合；npu-routing 验证两个 TP=1 副本的 APC/图/事件与生命周期；performance 校准前缀实际节时、混合阶段和 Host 开销；release 验证目标 aarch64 制品、MOD 真实加载/停用与降级。先固定主配置再做风险驱动组合覆盖，未测试组合不进入支持矩阵。

异步微基准在必要边界正确同步，服务测试不逐请求同步设备。图与 KV 冷热分别控制；采样诊断不用于正式成绩，observer 开关单独对照。完整版本/模型/图/内存/Host 指纹和独占条件随报告保存。手动测试仍用统一入口，新增 NPU CI 前遵守设备预约、凭据和 PR 审查约束。
