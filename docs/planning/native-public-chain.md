# 原生订阅与真实公开 HTTP 链验收

更新：2026-10-17。状态：候选实现，结果须由独立 evidence 记录；本页不代表生产 ABI 冻结。原 volatile 模式保留；本轮增加显式 `--durable-store` 资格模式，不能将两种模式的结果混用。

`hust_runtime_probe.rs` 使用固定 Router 的 `build_app` 和原 HTTP/SSE 转发。同一进程安装实际 Qwen tokenizer、CompletionFeaturePort、原索引/InProcessCache、RuntimeOwner 与基础 RuntimeProvider。两个专属阻塞 owner 分别持有原生 SUB/DEALER 和 HustSourceControl，500ms 控制刷新；这些期限仅是资格测试配置。

`tools/hust_runtime_chain.py` 复用 OwnedBackend、已有 HUST 启动环境、版本/ACL 守卫和进程清理，启动两个受控空服务。控制配置仅写入 0600 私有 artifacts 文件；Python 不转发推理请求。当前 wheel 必须提供独立身份接口，不能只根据同版本字符串认定安装内容正确。

## 最小验收

1. 两端冷身份、PID、原生空起点通过；两个 M7 owner 写入同一个 M2 Pool 的独立分区。
2. 在副本 B 直接预热公开合成前缀，验证真实事件得到三个块且 A 无该驻留；该预热不计为 Router 请求或策略性能。
3. 经过真实公开 HTTP 入口发送 A 冷、A 热、B 热请求，真实 cached_tokens 预期为 0/384/384，M3 实际目标应为 A/A/B。
4. 每次先保留 M3 已发送负债，再验证完整终态批次、全部 family/child 与来源，应用同一 owner，重复应用不重复释放。原 volatile 模式不发送持久 ACK；durable 模式要求先持久化 M3/cursor，再验证未 ACK 时仍能重放，拒绝旧 cursor 重复应用，最后才 ACK。
5. 不支持的数组 prompt 明确拒绝；流式收到实际生成内容后断开，HTTP 断开不清债，实际核心 ABORTED 才结清。
6. 先停止公开 HTTP，再停订阅和控制；目录停止后为 unknown，自有进程正常退出。保留失败和重试日志。

## 后续持久化与冻结条件

此例仅有界资格接线，不能作为最终二进制/Manager 生命周期。正常、取消之外还要补并发配置竞争、控制失联、Router 崩溃和启动排他。durable 模式在真实请求后检查磁盘 active 保债及暂停重开，结束后再核对游标；它不是强制退出后的端到端恢复。终态接收不能先 ACK 再落盘。

长期服务还受 HUST 可选观测的 core seen、frontend family/child 留存上限限制。现有 journal 只允许回收自身分配的 managed ID，实际 HUST 入口绑定的是 opaque child 和外部 Router attempt，不能直接调用该回收原语或按 TTL 删除。下一项必须明确 Router 不复用命名空间、持久消费者游标、已结清记录回收及迟到重复/旧进程的处理，再选择可测的 Rust 本地提交方案。

生产接口需在这些行为通过后冻结。基本 HTTP 实机通过不代表持久恢复、长期 GC、图模式、性能或 MOD enable 通过；M8 和最终算法不作为冻结前置条件。
