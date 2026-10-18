# 受信终态退役候选：结果、失败与重跑边界

日期：2026-10-17，2026-10-18 完成归档重跑。代码链：`1c11b3c`（退役协议）、`ed05639`（Rust 身份字段同步）、`9340f16`（审查补充计数关系与无退役缩减拒绝）。状态：CPU、aarch64 定向源控制、真实单副本退役和双副本公开链已通过并归档；长期资源趋势、强制退出接管和性能尚未完成。不得扩大为生产、长期回收或性能资格。

## 改动范围

- 默认保持 `observe_only`；只有显式 `exclusive_router_v1` 才将真实 HUST family 标为受管并开放退役。
- 顺序固定为 M3 持久终态/cursor → 源 ACK → 同 observer epoch、同水位 retire。退役只删除已完整结束且受管的 family/child/status；未决、unknown、外部 family 保留。
- 复用既有 HUST endpoint plugin 和私有控制 token，新增 `POST /prefix-routing/terminal/retire`；不修改 HUST scheduler、分配、淘汰、KV 数据面或 NPU 执行。
- 首版信任边界是专属受管副本池、后端仅回环监听、Router 覆盖客户端 request ID、每进程随机命名空间。共享后端直连不在首版支持范围。
- 固定 Rust `HustSourceControl` 保留 `deny_unknown_fields`，显式接收 `reserved_children`、`acknowledged`、`retired_through`、`ownership_mode`，检查上限、顺序、模式稳定和无退役时资源计数不得减少。

## 已取得的确定结果

- Windows 本地完整 Python：最终 587 passed、7 skipped（新增 stage-driver 协议与调用顺序反例后）；Ruff check/format 均通过。
- CPU 状态机覆盖：累计 600 个 family 的小窗口运行、重复退役幂等、旧 cursor 失败、observe-only 拒绝、binding/水位不符、未决负债保留。
- wheel 构建、干净安装和新 API 导入在此前步骤通过。当前平台服务 venv 的全量 `pip check` 报告预装工具的可选依赖缺失；未修改全局环境，也未将其误报为本 MOD 引入或已解决。
- GitHub head `3594171` 的 CPU 3.11/3.12、HUST source-methods 3.11/3.12、Native helper 与 Native index 三个 workflow/六个 job 均 success；Native index job 已格式化、编译并执行包含 `HustSourceControl` 新字段/反例的 Linux x86_64 回归。该结果不替代 aarch64 完整宿主构建。

## 实机经过

两张 910B2、固定 HUST Core `ced6857` / Ascend `b47795c`、Qwen2.5-14B-Instruct、TP1/eager。没有更换 CANN、驱动或全局 Python。

1. 单副本有限链曾在服务器目录 `artifacts/terminal-retire-real-20261017` 返回 `status=pass`，completion 与 cancel 都经过真实 Core，临时报告字段 `durable_terminal_retirement_verified=true`；Router/后端均返回 0，结束时无 NPU 残留。该目录在服务器变得不可访问前未复制到本地或生成校验归档，因此只记为诊断观察，必须重跑后才算可复验证据。
2. 双副本 `artifacts/t10-retention-regression` 在请求前失败：旧 Rust `SourceIdentity` 使用 `deny_unknown_fields`，无法接受 Python identity 新增字段，Router exit 101；两个模型均曾真实加载并被清理。这是接口调用方遗漏，不是 NPU、模型或 KV 数据面失败。
3. `ed05639` 同步字段后，审查再补 `9340f16` 的资源关系/单调反例。项目隔离 Rust 1.99 release 测试正在 thin-LTO 链接时，SSH `36.140.239.91:32093` 开始持续拒绝连接；只能确认实例已不可访问，不能从客户端断定是回收、重启还是其他平台故障。没有取得测试完成输出，也没有构建修订后的新 ARM 二进制；旧 `d92e856...` 不包含完整修复，禁止复用为最终验证制品。

## 2026-10-18 归档重跑

- 新实例 `32066`，逻辑设备 0/1 对应物理 3/4，两张 910B2 各 65536 MiB。仓库以完整 Git bundle 快进到 `4d59bc0`，不改 HUST/CANN/系统 Python。固定 Core `ced6857`、Ascend `b47795c`，实际包为 vLLM `0.30.0+empty` 与 vLLM-Ascend `0.25.1rc2.dev766+hust.20260903.4.gb47795c63`。
- 两张卡分别完成 Qwen2.5-14B 普通/重复/流式烟测，每次均观察到 5 个真实 KV block/640 token，自然退出、无强制进程组清理。首次失败是手动命令把镜像完整 `PYTHONPATH` 覆盖成单一 TBE 目录，使 `acl.so` 无法导入；保留失败报告后用原镜像环境通过，未换版本。
- 两个 `hust_source_control` 文件与提交源 SHA 一致；ARM `rustfmt` 通过，`cargo test --example hust_runtime_probe ... hust_source_control::tests` 实际执行 4 项全部通过。修订后 release 二进制 SHA256 为 `f5573141100381d42cdad1b551b61716858e4bac6d7f6ccdd1fe0a40101868d2`。宿主原有弃用/未使用方法 warning 保留，不冒充全宿主零 warning。
- 单卡链用协议匹配的 `live_terminal_chain` SHA256 `40b5ae1dcef6654cc078048c7512ebebb35814ae489067567a119fb5602d879a`。completion 两子请求与 cancel 单子请求均经真实 Core，分别得到 `FINISHED_LENGTH_CAPPED=2` 和 `FINISHED_ABORTED=1`；`durable_terminal_retirement_verified=true`，ACK 丢失后未决负债保留，Router/后端自然退出。本地归档 SHA256 `ee7893f900a6e67146e4593739acb5fc5e32890bb24f0bb16e3c40c9358ede40`。
- 双卡链使用修订后 `hust_runtime_probe`：实际选点 0/0/1，`cached_tokens` 为 0/384/384，cancel 得到 `FINISHED_ABORTED=1`，最终 active=0，`durable_commit_ack_and_paused_reopen_verified=true`；两个后端与 Router 均自然退出。公开归档明确排除含随机控制 token 的 `private-runtime-config.json`，双端 SHA256 为 `b9599049efaab937ae0543d5c56692523c01307cc0643eae785f310128997d52`。
- 一次误把双副本 config-driver 传给单卡 stage-driver 入口，在发出任何链阶段前 exit 101；后端子集仍通过并正常清理。已因此增加二进制协议标记预检，避免在昂贵模型加载后才发现类型不匹配。

本次完成的是有限真实正确性闭环，不是长期趋势、强制退出接管、图模式或性能资格。`interface.runtime_verified/routing_ready=false` 仍是保守生产 profile，不因实验子集通过而改真。

## 后续固定顺序

1. 在同一二进制和归属模式上运行超过 512 family/1024 child 的有界持续测试，记录 family/child/event 计数、Router/Frontend/Core RSS、错误与恢复水位。
2. 覆盖 retire 回复丢失、控制实例替换、Router 强制退出及旧 cursor 重启；恢复前始终暂停准入，未知负债不得回收。
3. 上述通过后再做直连/固定原 Router/仅接线的配对 Host 开销对照，然后才开始双副本强基线与多 SLO 容量曲线。

任何一步失败都保留原报告并使用新输出目录重试，不覆盖失败；没有归档、哈希和清理证据时不得把临时终端输出写成通过。
