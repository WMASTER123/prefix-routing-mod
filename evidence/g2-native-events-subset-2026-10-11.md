# G2 原生 KV 事件子集与恢复边界证据

日期：2026-10-11。真实 NPU 生命周期子集与合成协议审计分别记录，不等于完整 G2、生产目录或性能通过。

## 真实回归

使用 [W1 恢复环境](w1-runtime-restore-2026-10-11.md)、公共 Qwen2.5-14B-Instruct、逻辑 NPU 0，TP=1/BF16/eager。专用配置为 1024 context、1 GiB KV 与 4 批重放窗口；后端实际报告 5,376 tokens / 42 块，不将它混入 8k 功能配置或后续性能基线。16 个不同长前缀的完整缓存工作集共 80 块，大于实际 42 块容量。

| 记录 | 结果与范围 |
|---|---|
| run01 诊断 | 16 BlockStored、40 BlockRemoved，序号 0–15 连续；窗口内重放和溢出快照有响应。reset 返回 404，整次报告 fail；未改环境，保留原始报告 |
| run02 / 7364def340304d273f9ab596d598ff85c1d4a0ba | 子集 pass：17 BlockStored、40 BlockRemoved、1 AllBlocksCleared；序号 0–16 连续，reset HTTP 200 后观察到真实清空；同一临时服务正常关闭 |

404 原因为固定 HUST 在 VLLM_SERVER_DEV_MODE 开启时才注册开发 cache router；并非依赖或 NPU 失败。只在本次 loopback 回归进程设为 1；通用 smoke、环境恢复入口和生产配置未因此变更。API 本身不检查内部 reset 布尔值，必须以真实清空事件补验。

run02 窗口内重放返回 [15,16) 的一批事件，其脱敏摘要与实时流中序号 15 一致；本次不是多区间、乱序或重复消费验收。超窗请求从序号 0 开始，返回 next_seq=16 的快照，含清空标记及 40 个现存块哈希，token_count=0、parent=null；只证明响应存在，不证明快照与未来增量边界一致或已恢复精确索引。

两次服务均 parent exit 0、forced_group_cleanup=false；原始日志、命令与摘要保留于忽略的 artifacts/g2-events-20261011。没有保存原始请求/生成文本或将测试集合用作生产驻留目录。

## 确定性恢复边界审计

使用安装的固定 HUST core 13f11da44455d2f85f00f810a04e1bd2eaf30ba6 真实 publish / _build_replay_response 方法，构造合成事件与暂停发布的队列，无 NPU 计算、socket 或后台循环。最新官方工具副本重新执行，源码 commit、清洁状态及实际导入位置均检查。

观察：一个存储事件仍待发布时，snapshot 已含 1 个哈希，next_seq 保持 1。再排入移除后，snapshot 哈希数变为 0、队列为 2，next_seq 仍为 1。镜像修改先于序号分配的风险已可重复复现，不依赖调度时序碰运气。

这证明现有快照不能直接充当“与发布水位一致的完整状态”。它不意味着哈希快照一定无法用于精确匹配：直接按兼容的请求哈希查询或已有 token 目录可能另有方案，但必须验证身份、表示方式和共同边界。详见 [协议合同](../docs/kv-event-contract.md)。本轮未修改引擎 publisher 或引入 observer。

## 工程检查与审查

7364def：Windows CPU 108 passed、2 个权限相关 symlink 用例 skipped；Linux aarch64 CPU 110 passed；mock 三项通过。两端 sdist/wheel 与源码目录外干净 wheel 验证通过。[GitHub Actions](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38128562322) completed/success。

自检覆盖序号/epoch/完整区间拒绝、旧缓冲帧处理、摘要脱敏和上限、消息大小、socket 清理、仅自有进程、测试开发接口与生产配置隔离。首次 404 由源码定位后修正、保留失败并复测；没有忽略失败或调整验收线。[PR #6](https://github.com/WMASTER123/prefix-routing-mod/pull/6) 为独立 draft，叠在环境恢复 PR #5 上，待人工审查、未合并。

## 当前门槛与下一步

完整 G2 未通过，complete_recovery_validated / kv_events_validated / performance_validated 保持 false。剩余：多批缺口/去重、断连与静默、同地址重启/epoch、迟到旧事件、精确 hash/group 身份、冷接入与快照增量一致恢复、legacy/cross-node 传输按声明范围验证。

先补断连/重启及一致恢复方案的实证，再形成最小适配 ADR。快照风险解决或受限条件严格成立前不允许恢复精确目录可信性；不提前接 Router，不把基础降级当精确模式通过。G3 图与并发取消继续后置。
