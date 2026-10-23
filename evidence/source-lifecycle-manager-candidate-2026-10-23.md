# 控制/KV 统一生命周期、恢复与 Manager 入口候选记录

日期：2026-10-23  
代码输入：`b96d4c25a768b8ba417cf1a7e7b8a94bb7248c36`

## 本轮完成

- `ServiceSupervisor` 新增 `ManagedSourceTask` 边界，只接管 Router 自己创建的 KV 订阅和控制刷新线程；HUST backend 进程不属于该边界。
- 正常退出保持单一期限：先撤销新准入并等待 HTTP/后端负债/终态确认，再 stop-all，依次 join 终态、KV 和控制任务。任务重复 ID、未完成初始证明或粘滞失败均拒绝启动。
- `resume_recovered` 保持“恢复即暂停”：只有来源任务初始证明、控制绑定和 worker identity 同时成立，且调用方确认旧 dispatcher 已停止后，才持久 takeover 并启动终态消费者；失败会停止并 join 已创建的来源任务。
- 固定 HUST 宿主候选新增 `serve` / `recover-serve` 常驻模式，组合原 HTTP、M1、原 M2、M3/M4/M5、终态驱动、readiness、在途计数和上述统一线程收尾。它仍需固定宿主编译与真实双 NPU 资格。
- 新增 `prefix-routing-mod manager-run` 用户入口，复用固定 `extension-manager@00d1b940` 的 supervisor。入口要求绝对 Router 可执行路径，校验 drain/grace 边界和固定 Manager 提交，只管理 Router 子进程。
- MOD manifest 仍为 `import_only`，`extension enable` 继续失败；增加监督启动入口不等同于生产激活。

## CPU 审查结果

- Windows Python：`614 passed, 7 skipped`。
- Manager/manifest 定向：`6 passed`。
- Ruff：通过。
- GitHub `CPU checks`：成功，run `39932837554`。
- GitHub `Native HTTP extension helper checks`：成功，run `39932837427`；包含 Rust fmt、严格 Clippy、共享 helper、有限/后台终态驱动以及 Manager 信号恢复回归。
- GitHub `Native index adapter checks`：成功，run `39932837531`。

## 未完成与禁止扩大解释

- 上一台 `36.140.239.91:32049` 在 2026-10-23 已由平台关闭连接，本轮未获得新的服务器，因此未完成固定 Router 宿主的 `hust_runtime_probe` 构建，也未执行真实 HUST 常驻 `serve`、SIGTERM 排空、`recover-serve` 或双 NPU 请求。
- 没有新增 NPU、性能、容量或缓存收益结论。不得把 CPU substitute 结果写成真实 HUST service 已通过。
- `recover-serve` 的 `previous_dispatcher_stopped` 前提只适用于同一 Manager 已确认旧 Router 子进程退出的部署边界；跨 Manager/跨主机单写租约不在首版范围。
- 私有资格配置中的 `service_listen` / `service_drain_seconds` 尚不是冻结的公开配置 schema；真实资格通过前不得写入 manifest activation。

## 下一次实机顺序

需要 **2 张 NPU**，继续使用两个 TP=1 Qwen2.5-14B-Instruct HUST 副本：

1. 将提交同步到 `/root/work/prefix-routing-mod`，固定宿主构建并重跑完整 39 项宿主回归。
2. 使用全新账本和冷来源启动 `serve`，验证 readiness、连续请求、取消、控制失联、KV 断连以及 SIGTERM 的 KV/控制/终态 stop-all + join。
3. 在旧 Router 已确认退出后用原账本启动 `recover-serve`，验证默认暂停、旧债保留、同身份来源复核、takeover、M2 unknown 和恢复后新请求。
4. 用固定 Manager 执行同样的正常信号、二次信号和 grace 超时，确认仅清理 Router 进程树，不停止两个 HUST 后端。

