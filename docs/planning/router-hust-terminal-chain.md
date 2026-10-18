# Router 准入、转发与真实终态组合

日期：2026-10-14。前置为[上一阶段单后端终态](cedb477-live-terminal.md)。当前先验证下列有限链路，再接在线 KV 与双副本强基线；结果以 evidence 单独记录，不把计划当通过。

实机结果：cc2478b已完成实际Router库→真实HUST→原账本的普通n2与流式取消，2个family/3个child、确认前额度拒绝/确认后准入、丢许可无额外受管family/重开保债通过。详见[本轮证据](../../evidence/router-hust-terminal-chain-2026-10-14.md)。下列范围不等于所有生产组件完成。

## 复用与实际边界

固定 vLLM Router 91b65ee 的原库及已审查三文件补丁不变；从已校验归档重放、与前一宿主逐字节核对。新的 `live_terminal_chain.rs` 是调用原 typed HTTP 路径的有限 example：没有第二个代理，不复制原转发代码。它不是已经产品化的 Router 外部 HTTP 入口或 MOD 二进制启动器。

复用 AdmissionClient、LifecycleTrace、Outbox、Controller、AdmissionReceiver、DurableLedger，以及上一阶段 HUST endpoint/utility。AdmissionReceiver 仅增加显式 worker/domain 与准入前检查端口；不传参数时原 CPU fixture 的默认行为不变。新 TerminalSource 验证完整批次与完整 family，通用 Ended 和实际 Router 提供的业务结果分别交给同一 owner。

本轮不改 HUST 引擎文件或观测包装、不新建索引/生产账本服务、不复制 GPU 成本参数。不把逐请求 TCP/SQLite FULL 事务的实验 RPC 选为生产方案，也不宣称其开销可忽略。

## 要验证的顺序

1. 受控服务启动并取得真实 core/frontend PID、worker/instance/observer 身份。增强准入前重新检查同一控制绑定及实际 `/health`，失效/超时拒绝新请求；不能使用 fixture-target。
2. 原 Router 自有 attempt header 覆盖两个不可信客户端同名 header。原 M3 成功提交预留后才准许转发。普通 completion n=2 和流式 n=1 走实际 HTTP。
3. 业务请求返回/主动丢弃流后，故意暂不消费核心证据；额度为1，下一请求必须由原账本拒绝。完整终态与业务结果入账后，下一次才成功。
4. 流式至少消费一个非空生成输出再 drop，实际核心须报告 ABORTED；HTTP drop 本身不清账。终态重复投递不能重复释放，迟到传输关闭只持久化原观察。
5. 明确的 supervisor 屏障后，在真实准入提交后丢 ACK；Router 不得取得许可或产生新的受管后端 family，再次准入拒绝。重开账本仍保留1笔未决且暂停。未证明任意进程崩溃自动恢复。

后台先检查完整序号、来源、覆盖与所有终态 family（包括批尾），然后才更新账本。不能先结清合法前半批再发现后半批损坏。普通 source fail 不代表后端终止；保留所有未决工作。

## 时钟、故障和资源限制

Rust Router 的 admission 与传输观察使用同一显式源时钟 ID，源序号与 M3 转换序号分开。终态入账使用该源已知的最新逻辑位置，不能解释为终态的物理接收时刻或延迟；不将后端或 Python monotonic 数值放进该域。实验总期限120秒，小于账本固定健康时效；实际健康/控制读取另有1/2秒调用期限，准入RPC仍有原两秒总期限，超时不能授权发送。

源 journal 和本地 outbox 都是有限留存，无 GC；总输出最多1MiB，帧/HTTP回复有原有界限，Router stdout 在落地前统一移除两类凭据。case 只保留关联/状态，不保存请求原文到 Git。控制凭据只留环境，后端控制凭据不交给 Router。

默认关闭、固定版本、loopback/TP1/DP1/eager范围和 MOD import_only 保持。关闭用原工具管理的进程组，不强杀其他服务；真实取消/新旧 wrapper/原版关闭回归分别记。源故障、坏批、迟到关闭等先有CPU反例；不把CPU故障注入说成NPU网络故障已测。

## 运行和 CI

已核验的服务环境中，给既有 `tools.hust_native_probe` 增加 `--router-binary <.../live_terminal_chain>`，并显式启用 `--terminal-observer`；它替换直接请求端终态 case，保留原生请求/APC/KV 采集先验及自有清理。该参数只接受单副本 stage-driver 协议，工具会在加载模型前核验有界二进制中的协议标记；双副本、接受私有 JSON 配置的 `hust_runtime_probe` 必须交给 `tools.hust_runtime_chain`，不能在这个参数复用。binary 须由当前固定宿主/锁和本项目 example 构建并记录 SHA。目标仍是 1 张 910B2 与公共 Qwen2.5-14B-Instruct，不改全局环境或公共模型。

Python3.11/3.12 CPU/integration/wheel、固定HUST源码CI照常执行；共享原生helper CI加本example格式检查。完整Router aarch64 release构建与真实后端链路手动执行并记录，不伪称helper job编译了整个Router。

## 下一阶段与通过条件

实际组合链路通过且独立审查范围清楚后，接 M7 原生 KV 订阅到原 M2 索引。先验证受控空起点/完整连续流、完整身份/父链、消息与总资源预算；断连/缺口/源失效立即撤销精确信任。请求生命周期或请求历史不能替代驻留。已热接入/超窗恢复仍需新版共同边界协议，不直接启用旧 publisher hook；若需扩大修改先讨论。

受控完整路由链成立即用现有2张卡进行两个TP1副本的配对开销与强基线，不等待所有交付项才测性能。先直连/原生基础策略/同底座加接线，之后才加策略；低复用、热点改变和混合长度都纳入。用户已确认暂时没有固定业务SLO，正式比较先做多阈值容量曲线与输入/输出长度分组，具体阈值和分组边界在基础校准后、策略比较前统一冻结，不从最终结果中挑有利数值。生产HTTP宿主装配、自动恢复、长期GC、图模式与完整MOD启停仍须另验，尚不冻结三人生产ABI。

分组使用真实模板后的输入token数及负载预设的输出长度/预算，实际输出长度另报，避免按各策略运行后产生的不同输出重新挑样本。各组报告TTFT与生成停顿的P95/P99、完成/拒绝/超时率；容量曲线同时施加TTFT、停顿与完成率条件，失败请求不从到达总数中剔除。流式chunk间隔若不是逐token测量，必须单独标注，不能冒称精确token间隔。当前仅确认评测口径，尚未开展性能测量。
