# 测试与 CI

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

## CI 策略

GitHub Actions 在 push main、PR 和手动触发时运行 Python 3.11/3.12、锁文件依赖、同一测试入口和干净 wheel 检查，保存证据 14 天。仅 contents:read，不发布、不部署、不占 NPU。锁定第三方 action 的不可变提交是后续供应链加固项，当前使用主版本标签，依赖版本由 uv.lock 固定。

NPU 先手动执行，后接受控 runner。不得将未审查 PR 放到有服务器凭据的 runner。真机测试必须预约/设备锁，清理只针对本次创建的资源。

## 待补覆盖（不能省略）

P1/P2：真实 Router 协议透传、即时 SSE、慢客户端、取消、发送前/后失败、排空与无可用副本。P3：身份隔离、事件乱序/缺口/恢复、代次失效、账本幂等和元数据上限。P4/P5：强基线、负对照、独立消融与过渡期成本。P6/P7：持续运行、跨节点、目标架构、版本拒绝、关闭回退和第二成员复现。
