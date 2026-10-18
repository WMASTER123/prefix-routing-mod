# 恢复、退役与累计资源资格（2026-10-18）

本记录只覆盖固定 HUST/Ascend/Router 基线上的首版 `recovery-retirement-v1` 正确性资格，不是容量或长期稳定性成绩。

## 固定范围

- HUST Core：`ced6857afa0ea7b2e3f0846a62e1394e90f15607`（`vllm 0.30.0+empty`）
- HUST Ascend：`b47795c63a8e63e7aed4d311f951428d94e045ab`
- Router 宿主：`91b65ee4bdfbe98863a32a2205dced5dbb249ffe` 的隔离复制
- 设备：两张 910B2；Qwen2.5-14B-Instruct，FP16/eager，两个 TP=1 副本
- 部署：回环 HUST 后端、显式 `exclusive_router_v1`、持久 M3、原生 KV IPC；未改 HUST 调度、KV 分配/淘汰或设备执行

## 真正 retire 响应丢失与累计历史

`r14-retire-churn` 在每个后端前放置仅回环可见的故障代理。代理先让真实 `/prefix-routing/terminal/retire` 成功提交，再关闭首次响应连接；Router 只因传输/读取失败在同一持久水位重试。

- 两个后端各丢失一次已提交响应；Router 观测并恢复 2 次，重复 retire 均返回 `retired=0`，没有重复释放。
- 基础请求/取消与 520 个 churn family 共退役 524 family；churn 使用 `n=2`，累计 1040 child。
- 9 个采样点中，Frontend `family_count/reserved_children/frontend_event_count/child_status_count` 与 Core `retained_records/duplicate_guards` 均为 0。
- Router 墓碑始终不超过 16；SQLite 文件族从首个采样到最终采样均为 321104 bytes。
- Router RSS 92860→94324 KiB。副本 0 Frontend 2120708→2121360 KiB、Core 2552320→2553116 KiB；副本 1 Frontend 2122240→2122640 KiB、Core 2554276→2555756 KiB。RSS 有分配器与采样噪声，只证明本次有界运行没有随 family 数线性留存，不能外推多日稳定性。
- 压力提示形成强热点，520 个 churn family 基本落到副本 0；两张卡在基础 A/A/B 链均实际加载和执行。该结果是单热点最坏方向的历史边界，不是均匀双副本吞吐测试。
- 两个服务自然退出，无强制进程组清理和 NPU 残留。

原始报告 SHA256：`454afccbf0edf7fdc21a89e8138358937dfd1cfa156cb6859d92f76f3f2600d4`；实验二进制 SHA256：`241c3fc7408e39386a0012c583462dee2cebaa672e1fe63f85d3ef6cc6ed6019`。

第一次长链 `r13-retire-churn` 使用 `n=2, temperature=0`，被 HUST 正确拒绝为 HTTP 400；改为允许多采样的 `temperature=0.7` 后才得到上述成绩。失败未计入通过结果。

## Router 强退、暂停恢复与实例替换

最终资格使用 `r21-router-recovery-final`。它保持两套 HUST 服务运行，Router 在一个真实请求完成 HTTP 响应、M3 已持久记录为 sent、尚未消费终态时使用专用退出码 86 直接结束。新 Router 随后执行：

1. 从同一 SQLite owner/generation 和两个零水位来源绑定恢复，旧 active 转为 uncertain，准入保持暂停；暂停期真实 HTTP 请求被拒绝且不增加负债。
2. 核对相同 worker/instance/observer/ownership，确认旧派发进程已停止后 CAS takeover。审查确认 HUST 原生 replay END 不携带权威当前序号/快照水位，因此重启后不安装任何暖 M2 回放，目录明确为 `unknown_until_fresh_source`；基础策略仍可服务，不能产生伪命中。
3. 消费原请求终态、原子提交 M3 与 cursor、ACK、同水位 retire；水位以下旧 cursor 返回 503（恢复窗口不可用）。
4. 恢复后再发送并完成一个请求；最终退役 2 family，持久终态水位为 `[2, 0]`。
5. 正常停止原副本 0，使用同一地址和 worker_id 启动新的 HUST 进程。实际 instance epoch 从 `8f668edf...` 变为 `526e695c...`；旧持久绑定被拒绝，未开放准入。

三个后端进程（原两个加替换实例）均自然退出，无强制清理和 NPU 残留。报告顶层 `durable_recovery_verified=true`，链内 `durable_recovery_verified=true`、`actual_source_replacement_verified=true`、`m2_recovery_mode=unknown_until_fresh_source`，汇总与原始日志一致。

原始 SHA256：

- `report.json`：`1170cca850dba246c17074a18ae6f3b36a41cf81d1c648a1b69102c1a9094df3`
- `router-crash.log`：`8b3d8bd0a9ed6ef5a0f294b5aa3dbe8441b78aecdfa7db106bc1f533273b91db`
- `router-recovery.log`：`98441ce14183f35acbf6c3043db1323026f78cb09a9da73663f7a8c3d00377c8`
- `router-replaced-source.log`：`64266ee0428702b2671a159d6dc251b07b08a0253b716b998cd129ca280ad35d`
- 实验二进制：`7e71973266df5c4aa3a37c4674af647b5a0c46383fe1eddca5d853bf8a1c8ad6`

`r15` 暴露 M2 只允许冷起点；当时增加了同实例零序号回放。后续审查发现原生 END 没有权威当前边界，空回放无法证明目录完整，因此删除该恢复方法和对应“成功”测试，改为重启后 M2 unknown。`r16` 暴露旧 cursor 实际语义为 503 而非测试假定的 409。`r17` 的恢复链已通过，但替换实例工具漏建父目录且清理掩盖原错。`r18` 验证旧实现的完整自动化；`r19` 验证安全降级，但远端脚本未同步导致报告顶层仍为旧默认 false；`r20` 修复了汇总并增加 M2 模式硬校验。最终审查又增加“新账本来源 cursor 必须为零”的构造守卫和反例，重新构建后由 `r21` 验证与待提交源码一致的最终二进制。失败/中间试件均清理自有进程，未冒充最终通过。

## 自动回归与审查

- Python：Ruff/format 通过；非 integration 588 passed、7 skipped、3 deselected；integration 3 passed。响应丢失代理在 Windows 连续重复 8 次通过。
- 固定 aarch64 宿主：最终 example 38 tests passed（删除 1 项无法证明安全的暖 M2 恢复测试）；独立 checks 58 tests passed。
- 固定宿主全库直接 `clippy -D warnings` 被四个原 Router 既存 lint 阻断；记录并仅允许这四项及 example 组合产生的既存 dead-code/type-complexity 后，本轮代码仍以 `-D warnings` 通过。没有为消除上游 lint 修改原 Router 业务代码。

## 结论与限制

正确性门槛已满足，因此只冻结首版恢复与回收合同 `recovery-retirement-v1`。冻结内容见[持久恢复计划](../docs/planning/durable-runtime-and-retention.md)；整个 MOD 的公开配置、Manager 生命周期、跨版本 ABI 和性能策略仍未冻结。

本次不证明：任意已热或超出原生 replay 窗口的接管、共享后端旁路业务、图模式/TP>1、跨节点控制、容量提升、持续多日运行。M2 恢复失败可以降级为 unknown，但 M3 身份/持久性失败必须保持暂停。
