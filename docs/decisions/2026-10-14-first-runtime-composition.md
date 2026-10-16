# ADR：首版请求链采用固定 Router 内本地组合端口

日期：2026-10-14，2026-10-16更新。状态：候选接口、同进程M3 owner与原M2索引适配已通过固定宿主CPU组合；真实M7/M1/终态同时装配和生产ABI尚未冻结。

## 决定

首版继续使用 `vllm-project/router@91b65ee` 作为外部HTTP宿主，不在 Python 中增加第二层推理代理。推理、原生KV事件、终态和版本判断只针对 `vLLM-HUST/vllm-hust@ced6857` 与 `vllm-ascend-hust@b47795c`；包名或官方文档不能替代这两份固定源码与实机证据。

`runtime_provider.rs` 在同一个Router进程中组合三个本地端口：M1 `FeaturePort`、M2/M3原子只读 `SnapshotPort`、M3异步 `AdmissionPort`。宿主原有registry revision、worker可用性复核、HTTP发送与SSE生命周期仍由既有窄幅patch负责。M4第一版只实现可解释的基础决策：过滤不健康/排空实例；存在正的可信缓存命中时选择最大命中，命中相同选择最低本地在途；没有正命中时，可信miss和unknown均进入最低在途/稳定URL回退。unknown不改写成0，也不因为“更确定”获得虚假优先级。

候选数组顺序不是身份契约。M2/M3必须以规范化worker URL、instance epoch及版本围栏关联状态；不得用启动参数位置、注册次序或map遍历顺序解释目录归属。固定宿主探针首次运行已用反转选点暴露该错误，改为显式身份关联后才通过。

M3必须分配不可复用的attempt ID，并在异步reserve中原子复核request、实例、worker/config版本和容量。同步reserve明确返回 `async_admission_required`，避免把磁盘或IPC阻塞塞进Router同步回调。M1/M2查询仍须本地、有界，不在请求热路径等待目录恢复。

M2使用一个进程内 `Pool`，不复制第二棵KV树。`InProcessCache`只把稳定worker URL、HUST instance epoch、publisher/KV identity与原索引Binding关联；lookup只持有有界本地锁，不做网络、磁盘或设备同步。断连立即撤销信任；同URL新实例通过精确旧Binding替换并重新完成snapshot/control确认；过期身份不能替换或注销当前实例。Pool总逻辑配额随精确注销回收，但这不是RSS硬上限，仍须用实测校准。

## 历史回收边界

已确认的传输outbox槽位可在完整ACK后回收，容量表示未确认工作而不是进程生命周期累计请求数。终态journal新增owner分配的attempt/child身份和显式retire：只有终态已ACK、消费者游标已持久化到同一边界且身份由owner保证不复用时才删除family/child；外部任意ID保持原有限拒绝行为。重复或超窗旧证据只能返回duplicate/unknown或触发保守失效，不能再次释放额度。

这只是回收协议原语。消费者游标的实际持久化、M3生产存储/批提交、Router重启后未决负债恢复仍未选定，因此不能把当前SQLite工具账本、stdio索引或CPU TCP接收器冻结为生产实现。

## 放行条件

下一提交须把已验真实HUST subscriber接到该同进程M2，再把真实Qwen tokenizer/template和HUST终态装入同一provider，通过公开completion入口的正常、取消、断连、目录失效、配置竞争和重启用例；chat/template可在明确拒绝的前提下后续启用，不得静默伪装支持。随后测量一次分词、快照读取、reserve/checkpoint提交和host竞争；接口及错误语义在这些结果通过后冻结。
