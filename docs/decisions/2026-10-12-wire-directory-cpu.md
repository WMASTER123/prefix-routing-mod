# ADR：有界 wire 规范化接复用索引，并限制注册预算

状态：CPU 接线评估；真实服务验收和生产 ABI 未冻结。日期：2026-10-12。

M7 负责四帧、publisher epoch、解码前 payload 限额、完整块/布局验证与本地 token digest。使用固定 HUST 原生 Struct/msgspec 编码的合成输入做联调；不导入引擎或启动设备。输出只含 opaque anchor 与 digest，不保存 tokens。M2 继续调用原 KvBlockIndexer，保留完整快照、祖先拓扑、连续增量和 unknown/miss 区别。

Rust CPU adapter 增加一个进程内 Pool：注册时保留分区逻辑额度，所有分区额度之和不能超过全池预算；不动态借贷或驱逐其他分区。查询还需要独立控制面确认，实时帧不续期确认。到期返回 unknown，断连/序号缺口需要恢复。旧实例/发布代次不能污染当前目录。

声明额度约束不等于进程 RSS 限制；解码、元数据、索引重建暂态及运行时开销需要实际资源测量。规范化 JSON 文件只用于 CPU 实验，不作为每请求生产跨语言序列化方案。worker instance 的 u64 为实验本地编号，生产仍按真实实例身份核验，不能把 opaque epoch 随意截断/hash 后宣称无碰撞。

下一步核实真实控制面握手/共同边界、完整 pool 生命周期和目标服务消息；使用真实请求 tokens 查询索引，验证恢复/新鲜度/资源开销。NPU 结果未通过前保持 MOD import_only。无需新代理或第二套匹配树。
