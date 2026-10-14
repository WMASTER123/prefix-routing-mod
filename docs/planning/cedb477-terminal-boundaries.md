# 新版请求关联、终态接线与现场验收

更新：2026-10-14。目标仍为 HUST Core ced6857 / Ascend b47795c。当前完成固定源码方法与既有账本的 CPU 组合；引擎观测适配尚未安装。结果见[本轮证据](../../evidence/cedb477-terminal-boundaries-2026-10-14.md)。

## 当前确定的接缝

| 边界 | 新版源码事实及本轮处理 | 现场必须验证 |
|---|---|---|
| Router attempt → HTTP | BaseServing 移到 `entrypoints/serve/engine/serving.py`；原生 `_base_request_id` 优先读取 X-Request-Id。继续复用 Router 自有载体 | 实际 Starlette headers、重复客户端 header、body ID、流式/非流式；不解析响应 ID 后缀猜对应 |
| HTTP → 完整 family | renderer 返回全部输入后、生成器被消费前取得组数；生成参数和实际 child 数必须对应。复用 EntryCapture 的 begin/bind/submitted/abandon | 实际 tokenizer/renderer、多 prompt × n、n 的默认值、失败和取消；beam/流式输入/多模态不由文本采样结果外推 |
| family → 实际 core child | AsyncLLM 在首次 IPC await 前调用 OutputProcessor.add_request；此时已有 external/internal ID。新版额外执行后端准入检查 | 新版最终安装 callable、调用顺序、全部提交后才 seal；第二组准入拒绝不能结清第一组之外的未知工作 |
| core → 正终态证据 | `_free_request` 成功返回后才记录；新版返回 KV/EC 两类参数，finish_requests 返回 Request 对象。复用原清理方法，观察不改变原返回/异常 | 真实正常完成、stop string、取消；原清理失败不产生 ENDED；无设备同步或调度/分配/淘汰修改 |
| 证据 → M3 owner | 保留关联，通用 Ended 与业务结果分别输入；仍用同一账本分配生命周期序号，提交后 ACK | 真实进程角色、服务/observer 身份、有界投递及重放、ACK 丢失、重启恢复；旧事件不得授权新派发 |

这些是安装候选与验收边界，不是已冻结生产 ABI。当前 `tools` 包装只作用于提取源码构造的独立对象，未修改或导入 HUST 引擎。runtime profile 的 terminal_observer_available 继续为 false；MOD 保持 import_only。

原生 `include_finished_set` 默认关闭，启用与内部 DP balancing 有关；普通 abort 仍只发消息且 core 不返回逐请求清理确认。现有 finished_requests 字段可以继续评估复用，但它自身不提供有界重放/跨重启对账。不能为了获取确认改变 DP 部署，也不能只根据字段存在就宣布终态接通。

## 当前可重复 CPU 门槛

从仓库根目录运行；不启动模型或导入 HUST：

```bash
python -m tools.fetch_hust_audit_sources --execute --output artifacts/hust-source-gate/source
python -m tools.audit_backend_terminal --profile hust-cedb477-native-v1 --source-directory artifacts/hust-source-gate/source --output artifacts/hust-source-gate/terminal.json
python -m tools.audit_http_entry --profile hust-cedb477-native-v1 --source-directory artifacts/hust-source-gate/source --entry-directory artifacts/hust-source-gate/source --output artifacts/hust-source-gate/entry-ledger.json
```

下载固定 HUST 提交的十个文件，每文件最多 1 MiB、三次有限网络尝试，20 秒 socket timeout；hash/大小不符立即失败。输出目录必须全新且位于本项目 artifacts 下。失败保存 fetch.json，不能覆盖旧目录或关闭 TLS 校验。离线时可以显式准备相同十文件，再运行两个审计入口，所有 hash 仍需通过。

Python 3.11/3.12 独立 CI job 执行下载、方法审计及三个反例/组合测试。通常 CPU 测试中的三个源码执行项未设置 HUST_AUDIT_SOURCE_DIR 时明确 skip；专用 job 设置该目录，缺文件或错误 hash 会失败，不转为 skip。

入口和核心清理通过 CPU 调用连接同一 SQLite 实验账本：完整 family 结束后从 1 结清至 0；提前结束、第二组提交失败/取消/后端准入拒绝、核心清理失败维持 1；提交后丢 ACK 再恢复不重复转换。业务结果、健康、时钟、IPC 与 detokenizer 都是明确替身，不能把这些结果写成网络 HTTP、真实取消、NPU 或吞吐验收。SQLite 仍仅是原语义验证存储。

## 停服报错与环境判断

原实机日志顺序为 EngineCore 排空完成、MPClient 资源清理完成，随后 AsyncLLM output_handler 报 EngineDeadError。固定 core_client._ensure_output_queue_task 在捕获 CancelledError 后向输出队列写入 EngineDeadError；本轮直接执行完整固定方法复现此路径。它解释了一条与日志吻合的来源；完整 shutdown 竞态、其他错误来源与资源释放仍须现场确认。

当前证据支持保留已经通过依赖和 Qwen 请求验证的隔离环境。下一轮先在同组合下重现正常停服、带在途请求排空和超时取消，记录原始时序、退出码及残留。没有必要仅因这一日志重新组合全套依赖；若确需后端修复，先列出 HUST 固定提交的改动与配套影响，再单独验证，不在 MOD 内吞异常掩盖故障。

## 近期顺序与用卡

1. 当前服务器 32099 端口拒绝 SSH；等待用户提供可用分配，再核验设备/目录/环境。继续只操作本项目路径。
2. 先用一张 910B2、公共 Qwen2.5-14B、FP16/eager/TP1，核验 Ascend 加载后的实际方法，收敛可选观测的安装/卸载点与单后端跨进程链路。保留关闭、版本拒绝、有限预算和未决请求；大幅 fork 或新增核心 IPC 字段仍需讨论。
3. 再接真实 KV 事件与原索引：受控空起点的连续覆盖先验证；断连/缺口立即撤销可信性。已热接入和超窗恢复仍需新版共同水位 publisher，不能降低正确性门槛。
4. 受控完整链路成立后，用两张卡做两个 TP1 副本的直连、原 Router 基础策略、加观测/目录的开销及强基线比较。尽早获得性能数据，不等待全部长期交付项完成才第一次测性能。
5. 根据瓶颈决定生产账本、GC、接口冻结及 MOD 启停；持续稳定性和恢复门槛在交付前完成。M8 与复杂收益预测继续后置。

现有两张卡的数量足够上述阶段；目前需要的是有效 SSH 分配。无需为本轮 CPU 审计额外占卡，不以保持忙碌替代实验。

## 固定来源

- [Core 请求提交](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/v1/engine/async_llm.py)
- [Core 清理与 finished set](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/v1/core/sched/scheduler.py)
- [前端输出与映射删除](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/v1/engine/output_processor.py)
- [原生 IPC 与取消路径](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/v1/engine/core_client.py)
- [Ascend 安装过程中的 Core patch](https://github.com/vLLM-HUST/vllm-ascend-hust/blob/b47795c63a8e63e7aed4d311f951428d94e045ab/vllm_ascend/patch/platform/patch_engine_core.py)
