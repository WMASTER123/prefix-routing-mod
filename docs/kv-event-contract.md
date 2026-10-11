# HUST KV 事件回归合同 v0

适用源码：core 13f11da44455d2f85f00f810a04e1bd2eaf30ba6。本文供 G2 测试与后续适配判断，不是生产目录协议实现。源码入口为 [kv_events.py](https://github.com/intellistream/vllm-hust-legacy-20260831/blob/13f11da44455d2f85f00f810a04e1bd2eaf30ba6/vllm/distributed/kv_events.py)。

## 已核对的接线与消息

- 同机私有 IPC：PUB bind，SUB connect；重放为后端 ROUTER bind，测试端 REQ connect。所有路径由本次项目运行独占，不复用公共 wildcard。
- 实时消息是三个 frame：topic、8 字节 unsigned big-endian 序号、msgpack batch。batch 是数组封套，事件是 HUST tagged map；实时 frame 本身不带 publisher_epoch。
- BlockStored 包含 hash、parent、token_ids、block_size、group_idx 等；medium=GPU 是历史标签，不代表实际为 NVIDIA。生命周期与可复用时点仍需按 NPU 路径验收。
- 结构化重放请求含 publisher_epoch、start_seq、force_snapshot；响应含 publisher_epoch、next_seq、replayed_batches 和可选 snapshot。next_seq 是排他的后续序号；无快照时必须完整覆盖 [start_seq, next_seq)。
- legacy 8 字节起点请求使用逐帧重放及结束标记；不得将该标记强套结构化响应。当前新增真实回归只验证结构化协议，legacy 保持未测。

## 快照的两个实际边界

快照保留 group 的块哈希集合与 block_size，token_ids 为空、parent 为 null。它可能适合直接按引擎一致哈希查询的实现，却不能自动视为已有 token radix tree 的完整重建输入；group/语义身份与请求哈希生成必须另验，不能直接宣布所有精确路由不可实现。

publish() 在生产者侧先更新镜像、再排入队列；发布线程后续才分配序号。强制快照的镜像状态与 next_seq 不在同一个发布边界。tools/audit_kv_recovery.py 用固定源码真实方法、暂停发布的合成队列验证此点：未发布的存储已经进入快照，随后未发布的移除又从快照消失，而 next_seq 仍为 1。它是确定性 CPU 协议证据，不是实际 NPU 丢包/重启实验。

因此，收到快照和 epoch 不能单独将生产目录标为可信。后续需验证快照/增量的共同边界、迟到帧处理与消费者表示方式；复用底座选型要检查是否具备该保证。当前不修改 HUST publisher，窄幅适配或其他恢复方案在缺口实证与 ADR 后确定。

## 有界真实回归子集

tools/regress_kv_events.py 复用已有启动命令、进程清理、请求与事件摘要。专用测试配置为 TP=1/BF16/eager、1024 context、1 GiB KV、4 批重放窗口；用 16 个不同长前缀产生自然淘汰，再测窗口内重放、溢出快照及 reset 后真实清空事件。此配置用于放大生命周期事件，不替代 8k 功能配置或性能基线。

测试接收器只保存有上限的摘要，拒绝实时序号缺口和不完整重放，丢弃冷启动快照水位前的已缓冲帧；不持有生产驻留树，不输出精确命中。HTTP reset 成功必须继续观察 AllBlocksCleared；本次源码的 API 并不检查内部 reset 布尔结果。

每次报告区分子集 pass 与 complete_recovery_validated=false。本子集没有验证：跨节点 TCP、legacy 重放、断连/静默、同地址重启、旧代次迟到帧、精确身份/完整目录恢复。以上通过前 G2 不关闭，MOD 不启用。

## 失败与后续讨论

存储/自然移除/清空缺失、帧或范围错误均失败，保留原报告；只清理本工具创建的服务组及 socket。报告成功不允许默默清除未恢复目录的 unknown 状态。

下一项方法决策是如何获得一致恢复边界并接入候选 Router 的索引：先复现压力/断连/重启，核对候选公开接口，再比较引擎已有保证、小型适配或受限恢复条件。不能把禁用精确模式写成精确模式已通过，也不在该合同中提前决定重写索引或 fork 引擎。
