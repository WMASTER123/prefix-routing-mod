# 当前进展与执行顺序

更新：2026-10-17。本页是现行完成情况入口；[接手指南](项目接手指南_2026-10-17.md)集中记录框架、端口、三人所有权和未决边界。[上一阶段](progress-native-owner-history-20261016.md)和 [更早历史](progress-history.md)保留原证据边界。组件数与测试数不用于计算“完成百分比”。

## 本阶段已完成

1. **原生传输回归**：Linux x86_64 和 aarch64 的共享原索引/owner/真实 IPC/身份控制各 90 项 release 测试通过；固定完整 Router example test 36 项通过，原 mock HTTP 组合回归通过。重放发送纳入总期限，真实 socket 测试已加入 CI。
2. **真实公开请求链有限闭环**：原 HTTP 入口、真实 Qwen M1、Rust M7→同进程原 M2、基础 M4、M3 准入、原 M5 转发与真实 HUST 终态已同时联通。A 冷/A 热/B 热实际目标 A/A/B，后端 cached_tokens 为 0/384/384；真实流式取消取得核心 ABORTED 后才结清，重复终态不重复释放，最终 active=0。详见 [公开链证据](../evidence/native-public-chain-2026-10-16.md)。
3. **新分配两卡复验与稳定性**：两张 910B2、Qwen2.5-14B TP1/FP16/eager，原目录隔离/断连复验通过；20 分钟后端稳定性完成 177/171 请求、12272/11856 生成 token，正常退出，无强制清理或残留设备进程。该稳定性未启用终态观测，不能替代其长期留存验收，见 [证据](../evidence/npu-stability-2026-10-16.md)。
4. **M3 持久提交候选**：新增 Rust 单写提交包装与 SQLite WAL/FULL 候选，发送许可晚于持久提交，终态/M3/cursor 同一事务，恢复默认暂停并用 owner/generation CAS 围栏。审查补齐异步取消失败关闭、完整终态 source binding、歧义快照拒绝和整批预校验；x86_64/aarch64 release 各 48 项通过，本地 clippy 通过。aarch64 完整快照 commit 在 0/64/512 active 下 P50 为 0.673/0.809/2.320 ms，因此不选作默认生产热路径，见[证据](../evidence/durable-runtime-candidate-2026-10-16.md)。
5. **Core 终态窗口回收**：按固定 HUST 源码与 hash 守卫确认 internal ID 和一次性清理边界，Core 记录及重复保护只保留到唯一 Frontend ACK，不再形成进程累计请求上限；CPU 回归覆盖 ACK 前重复、ACK 后复用和外来流量。Frontend family 退役仍待受信 Router 归属方案，不把前缀当凭据，见[ADR](decisions/2026-10-16-terminal-source-retention.md)。
6. **增量存储与交接**：同一 SQLite WAL/FULL 适配器新增变化行布局，整批终态先验证再修改；最终 helper 57 项通过，包括真实子进程未提交退出与已提交 active 保债。最小 feature 只新增三个包、未升级原宿主依赖，ARM 完整候选已构建。两轮相同持久性对照中，512 active 行布局 P50 2.577–2.598ms，对照快照 4.239–4.665ms；0 active 尾延迟不优，不能外推服务收益。真实 commit→ACK 资格见[本轮证据](../evidence/durable-public-chain-2026-10-17.md)。交接文档已提供可独立开展的 A/B/C 工作与共享文件规则，生产 ABI 仍未冻结。
7. **受信终态退役候选**：首版部署边界确定为受管副本池仅接受 Router 业务入口；默认 `observe_only`，只有显式 `exclusive_router_v1` 才允许在 M3 持久提交、ACK 后按同一水位退役完整 family。CPU 已覆盖累计 600 family、重复退役、旧 cursor 和默认拒绝；真实单副本 completion/cancel 的 commit→ACK→retire 曾在服务器上返回通过，但原始报告未在服务器被平台回收前归档，不能作为最终可复验证据。双副本首轮暴露 Rust 严格身份解码缺字段，调用方已修复并补计数单调/上限反例；修订后 ARM 测试链接和双 NPU 复验因服务器回收未完成。详见[本轮记录](../evidence/terminal-retirement-2026-10-17.md)。

固定基线为 HUST Core ced6857 / Ascend b47795c，Router91b65ee；未更换引擎、CANN 或全局 Python。服务使用项目隔离环境及窄 PYTHONPATH，本 MOD wheel 构建、干净安装和导入验证通过。当前平台镜像的服务 venv 全量 `pip check` 仍报告若干预装工具缺失可选依赖，不能再写成整环境依赖审计通过；这些项未由本 MOD 引入，需在冻结运行镜像时单列处理。

## 组件状态

| 组件 | 已实际完成 | 冻结/交付前剩余 |
|---|---|---|
| M1 | 真实本地 Qwen tokenizer、完整块摘要，已装入公开字符串 completion 链 | 输入/输出预算和一次分词开销；chat/template、数组/token-id 不在当前支持范围 |
| M2 | 复用原索引/Directory/Pool；真实 Rust 订阅写入同进程目录，连续性/父链/总配额/失信/实例围栏 | 大工作集/长期资源与并发成本；原生已热/超窗恢复不支持 |
| M3 | 条件准入、发出后保债、真实完整终态结清、幂等、增量存储及真实 commit→ACK、磁盘保债/暂停重开有限双 TP1 资格；受信 family 精确退役候选与 600 family CPU 回归 | 修订后真实退役报告归档、累计长测、强制退出及生产恢复；生产默认未选 |
| M4 | 基础可信正命中优先/最低在途回退，真实目标和缓存结果符合预期 | 配对开销与强基线；优化策略后置 |
| M5 | 复用原 Router HTTP/SSE，公开入口、真实转发及生成后取消通过 | 生产启停/排空、并发配置竞争/故障和可发布二进制 |
| M6 | CPU/源码/原生 CI、可重复装配、手动 NPU 公开链/稳定性及原始报告 | 同底座开销、持续服务、强基线和多阈值容量曲线 |
| M7/MOD | 固定版本映射、默认关闭的真实入口/终态适配、独立身份接口和原生 IPC | 生命周期、长期观测回收、正式 Manager 激活与升级逐项认证 |
| M8 | 边界和软计划契约 | 尚未实现；基础性能证据之前不加入 |

当前是有限真实接线通过，生产 ABI 尚未冻结，MOD 仍 `import_only`。没有容量提升或优于强基线的结论；原生流无 publisher epoch/完整共同水位快照，不能把本地连接代次称为后端 epoch。

## 接下来集中完成什么

1. 受信归属与 retire 接口候选已实现；下一台两卡服务器先完成固定 Rust source-control 测试和修订后二进制构建，再重跑单副本 commit→ACK→retire并把原始报告归档，随后重跑双副本公开链。中断前旧二进制失败不能被新代码结果覆盖。
2. 在同一修订二进制上补丢失 retire 回复、控制实例替换、Router 强制退出与旧 cursor 重启；累计超过 512 family/1024 child 并记录 family/child/进程 RSS 趋势。未知负债保留，禁止用 TTL 或请求头前缀清理。
3. 对直连、原 Router、同底座仅加接线做配对 Host 开销，排除重复分词/远程目录查询/持久化放大；通过后冻结生产目录、类型、线程/时钟、配置、错误及启停语义。
4. 现在可以按 A(M7/M2)、B(M1/M4/M5)、C(M3/M6/交付)指定受现有端口约束的独立任务；生产目录、恢复/退役及 Manager 交付仍由集成负责人先收敛、交叉审查后冻结。不等待 M8 或最终算法，也不把尚未验收的恢复语义宣布为冻结。见[工作包](planning/component-work-packages.md)和[接手指南](项目接手指南_2026-10-17.md)。

用户暂无固定 SLO：容量评测仍按预冻结的多阈值 TTFT/生成停顿与输入/输出预算分组，保留失败分母；chunk 间隔不冒称逐 token 间隔。尚未开展正式容量实验。

## 测试与审查

代码 `3264050` 的既有成绩仍按原证据保留。当前退役增量本地 Python 全量为 585 通过/7 跳过，Ruff 检查与格式检查通过；修订后的 Rust source-control 测试、ARM 完整宿主构建和双 NPU 结果尚未取得，等待新服务器重跑。离线 wheel 的干净安装验证通过；当前服务 venv 的全量 `pip check` 有平台预装工具依赖缺口，不能计为本轮通过。

PR23 已合并到阶段分支 `codex/online-kv-directory`；后续为[草稿 PR24](https://github.com/WMASTER123/prefix-routing-mod/pull/24)，`main` 尚不包含全部研发进展。实机代码输入为 `3264050`，文档收尾提交另记。真实新持久链 `artifacts/t9` 通过且三个自有进程正常退出、两卡无残留。没有自动合并，也没有正式发布。

已完成自审、自动回归和上述实机子集；独立人工审查/合并未完成，不自动合并。Linux CPU 可推进持久化/协议；再次真实持续服务与恢复回归需要两个 TP1 NPU。测试完成清理自有进程，不运行空载保活负载。
