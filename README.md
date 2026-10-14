# Prefix Routing MOD

当前状态统一见[进度](docs/progress.md)、[架构](docs/architecture.md)与[在线目录证据](evidence/online-kv-directory-2026-10-14.md)。历史试验保留在对应evidence和历史进度，不作为当前任务清单。

主要面向华为 Ascend NPU 上 vLLM / Ascend 同模型多副本服务的前缀路由综合套件。目标是在固定 NPU 与 Host 资源下提高同时满足 TTFT、生成停顿和完成率要求的服务容量，优先复用成熟路由宿主和索引；设备执行假设须在目标平台验证。

当前为研发版本：真实Router库→HUST→同一账本的有限闭环、原生KV→常驻原索引和双TP1目录隔离已有实机子集。公开HTTP入口的完整组件装配、生产恢复/GC、MOD完整启停和容量收益仍待验收。Manager可发现描述，`import_only`保持，不可启用尚未交付的生产功能。

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

可选NPU工具仅由操作者显式执行，使用项目隔离环境与公共本地模型；见[运维](docs/operations.md)、[在线目录计划](docs/planning/online-kv-directory.md)和[候选锁](runtime/candidates/README.md)。当前实机组合为指定HUST ced6857/b47795c；历史旧版publisher资格不外推到新版。

## 文档与实施

- [文档总览](docs/README.md)：架构、契约、规范、测试、运维、决策。
- [当前进度与后续工作](docs/progress.md)：完成证据、未决项、P0–P7 门槛。
- [当前详细实现计划](docs/planning/前缀路由MOD_整体实现计划与工程验收.md)：三人协作、NPU 校准和完整阶段验收。
- [Ascend 设计约束](docs/ascend-design.md)：复用边界、能力/执行档案、N0–N6 实验；不宣称已经实现。
- [贡献约定](CONTRIBUTING.md) / [AI 工作边界](AGENTS.md)。

首版仅考虑一个外部 Router 接入已有服务。暂不做 P/D 分离、分层缓存、KV 搬运、异构调度、引擎淘汰改造。原生接口优先，observer 仅在证实必要后添加。当前 Python 包是工具和 MOD 元数据载体，不预先决定线上 Router 的实现语言。

License: Apache-2.0；模板来源见 [NOTICE](NOTICE)。
