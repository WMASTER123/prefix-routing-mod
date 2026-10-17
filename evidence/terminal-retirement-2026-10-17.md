# 受信终态退役候选：结果、失败与重跑边界

日期：2026-10-17。代码链：`1c11b3c`（退役协议）、`ed05639`（Rust 身份字段同步）、`9340f16`（审查补充计数关系与无退役缩减拒绝）。状态：CPU 通过；真实单副本仅有未归档的临时通过观察；修订后 ARM 与双 NPU 未完成。不得作为生产、长期回收或性能资格。

## 改动范围

- 默认保持 `observe_only`；只有显式 `exclusive_router_v1` 才将真实 HUST family 标为受管并开放退役。
- 顺序固定为 M3 持久终态/cursor → 源 ACK → 同 observer epoch、同水位 retire。退役只删除已完整结束且受管的 family/child/status；未决、unknown、外部 family 保留。
- 复用既有 HUST endpoint plugin 和私有控制 token，新增 `POST /prefix-routing/terminal/retire`；不修改 HUST scheduler、分配、淘汰、KV 数据面或 NPU 执行。
- 首版信任边界是专属受管副本池、后端仅回环监听、Router 覆盖客户端 request ID、每进程随机命名空间。共享后端直连不在首版支持范围。
- 固定 Rust `HustSourceControl` 保留 `deny_unknown_fields`，显式接收 `reserved_children`、`acknowledged`、`retired_through`、`ownership_mode`，检查上限、顺序、模式稳定和无退役时资源计数不得减少。

## 已取得的确定结果

- Windows 本地完整 Python：585 passed、7 skipped；Ruff check/format 均通过。
- CPU 状态机覆盖：累计 600 个 family 的小窗口运行、重复退役幂等、旧 cursor 失败、observe-only 拒绝、binding/水位不符、未决负债保留。
- wheel 构建、干净安装和新 API 导入在此前步骤通过。当前平台服务 venv 的全量 `pip check` 报告预装工具的可选依赖缺失；未修改全局环境，也未将其误报为本 MOD 引入或已解决。

## 实机经过

两张 910B2、固定 HUST Core `ced6857` / Ascend `b47795c`、Qwen2.5-14B-Instruct、TP1/eager。没有更换 CANN、驱动或全局 Python。

1. 单副本有限链曾在服务器目录 `artifacts/terminal-retire-real-20261017` 返回 `status=pass`，completion 与 cancel 都经过真实 Core，临时报告字段 `durable_terminal_retirement_verified=true`；Router/后端均返回 0，结束时无 NPU 残留。该目录在平台回收服务器前未复制到本地或生成校验归档，因此只记为诊断观察，必须重跑后才算可复验证据。
2. 双副本 `artifacts/t10-retention-regression` 在请求前失败：旧 Rust `SourceIdentity` 使用 `deny_unknown_fields`，无法接受 Python identity 新增字段，Router exit 101；两个模型均曾真实加载并被清理。这是接口调用方遗漏，不是 NPU、模型或 KV 数据面失败。
3. `ed05639` 同步字段后，审查再补 `9340f16` 的资源关系/单调反例。项目隔离 Rust 1.99 release 测试正在 thin-LTO 链接时，SSH `36.140.239.91:32093` 开始持续拒绝连接，服务器被平台回收。没有取得测试完成输出，也没有构建修订后的新 ARM 二进制；旧 `d92e856...` 不包含完整修复，禁止复用为最终验证制品。

## 下一台服务器的固定重跑顺序

1. 同步当前 Git head，保持固定 HUST/Ascend 和项目隔离环境；先运行 Python CPU、wheel 干净安装与环境审计，并把镜像预装依赖缺口单列。
2. 在固定 Router 私有复制中同步两个 `hust_source_control` 文件，运行精确 rustfmt、定向 Rust 测试，再离线锁定构建 `hust_runtime_probe`；保存源码、lock、二进制 SHA256。
3. 单卡重跑真实 completion/cancel 的 commit→ACK→retire及 retire 回复丢失重试，立即将有界报告/日志打包复制回本地。
4. 两卡重跑 A 冷/A 热/B 热、目录隔离、终态/退役、断连与实例替换；结束后确认两卡无自有残留进程。
5. 在同一二进制上运行超过 512 family/1024 child 的有界持续测试，记录 family/child/event 计数、进程 RSS、错误和恢复水位；之后才开始直连/原 Router/仅接线的 Host 开销对照。

任何一步失败都保留原报告并使用新输出目录重试，不覆盖失败；没有归档、哈希和清理证据时不得把临时终端输出写成通过。
