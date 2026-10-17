# 固定 HUST 请求终态来源审计与接入门槛

2026-10-12 可执行增量：`tools/audit_backend_terminal.py` 先验证四个源文件完整 SHA256，再执行抽取的完整方法（合成 CPU 协作者）。现有方法与未安装 observer 的独立对象接线共 25 项检查通过；这不是启动完整引擎。用户已批准小范围 CPU 原型；实际范围、费用和未过门槛见[终态适配评估](terminal-observer-scope.md)。

补充源码事实：`EngineCore.abort_requests`（core.py:493–499）忽略 `finish_requests` 返回的 ID；管理性暂停使用的 `_send_abort_outputs`（1826 起）另有显式输出，不代表普通取消也有。scheduler.py:2522 起延迟 blocks 释放；`finished_req_ids` 是逻辑结束，不是物理空闲证明。output_processor.py:694 起删除映射，随后同 ID 输出会被忽略；真实关联必须提前保留。完整正常 stop 检测/生成循环、真实 detokenizer、shutdown loop 及设备 fence 仍未执行，CPU 替身不覆盖这些范围。

日期：2026-10-12。范围：只读审查 `vllm-hust-legacy-20260831` 固定提交 `13f11da44455d2f85f00f810a04e1bd2eaf30ba6` 的本地归档源码。工作树无源码修改；未运行引擎或 NPU。本页是后续 M3/M5/M7 接线依据，不是已交付的终态 observer。

## 已确认的来源语义

| 来源与固定源码位置 | 能证明什么 | 当前不能证明什么 |
|---|---|---|
| `entrypoints/openai/completion/serving.py:463–473` | SSE 生成器结束，错误路径也发送 `[DONE]` | 所有内部请求已经从核心退出；不能单独生成 ENDED |
| `v1/engine/async_llm.py:578–598` | 前端已取得 `RequestOutput.finished`；断连时调用 abort | 前端 finished 与核心终止在同一边界 |
| `v1/engine/output_processor.py:634–694` | 前端 Detokenizer 可自行检测 stop string、输出 finish_reason，再把尚未核心结束的请求加入 `reqs_to_abort` | finish_reason 本身保证核心已处理取消 |
| `v1/engine/async_llm.py:688–692,712–721` 与 `core_client.py:1215–1217` | abort 最终发送 ABORT 输入消息 | ZMQ send 返回就是核心处理完成的确认；这里没有逐请求应答 |
| `v1/core/sched/scheduler.py:2428–2505` | 核心 finish 后移出调度队列、记录 finished ID，并执行资源释放逻辑 | 本机 NPU 所有异步操作的完成时点；对应图/流水配置仍需验证 |
| `v1/core/sched/scheduler.py:126–132,2107–2119` 与 `engine/core.py:1041–1044,1058–1064` | finished ID 的导出受 `include_finished_set` 控制；EngineCoreProc 把 `has_coordinator && !data_parallel_external_lb` 作为该参数传入 | 目标无 DP coordinator 的单副本会默认导出 finished IDs；该条件为 False 时不导出该集合 |
| `v1/engine/core_client.py:1567–1571` | DP 多引擎客户端已使用 finished IDs 清理内部在途映射 | 这是本项目 TP=1、DP=1 的现成公开请求对账 API |

上表路径均相对于归档的 `vllm/`。范围暂为 completion 与普通 V1 路径；chat、多候选/多 prompt 的父子请求关联，以及 HUST/Ascend 执行配置不得自动继承本结论。

## 当前接线决定

M5 的真实 Router 回调先规范化为 `send_started`、`not_sent/not_accepted` 或 `outcome_unknown/none`。HTTP headers、EOF、error、drop 不产生 ACCEPTED 或 ENDED。CPU 实验以有界记录验证字段、连续序号、重复投递和账本保留，不能把实验结束后重放称为生产可靠投递。

生产必须同时补齐两个部分：正常完成有可关联的可靠终态；断连/取消后未知工作有可靠对账。仅用保守 unknown 会逐渐占满额度，不能成为最终可用版本。不得通过超时清零解决这个问题。

优先评估原生核心输出中的 finished 信息能否在目标配置完整覆盖正常、stop string、显式 abort 和未接受请求。若缺少覆盖，再评估默认关闭、固定源码守卫的只读终态 observer；评估不修改调度决策、分配/淘汰或插入设备同步。需列明实际接缝、实例/内部 request ID 与 Router attempt 的映射、丢失后恢复方式及成本，超出小型观测范围则先讨论。

## 下一项 CPU 与真机验收

1. 初始化传参已定位为内部 DP balancing 条件；继续用固定源码的最小 CPU fixture 检查普通单副本取消输出、终态覆盖与停机边界，不能把 DP 专用清理接口当目标场景已有支持。
2. 定义同一次尝试的可靠投递/幂等重放、满额拒绝与故障暂停；发送前应能确认预记账事件已被接受。CPU transcript 目前没有该保证。
3. 验证外部 request ID、多个内部请求、服务实例代次和 publisher 代次的分别关联；同 URL 重启不能继承旧确认。
4. 需要 **1 张 NPU** 后再用公共 Qwen2.5-14B-Instruct 验证自然完成、长度停止、stop string、断连/取消、超时、重启及目标执行配置。CPU 源码检查不能替代这些测试。

长期留存、Router 重启恢复、配置切换/排空及多分区预算仍分别验收；本轮不冻结生产 ABI 或启用 MOD。
