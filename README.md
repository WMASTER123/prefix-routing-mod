# Prefix Routing MOD

2026-10-14 后续：实际Router库的准入/转发已与真实HUST终态接同一实验账本，普通n2与流式取消、确认前额度拒绝、丢许可保债通过；不是生产HTTP启动器、在线KV或性能验收。当前优先阅读[组合计划](docs/planning/router-hust-terminal-chain.md)、[实机证据](evidence/router-hust-terminal-chain-2026-10-14.md)，以下同日较早记录保留原范围。

2026-10-14 最新：已在固定 HUST/Ascend 上通过可选真实终态子集，4 个 family/8 个 child 接原账本，重放和自有包装恢复通过。尚未把原生 Router 接入该链路、没有在线 KV 目录或性能结论；当前步骤见[实机接线说明](docs/planning/cedb477-live-terminal.md)和[证据](evidence/cedb477-live-terminal-2026-10-14.md)。下方较早记录只对应当时阶段。

2026-10-14：新版HUST入口/终态→原账本的固定源码CPU组合和独立CI门槛已补充；实际后端观测安装和跨进程链路仍待验收，见[当前接线计划](docs/planning/cedb477-terminal-boundaries.md)。

2026-10-13：新增 HUST 固定版本 profile、`backend-doctor` 和共用 KV 规范化入口；迁移至 ced6857 / b47795c 的步骤与能力缺口见[版本适配](docs/planning/hust-version-adapters.md)。环境和协议子集不代表生产 Router 或性能验收，MOD 仍 import_only。

主要面向华为 Ascend NPU 上 vLLM / Ascend 同模型多副本服务的前缀路由综合套件。目标是在固定 NPU 与 Host 资源下提高同时满足 TTFT、生成停顿和完成率要求的服务容量，优先复用成熟路由宿主和索引；设备执行假设须在目标平台验证。

**当前不是可运行的 Router。** 已提供静态配置预检、MOD 描述、CPU 测试入口、模拟后端和真实 HUST 回归工具；新增用户批准实验的可选 publisher hook，默认关闭且只面向固定版本。没有生产精确缓存路由或性能提升声明。Manager 可以发现描述，但必须拒绝启用未实现功能。

## 开发起步

安装 Git、Python 3.11/3.12 与 uv 后：

```bash
git clone https://github.com/WMASTER123/prefix-routing-mod.git
cd prefix-routing-mod
uv sync --frozen --extra test
uv run --frozen prefix-routing-mod doctor --config configs/example.json
uv run --frozen python tools/check.py --suite cpu
uv run --frozen python tools/check.py --suite integration
uv run --frozen python -m build
uv run --frozen python tools/verify_wheel.py
```

仓库目前私有，需要本人或协作者权限。尚未发布 PyPI 包。doctor 只校验配置，不探测后端、不证明兼容性。

2026-10-11：新增可选单卡后端诊断，公共 Qwen2.5-14B 已运行非流式/重复/流式请求；使用方法见[运维](docs/operations.md)，严格范围见[证据](evidence/qwen14b-preflight-2026-10-11.md)。这不是已完成的 Router，也不是加速结论。

最新已独立构建用户指定 HUST 配套源码，隔离依赖、Qwen14B eager/IPC 存储事件及 venv 重建复验通过。完整事件恢复、图模式及路由性能未验收；见[最新记录](evidence/hust-runtime-candidate-2026-10-11.md)与[候选锁](runtime/candidates/README.md)。

## 文档与实施

- [文档总览](docs/README.md)：架构、契约、规范、测试、运维、决策。
- [当前进度与后续工作](docs/progress.md)：完成证据、未决项、P0–P7 门槛。
- [当前详细实现计划](docs/planning/前缀路由MOD_整体实现计划与工程验收.md)：三人协作、NPU 校准和完整阶段验收。
- [Ascend 设计约束](docs/ascend-design.md)：复用边界、能力/执行档案、N0–N6 实验；不宣称已经实现。
- [贡献约定](CONTRIBUTING.md) / [AI 工作边界](AGENTS.md)。

首版仅考虑一个外部 Router 接入已有服务。暂不做 P/D 分离、分层缓存、KV 搬运、异构调度、引擎淘汰改造。原生接口优先，observer 仅在证实必要后添加。当前 Python 包是工具和 MOD 元数据载体，不预先决定线上 Router 的实现语言。

License: Apache-2.0；模板来源见 [NOTICE](NOTICE)。
