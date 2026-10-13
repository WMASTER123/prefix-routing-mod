# 完整入口关联、动态准入与启动交接：CPU 证据

日期：2026-10-13。基于 054ae0a，分支 codex/entry-admission-recovery。只在本项目 CPU 服务器和 Windows 工作区操作；不改 HUST、驱动、全局环境或 MOD 状态。

## 实现范围

- 新增 EntryCapture，复用 TerminalJournal 的全部 child 额度和 seal；从实际 group/internal ID 关联，不解析后缀。固定 13f11da 的十个文件 hash 守卫后，执行 serving/render wrapper、generate、普通 add_request、ID 分配和 frontend 记录删除方法。五种 completion/chat、stream/n 组合以及第 4 个 child 提交失败/取消，共 12 检查在 Windows、aarch64 通过。renderer、参数校验、collector、IPC、核心清理仍为替身，没有真实网络 HUST 或 NPU 证明；chat 只支持该固定入口的一个 rendered input。
- 原生 Router 同一接缝增加 reserve_async（默认适配原 reserve）和可选关联 header。实际 send 前仍检查 registry/control fence。动态 CPU 接收器从空账本按请求提交预留，重试/重复申请不发新许可；旧传输通道复用，不新造代理/索引/生产账本服务。
- 参考 Attempt 保留原时钟，当前 Worker 观测与它分开。旧传输按原时钟验证；可靠终态的新接收确认可用当前时钟，不跨时钟比较时间数值。显式持久化 runtime 交接只接受同一服务实例、完整新鲜观测、进位控制版本、从未复用的时钟及保留的在途计数；重开日志自动暂停。旧 dispatcher 已停是调用方的明确前置条件，不是工具自动探测/多 Router HA。

## 验证记录

初轮：Windows CPU 434 + 4 skips；Linux CPU 438；Linux integration 3；原生 helper 21。后续审查增加提交前/后独立进程退出和正常拒绝反例，最终成绩以下方追加为准。完整宿主隔离 release 构建/动态 HTTP/旧在线/关闭/stream 仍在本轮复验中，不能继承前一分支成绩。

构建使用原 Router 91b65ee、原 Cargo.lock SHA256 8675caa4ef6227d0f2cb273ac921e9dd0a6989cdad304b5600887a9795355156、私有 Rust 1.99.0 / OpenSSL SDK；没有改依赖掩盖问题。工具和原生测试进入既有 CPU/helper CI，不将手动固定 HUST 方法执行冒称为 GitHub CI。

## 自审修正与限制

- 普通额度/旧决策拒绝此前会把接收 owner 一并置故障；修为提交前的 ReservationRejected 回滚内存候选并保留接收能力，磁盘/生命周期错误仍 fail closed。新增拒绝后继续接收原请求 SEND 的反例。
- 新旧单调时钟不得直接比大小；当前计数的观测时间不能被旧事件倒写。旧 reserved 不能经 apply、notify 或 batch 再取发送权，但历史观察仍可接收；跨代次时钟不复用。
- 取消准入 future 不等于撤销可能已经提交的记录。RPC 工作保留八个槽位上限，绝不在任务中进行后端 HTTP send；未确认记录保留负债。测试等待 job 实际退出，避免把 mock peer 退出误当客户端 job 已退出。
- 初次 lint 的格式/长行问题已修；固定源码审计和初轮单测通过，不包含 NPU/性能结果。旧索引无修改，本轮未重复其 58 项成绩。

## 未完成与下一步

真实 Router 自有 identity → HUST frontend → core 的可靠跨进程绑定/终态 observer 尚未安装，业务结果不能由通用 ENDED 猜测。启动排他、真实服务实例/健康观测采集和原生 Router 整进程 takeover 仍须联通；目前是账本级显式 CPU 交接。新 RPC 的固定 fixture 身份/时钟/版本不是生产身份服务，group commit/热路径开销没有性能验收，长期 GC 仍未实现。

先收敛上述真实适配/恢复编排，再接已有 publisher 的真实 KV 订阅、共同水位与目录新鲜度。到真实单后端阶段需 1 张 NPU 和公共 Qwen2.5-14B-Instruct，届时先反馈；双 TP=1 后续 2 张。MOD 保持 import_only，生产接口/交付方式不冻结，不自动合并或把自审当另一成员独立评审。
