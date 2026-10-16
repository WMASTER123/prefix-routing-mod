# 固定 HUST 原生 KV 单 owner 与 ZMQ 传输候选

日期：2026-10-16。收尾：Linux x86_64/aarch64 的共享原索引/owner/IPC/身份控制各 90 项、固定完整宿主 36 项和真实 HUST Rust 公开链均通过，见[后续证据](native-public-chain-2026-10-16.md)。下文保留阶段验证过程，不能将历史“下一门槛”作为当前未完成项；生产持久恢复/长期回收仍未验收。

## 实现与边界

- `native_kv_owner.rs` 只拥有一个精确 `PartitionSpec` 的信任状态，复用同一个 `InProcessCache` 和原 Router 索引，不创建第二棵 KV 树。
- 启动只接受经过只读身份检查且 legacy replay 为空的受控冷起点。随后注册空视图、重放连续增量并在控制检查前后身份一致时确认租约；普通实时帧不能恢复已撤销的信任。
- 解码、序号缺口/冲突、源身份、控制、传输或索引任一失败，及显式停止/owner drop，均撤销该精确分区的 M2 信任；不修改 M3 请求负债。旧 owner 对已经替换的新实例只能得到 stale，不能撤销新实例。
- `native_kv_zmq.rs` 显式创建一个 SUB、一个 DEALER 和一个断连 monitor，只接受长度受限的同机 `ipc:///` 端点。接收逐帧检查帧数和总大小，replay 有 128 批与 30 秒以内的调用方期限；模块不自行创建线程，不在包导入时联网。
- 原生协议仍没有 publisher epoch 或完整共同水位快照。因此当前设计不支持任意已热服务接入或超窗恢复；恢复需新的受控起点。

## 审查中修正

1. 原 Rust `BytesVisitor` 同时接受 MessagePack binary 和整数数组，会扩大固定 HUST 协议。删除数组访问器，测试 fixture 改为显式 binary 编码；真实固定 `msgspec` fixture仍通过。
2. `poll_once` 初版对传输和解码错误使用提前返回，可能绕过统一失信。改为先收敛成结果，再在任何错误上执行精确 `disconnect`。
3. 连续性先推进而索引应用失败时，owner 立即永久失败并失信，不能在同一 owner 上继续；新的受控恢复必须取得新 ticket。
4. 配置窗口在注册前验证；空视图安装失败会注销已占用分区，避免错误配置或半初始化占用全池额度。

## 已执行验证

- 固定原索引、Pool、CachePort、wire 与新 owner 的最终提交由 GitHub `validate-index` release job 验证为 `78 passed, 0 failed`，覆盖冷起点、replay+live、精确重复、序号缺口、控制失败、显式断连、warm origin拒绝、停止失信和同地址新实例替换。
- 固定 `vllm-project/router@91b65ee` 宿主 release `cargo check --locked --example runtime_cache_owner_probe` 在 ZMQ 候选初版上通过；只有宿主原有 `Atomic::fetch_update` 弃用警告。最终静态审查又调整 socket/context 析构顺序和单次 poll 上限，需在新服务器重跑构建。
- Windows Python：ruff通过；pytest `571 passed, 7 skipped`。GitHub Python 3.11/3.12 与固定 HUST 源码方法检查也通过。
- 固定 Router 的完整 example test 尚未取得最终结果，随后 SSH 返回 `Connection refused`。仅凭连接错误不能确认平台回收原因；该项记录为未完成。

## Linux 真实 IPC 增量

`experiments/router-index` 增加可选 `native-zmq` / `hust-control` feature，编译同一份传输、owner 和原索引；没有增加生产索引或修改固定 Router 的依赖锁。Rust 1.99.0，项目私有工具链，无全局安装；测试锁新增 libzmq/reqwest 的测试依赖，原已锁版本保留。

真实 XPUB/ROUTER peer 使用 0700 私有 IPC 目录和订阅就绪屏障。验证重放接收、实时增量、重复/冲突、缺口、错误帧数、总消息超限、超时/129 批上限、实际 PUB 断连、停止及旧 owner 对新实例无效。固定 HUST msgspec store fixture 经过真实 socket 写入原索引得到 2 块匹配，clear 后为可信 miss，停止后为 unknown。源控制是独立 HTTP fixture，不能算真实 HUST 认证。

审查修正：接收改用非阻塞逐帧读取；发送连接等待纳入同一 replay deadline，设置发送 HWM=1/IMMEDIATE，部分发送歧义直接失败，防止无界等待或错误重试。CI `validate-index` 新增 `--features hust-control` 的 release 实际 socket 回归。

新分配两张 910B2 的 `artifacts/r5/report.json` 通过旧 Python subscriber/同一原索引子集：两端实际 cached_tokens 为 0→384、单侧驻留隔离及断连降级正确，两个服务退出均为 0。该结果不代替 Rust 真实订阅或公开 Router 整链。

## 当时的下一门槛（现已完成有限公开链接线）

新服务器上先运行固定 Router example tests，再用私有 IPC fixture 覆盖正常 replay/live、重复、缺口、额外帧、超限、控制超时、SUB 断连、显式停止和实例替换。全部通过后，才把 `SourceControl` 映射到已验 HUST 只读终态身份接口，并用真实 HUST 事件替换组合探针的 fixture；随后再装入公开 HTTP 请求链。
