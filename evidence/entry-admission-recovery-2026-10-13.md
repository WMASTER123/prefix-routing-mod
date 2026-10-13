# 完整入口关联、动态准入与启动交接：CPU 证据

日期：2026-10-13。基于 054ae0a，分支 codex/entry-admission-recovery。只在本项目 CPU 服务器和 Windows 工作区操作；不改 HUST、驱动、全局环境或 MOD 状态。

## 实现范围

- 新增 EntryCapture，复用 TerminalJournal 的全部 child 额度和 seal；从实际 group/internal ID 关联，不解析后缀。固定 13f11da 的十个文件 hash 守卫后，执行 serving/render wrapper、generate、普通 add_request、ID 分配和 frontend 记录删除方法。五种 completion/chat、stream/n 组合以及第 4 个 child 提交失败/取消，共 12 检查在 Windows、aarch64 通过。renderer、参数校验、collector、IPC、核心清理仍为替身，没有真实网络 HUST 或 NPU 证明；chat 只支持该固定入口的一个 rendered input。
- 原生 Router 同一接缝增加 reserve_async（默认适配原 reserve）和可选关联 header。实际 send 前仍检查 registry/control fence。动态 CPU 接收器从空账本按请求提交预留，重试/重复申请不发新许可；旧传输通道复用，不新造代理/索引/生产账本服务。
- 参考 Attempt 保留原时钟，当前 Worker 观测与它分开。旧传输按原时钟验证；可靠终态的新接收确认可用当前时钟，不跨时钟比较时间数值。显式持久化 runtime 交接只接受同一服务实例、完整新鲜观测、进位控制版本、从未复用的时钟及保留的在途计数；重开日志自动暂停。旧 dispatcher 已停是调用方的明确前置条件，不是工具自动探测/多 Router HA。

## 验证记录

初轮：Windows CPU 434 + 4 skips；Linux CPU 438；Linux integration 3；原生 helper 21。后续审查增加提交前/后独立进程退出和正常拒绝反例，最终成绩以下方为准。

代码 8b66f91fd169c57dc4a8817dd6e4f30b2c664449 的最终 Python 回归：Windows 437 + 4 平台 skips，Linux aarch64 441，integration 两端各 3；两端新 sdist→wheel 与干净安装通过，默认导入未加载 vllm/torch。原生 helper 最终 21 项通过。该 head 的 [CPU CI](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38359606510) 两个 Python 3.11/3.12 job 及 [helper CI](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38359606653) 均单独核验 completed/success；后续提交不继承 CI 状态。

本地、私人 GitHub 与服务器已同步 8b66f91。服务端暂存文件逐一核对为本轮 staging 或最终提交内容，再保存可恢复 stash 并 fast-forward，未覆盖其他工作。[草稿 PR #16](https://github.com/WMASTER123/prefix-routing-mod/pull/16) 基于 PR #15，已附到任务，另一成员独立审查待完成，不自动合并。

完整 aarch64 release 最终构建通过，约 10m23s；新 admission example SHA256 为 42f1ef09df74cf6704a46ff70df849510324e3193da7db58fb81e4b8c241b9f9，stream example 为 ae0be2d7734bc556033d1243b2d74756a2b254e2ad162ba6bb4aaa39317fabfc。实际结果：

| 测试 | 结果 |
|---|---|
| --online --dynamic | 空账本启动，1 条动态预留/2 条在线观察，0 条未使用预填记录；真实后端到达时已见提交，两个客户端 header 被唯一 attempt-1 覆盖；第二请求 503、无重发 |
| 动态未知与额度 | HTTP EOF 后保留 1 条负债；额度拒绝不关闭原接收 owner，admission_ready 仍为 true（表示通道可用，不代表有剩余额度） |
| 默认关闭/旧 --online | 两种模式均 24 尝试/46 条传输记录、22 未知/2 未发送；原在线模式 46 次确认在 producer 存活期间，40 个历史 fixture 未使用预留仍保留，共 62 条负债 |
| 原 stream example | 原 10 类断言/8 次 backend POST 通过，两种原生负载分支与 drop/idle/大小约束不退化 |
| 旧在线提交后丢 ACK | 2 条唯一观察、3 次确认尝试、1 次注入失败；原载荷重投后仍停止新派发，后端不重发，64 条含未使用 fixture 的负债保留 |

报告位于服务器 artifacts/entry-admission-20261013 的 http-dynamic-01、http-off-01、http-online-01、http-stream-01；进程正常退出，未触发强制清理。它们使用实际 Router 和 loopback mock HTTP 服务，不是实际 HUST 模型推理或吞吐成绩。Header 到真实 serving 方法和 frontend/core 的关联分别验证，尚未把 observer 安装后串成一个端到端链路。

旧通道 ACK 丢失复验报告为 http-ack-loss-01；不把它当动态 reserve 回执丢失的完整宿主测试。动态回执丢失目前分别由 Rust RPC 反例与 Python 提交后丢结果/重启反例覆盖。取消准入、真实启动编排与此故障在整机组合中的验证仍待补齐。Linux 本轮 wheel SHA256 e65c6be99fe110ed7736084567abbda57df598d1e3be7ef39f65be5ad30290e5，sdist 8d0d3ddbfacf7819ee2911593424ad96a4fbf5c340e669ed6147e77db4e6f0c8；这些制品对应 8b66f91，不自动覆盖后续文档提交。

构建使用原 Router 91b65ee、原 Cargo.lock SHA256 8675caa4ef6227d0f2cb273ac921e9dd0a6989cdad304b5600887a9795355156、私有 Rust 1.99.0 / OpenSSL SDK；没有改依赖掩盖问题。工具和原生测试进入既有 CPU/helper CI，不将手动固定 HUST 方法执行冒称为 GitHub CI。

## 自审修正与限制

- 普通额度/旧决策拒绝此前会把接收 owner 一并置故障；修为提交前的 ReservationRejected 回滚内存候选并保留接收能力，磁盘/生命周期错误仍 fail closed。新增拒绝后继续接收原请求 SEND 的反例。
- 新旧单调时钟不得直接比大小；当前计数的观测时间不能被旧事件倒写。旧 reserved 不能经 apply、notify 或 batch 再取发送权，但历史观察仍可接收；跨代次时钟不复用。
- 取消准入 future 不等于撤销可能已经提交的记录。RPC 工作保留八个槽位上限，绝不在任务中进行后端 HTTP send；未确认记录保留负债。测试等待 job 实际退出，避免把 mock peer 退出误当客户端 job 已退出。
- 初次 lint 的格式/长行问题已修；固定源码审计和初轮单测通过，不包含 NPU/性能结果。旧索引无修改，本轮未重复其 58 项成绩。
- 自有 header 新补丁首次干净归档 apply --check 因新增 hunk 缺前后上下文而拒绝，未执行修改；补全上下文后检查/应用通过，并清除旧 patch 中混入的 Git 换行提示。完整宿主源码输入与仓库 helper/example 逐字节一致，HUST 固定工作树保持干净。最初 admission 示例的弃用 API 警告已改为有界 CAS；固定上游库原有警告不擅自修改。
- GitHub 代理链查询出现 EOF；仅在当前检查进程取消代理变量后成功核验 CI，没有修改系统代理或增加令牌权限。Windows 干净 wheel 安装后的 pip 版本检查遭遇网络 EOF，但 wheel 安装和隔离导入断言成功，未以此升级环境。

## 未完成与下一步

真实 Router 自有 identity → HUST frontend → core 的可靠跨进程绑定/终态 observer 尚未安装，业务结果不能由通用 ENDED 猜测。启动排他、真实服务实例/健康观测采集和原生 Router 整进程 takeover 仍须联通；目前是账本级显式 CPU 交接。新 RPC 的固定 fixture 身份/时钟/版本不是生产身份服务，group commit/热路径开销没有性能验收，长期 GC 仍未实现。

先收敛上述真实适配/恢复编排，再接已有 publisher 的真实 KV 订阅、共同水位与目录新鲜度。到真实单后端阶段需 1 张 NPU 和公共 Qwen2.5-14B-Instruct，届时先反馈；双 TP=1 后续 2 张。MOD 保持 import_only，生产接口/交付方式不冻结，不自动合并或把自审当另一成员独立评审。
