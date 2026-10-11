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

因此，原生模式收到快照和 epoch 不能单独将生产目录标为可信。用户已批准实验性窄幅 publisher hook，见下节与 [ADR](decisions/2026-10-11-optional-hust-publisher.md)。引擎文件保留不动，安装包默认不启用适配；原生三帧/仅哈希快照的限制仍存在，不能将适配后的结果转记到原生模式。

## 可选实验协议 v1（与原生模式分开）

`PREFIX_ROUTING_HUST_PUBLISHER=1` 且 `VLLM_PLUGINS` 显式包含 `prefix_routing_publisher` 才启用；版本、文件 SHA-256 和重复绑定守卫在启用时检查。此内部工厂适配尚未获准作为生产交付，MOD 仍 import_only。

- producer 只入队，publisher owner 按编码→镜像/元数据→重放→next_seq 的顺序提交，然后发送；快照与排他水位同属一个 owner，不包含仍在队列中的变化。
- 实时四帧为 topic、8 字节序号、原生 msgpack batch、ASCII epoch；结构化回复另含 `schema_version=1`、`valid` 和 `reason`。valid=false、epoch 变化或连续性缺失必须撤销精确资格，不以普通存储事件恢复旧目录。
- 快照包含 token 和父链。已淘汰但被驻留子块依赖的祖先先作为拓扑恢复，随后由 BlockRemoved 撤销驻留；消费者必须整批暂存后安装，不能在中间步骤开放命中。
- 元数据数/token 数、单 payload、回复和重放总 payload 分别有界；限制不等于 RSS 已验证。上限或身份错误时反馈不可恢复；真实清空后才可建立新的已知起点。
- epoch 过滤不能替代传输恢复。已知重启或采用新代次时丢弃旧目录，关闭旧 SUB、建立新连接并重新握手；PUB 没有订阅就绪确认，冷接入/丢帧必须通过快照和完整重放补齐。

实验验证范围为 TP=1、DP rank 0、Qwen2.5-14B 纯文本/group 0/完整块/IPC/eager。构造器拒绝非零 DP rank，元数据拒绝 LoRA/extra keys/部分块；TP=1 是受控启动配置和本轮验证范围，不宣称 hook 已对所有 TP 配置实现独立预检。目标图、压力、Host 开销、跨节点和生产索引原子安装仍须独立验收。

本地匹配键按候选 token 索引的 u32 little-endian/SHA-256 计算；引擎整数 hash 是不透明 parent/removal 锚点。本路线不要求改默认 xxhash 或统一首块 seed，不能将“局部 token 对应正确”写成完整原生 bytes 哈希链重算通过。

## 有界真实回归子集

tools/regress_kv_events.py 复用已有启动命令、进程清理、请求与事件摘要。专用测试配置为 TP=1/BF16/eager、1024 context、1 GiB KV、4 批重放窗口；用 16 个不同长前缀产生自然淘汰，再测窗口内重放、溢出快照及 reset 后真实清空事件。此配置用于放大生命周期事件，不替代 8k 功能配置或性能基线。

测试接收器只保存有上限的摘要，拒绝实时序号缺口和不完整重放，丢弃冷启动快照水位前的已缓冲帧；不持有生产驻留树，不输出精确命中。HTTP reset 成功必须继续观察 AllBlocksCleared；本次源码的 API 并不检查内部 reset 布尔结果。

该 reset 路由受 VLLM_SERVER_DEV_MODE 控制。首轮未开该开关时返回 404，保留为失败记录；回归专用服务在进程环境中设为 1，监听保持 loopback。通用 smoke、环境恢复入口与未来生产服务不因此开放开发接口。

每次报告区分子集 pass 与 complete_recovery_validated=false。本子集没有验证：跨节点 TCP、legacy 重放、断连/静默、同地址重启、旧代次迟到帧、精确身份/完整目录恢复。以上通过前 G2 不关闭，MOD 不启用。

## 失败与后续讨论

新增恢复回归以[ADR](decisions/2026-10-11-g2-recovery-regression.md)为准。tools/kv_recovery_cursor.py 是测试流的连续性游标，tools/regress_kv_recovery.py 复用启动和接线验证多批重放、断连及同地址重启。它们不持有生产 KV 驻留树。

受控新服务必须在任何模型请求前确认序号 0 和空缓存，才能建立本次测试的已知起点。窗口内完整重放可恢复该起点的事件覆盖；超窗快照不能恢复共同水位保证。控制面确认有失效期限，正常空闲无事件不是失败，实时帧到达也不独自续期代次确认。

实时 frame 缺少 epoch：连接 generation 只是本地 socket 来源，发现新 epoch 后关闭旧 SUB 并创建新 SUB。旧连接帧拒收用例含显式本地注入，不能写成真实网络逐帧 epoch 隔离已完成。生产接管已热服务、检测重启前的迟到帧及完整身份仍需进一步适配。

固定 HUST 的 KVEventsConfig.publisher 是 Literal["null", "zmq"]。即使工厂提供 register_publisher，也不能由此推出第三个 publisher 名称可通过配置校验；可选适配因此守卫现有 zmq 工厂绑定，不新增未经配置准入的名称。默认缓存哈希为 xxhash，首块 NONE_HASH 在未设置 PYTHONHASHSEED 时随机初始化；请求哈希复现不能盲目照搬上游默认或跨进程假设。本轮纯文本 token 身份子集已有证据，但未调整哈希算法/seed，也不宣称原生完整 bytes 父链重算或生产精确路由通过。

存储/自然移除/清空缺失、帧或范围错误均失败，保留原报告；只清理本工具创建的服务组及 socket。报告成功不允许默默清除未恢复目录的 unknown 状态。

共同边界/已热 token 快照的可选适配路线已由用户批准实验，并有回归证据；下一项验收是压力/竞态/开销及生产消费者接线。不能把禁用精确模式写成精确模式已通过，不因此决定重写索引或 fork 引擎。
