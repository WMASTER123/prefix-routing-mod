# HUST 候选锁，不是发布支持声明

2026-10-13 当前新增目标为 Core ced6857 / Ascend b47795c，见[迁移入口](../../docs/planning/hust-version-adapters.md)。`hust-cedb477.json`、`hust-cedb477-py312-aarch64.txt` 和 `hust-cedb477-extra.txt` 为独立配方，不能与下方归档锁拼用。新入口恢复已核验的 owner-local wheel；完整公开源码构建/分发还未交付。下方继续保留旧组合的恢复和证据范围。

目标：Linux aarch64 / Python 3.12.13 / 910B2。源码必须固定为 [ADR](../../docs/decisions/2026-10-11-hust-runtime-candidate.md) 中的两个完整提交；禁止把此锁用于最新主干或上游 Ascend。

- `hust-legacy-20260831-py312-aarch64.txt`：两仓库 common/runtime/empty build requirements 联合解析所得 186 包，包含制品 SHA256。文件本身的 LF SHA256 为 `7bd3f97e9a632833a55209a285b2f3e2ff855e9ec1ce2136465a7de5f008d587`；不含两个本地 editable 引擎，源码提交与构建另锁。
- `ascend-hust-build.in` / `ascend-hust-build.txt`：Ascend 隔离构建工具的观察版本和带 hash 输出；不替代 core 的 `<81` setuptools 约束。core empty build 复用主候选锁中的构建工具，Ascend 单独使用 build constraints。

解析/安装索引明确为 `https://pypi.org/simple` 和 `https://mirrors.huaweicloud.com/ascend/repos/pypi`，`unsafe-best-match` 来自 HUST README 的联合安装配方，只允许这两类已记录来源。使用 `--require-hashes` 验证依赖下载；构建隔离另需 `--build-constraints`，普通 Python 依赖 hash 不覆盖本地引擎和 CANN。

`triton-ascend==3.2.1` 自身声明依赖 `triton==3.5.0`，两者存在 35 个共用文件。本次 uv 并行安装结果曾由通用 Triton 覆盖；必须最后重装锁定 Ascend 并校验 RECORD 对应文件哈希、确认运行 backend 为 ascend。不能删除它声明的通用 Triton 包来让 pip check 报绿，也不能只看发行版本号。

详细执行与真实结果维护在 [运行环境](../../docs/runtime.md)。这是候选快照；CANN 9.1 与仓库参考 CANN 9.0 的差异、首次启动、完整 KV 恢复、图/并发和性能仍须按阶段验证。所有原始日志留忽略的 artifacts，不上传权重、原始 prompts 或凭据。

`hust-restore.json` 将上述源码、两份锁文件的 LF hash、Python/uv 和进程级路径固定为同机研发恢复配方。使用[恢复入口](../../docs/runtime.md)验证它；制品锁不覆盖 CANN/驱动和 editable 引擎。构建 constraints 固定隔离 builder 的版本，不等于整个工具链已经成为可移植发行镜像。
