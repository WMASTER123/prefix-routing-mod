# 固定 HUST 原生 KV wire 的 Rust 适配证据

日期：2026-10-16。结论：有界解码与连续性 CPU 候选通过，并能装入固定 Router 宿主；真实 ZMQ socket owner 尚未接入。

## 实现范围

`experiments/router-extension/native_kv_wire.rs` 只承担固定 `hust-cedb477-native-v1` 的输入适配：

- 校验 live/replay multipart 数量、空 topic、8 字节大端序号和 END 标记；END 只表示 replay 结束，不产生快照或 publisher epoch。
- MessagePack 解码前限制单 payload 1 MiB；解码后限制 4096 operations、262144 tokens、block size 128。
- 只接受 32 字节 block hash、GPU/group0、`full_attention`、LOCAL/未指定 locality、无 ownership/LoRA/滑窗/额外键的固定身份。
- token ID 按小端 u32 做 SHA256，与既有原 M2 本地键一致；token 不保留在目录记录中。
- 连续性窗口只保留至多 128 个 `(sequence, SHA256 fingerprint)`，不保留完整 batch；精确重复幂等，冲突重复、窗口外重复、缺口或序号溢出后永久失败，调用方必须撤销该分区信任。

模块不创建线程、socket、文件或设备调用；也不负责决定受控空起点、实例身份或恢复共同边界。这些仍由后续 subscriber owner 负责。

## 审查中修正

1. 初版把 clear 表示为 Serde 单元枚举；反例表明未知字段会被忽略。改为空结构变体后，`AllBlocksCleared + unknown` 明确拒绝。
2. 初版连续性窗口保留完整 batch，最坏可积累 `128×4096` 个操作。改为每批 32 字节指纹，避免重复检测本身成为长期 Host 内存来源。
3. 首次固定宿主构建误用了旧服务器不存在的 SDK 目录；改回本项目已审计 `build-sysroot` 后通过，没有安装系统包或改变 Router/HUST 源码。

## 验证

- aarch64 release：原索引、Pool、CachePort 与新 wire 共 74 tests passed，0 failed。
- fixture 由固定 HUST 环境的 `KVEventBatch/BlockStored` 与 `msgspec.msgpack.encode` 生成，使用合成 hashes/tokens/session，不含真实请求内容；Rust 成功得到两个父子 store。
- 覆盖错误帧、超限 payload、未知字段、错误介质、END/live 混淆、精确重复、冲突/过窗重复和序号缺口。
- 相同源码复制进固定 `vllm-project/router@91b65ee` 隔离宿主后 release 构建通过；只保留宿主原有 `Atomic::fetch_update` 弃用警告。
- 固定 Router typed HTTP + 原 M2 + M3 组合探针再次通过，真实 loopback 转发、选点、负债、可靠终态、实例替换、断连和配额回收结论未退化。

| 制品 | SHA256 |
|---|---|
| `runtime_cache_owner_probe` | `0afa4194d97fb4dc68904a6626379fcbf4c451509c2f8f54f17754438df96786` |
| `runtime-cache-native-wire-bounded-20261016/report.json` | `8902a6419a4bace344ad4aa367f106d21bf1895aa2c4ceec79597cf7da20107f` |
| `runtime-cache-native-wire-bounded-20261016/probe.log` | `45ef52a3e9b96a14570fb7fdb221d96ff8ee116223c7efab998ee967dec05601` |

没有把该结果标为在线订阅通过。后续单 owner 与 ZMQ 候选进展见[新增证据](native-kv-owner-zmq-2026-10-16.md)：统一失信状态机已有 CPU 成绩，实际 socket 行为和真实 HUST 接线仍待完成。
