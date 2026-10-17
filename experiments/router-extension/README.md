# 普通 HTTP 接口接缝 CPU 原型

2026-10-13 新增显式 `--online` 接收实验：`online_bridge.rs` 控制线程读取已有 LifecycleTrace 的待确认观察，提交成功才推进游标。Lease 回调没有网络或磁盘操作。源观察序号与账本转换序号分离；有界留存、丢确认重投、断连锁派发和停止后拒绝均独立测试。不开启选项不启动控制线程。原三文件宿主 patch 不变，不新增生产代理/账本服务。

可同时指定 `--lose-first-ack`：在实际接收提交后丢第一次确认，宿主只允许第一个请求到后端，重投确认后仍拒绝新派发。该显式故障选项必须与 `--online` 同用；不覆盖原正常/关闭回归。探测结果只在所有在线断言和接收上下文关闭成功后标 pass。

在下方隔离构建步骤中，还需将 `online_bridge.rs` 复制到同一宿主 `examples/` 目录。构建完成后执行 `python -m tools.router_extension_probe --binary <刚构建的 example> --output <新的 artifacts 目录> --online`，再省略 `--online` 做关闭回归。工具在启动前持久化 64 条合成预留；未使用预留保持负债并单独报告，不声称真实动态 reserve 已联通。原子提交仍用实验 SQLite，不能把异步投递解释为性能问题解决。详细边界和结果见[本轮证据](../../evidence/online-transport-2026-10-13.md)。

2026-10-12 审查收尾：空数据块被过滤，每次 poll 处理一个空块后让出执行权，持续空块不能绕过 idle。已有非空数据优先交付，允许慢消费者暂停后读取缓冲数据；idle 只在消费时检查，完全停止消费时的期限由宿主/下游机制另行验收。`checks/` 直接包含同一 helper 和 `lifecycle_bridge.rs`，使用虚拟时钟运行回归并进入 CI，不代替完整 Router、HTTP/2 或 NPU 验收。

`prefix_extension_probe` 的 Lease 由实验 `TracedLease` 包装，生成 component-v0.1 的有界传输记录；工具在进程退出后重放到既有参考账本并校验幂等。每尝试最多两个事件，不保留逐 token 事件或请求内容，fixture 最多 128 尝试。全记录中的未知数与最后一批 fixture 未决数分别报告，因为原 fixture 中间使用合成 ENDED 清债；记录不伪装真实后端确认。生产可靠投递/留存/重启恢复仍待实现，范围见[终态审计](../../docs/planning/backend-terminal-evidence.md)。

2026-10-12 增加可选 `stream_limits` 与 `DirectStream` CPU 实验。未安装 provider 或未选择该选项时仍走原生路径；选择后从同一 reqwest response 按客户端消费进度拉取，无中间队列或 detached producer。drop/error/超限释放本地 source，生成原有传输证据，不认定 backend ENDED。新 example 为 `stream_extension_probe.rs`，启动工具为 `tools/router_stream_probe.py`。

当前 patch 仍涉及三个原有文件，逻辑增量 202 新增/3 删除行；新增 helper/两个 example 独立计入本项目维护成本。队列消除不代表整个 HTTP 栈内存有硬上限：reqwest/Hyper/socket 缓冲、非流式响应、请求入口和并发准入仍需分别验收。每 chunk/累计响应/idle 限制是显式实验配置，不作为生产默认。

`stream_extension_probe` 直接检查无消费时零 pull、慢消费者、源 drop、idle、chunk/总量、源错误与正常 EOF；随后走实际 Router 的普通和负载追踪两条 HTTP 分支，检查本地回调恰一次、原生连接计数释放及配置错误在发送前拒绝。真实结束对账与标准 LifecycleEvent 桥仍未交付。诊断 stage-comment 模式与该选项组合目前明确拒绝；未选择该选项不改变原诊断路径。

仅用于 R1 评估，不是 MOD 激活、生产插件 ABI 或性能优化实现。底座为 Apache-2.0 的 [vLLM Router 固定提交 91b65ee](https://github.com/vllm-project/router/tree/91b65ee4bdfbe98863a32a2205dced5dbb249ffe)。保留上游许可证；本目录新增文件同许可。原版源码、Cargo.toml/lock、HUST 源码不修改。

## 实际增量

`router-91b65ee.patch` 只改 policy registry、HTTP mod 和 typed HTTP Router 三个宿主文件；`prefix_extension.rs` 是本项目接口/guard，`prefix_extension_probe.rs` 是实际宿主库测试 example，不是新代理。没有引入 P/D、分层、program 调度或 Python/Rust 逐请求互调。

| 接缝 | 原型行为 | 尚未证明的生产能力 |
|---|---|---|
| 注入 | registry OnceLock 保存可选 provider，未安装时原生 policy/转发路径；重复安装拒绝 | 运行期拆卸、排空/进程重启账本恢复、MOD 二进制装配 |
| 特征/决定 | 一次 prepare 接收完整序列化请求；只读 select 返回目标与不透明完整决定；M3 reserve 收到同一特征和决定 | 真实 tokenizer/template/domain/instance 对应、零拷贝 Host 开销；fixture token 不是真实 Qwen token |
| 发送门 | 当前候选池/registry revision 检查；reserve 后、HTTP send await 前检查本地控制/配置 fence；发送前拒绝撤销 | 真实 HUST 实例身份、并发排空协议；不声称网络发送与控制面原子化 |
| 生命周期 | 独占 RAII guard，发送前 not_sent；发送后 EOF、5xx、网络错误、body drop/future cancel 均保留 unknown；关闭回调恰一次 | 原型回调不是完整 LifecycleEvent 协议；可靠终止、晚到对账、事件投递与持久化未接 |
| 重试/回退 | provider 安装时关闭模糊自动重试；select None 明确回退原生选点；未安装 provider 使用原重试配置 | 所有路由/故障策略等价性；本轮仅 completion/chat ordinary HTTP 范围 |

当前接缝接受范围仅 HTTP、DP=1、无 program scheduler、completion/chat；范围不符明确拒绝。fixture provider 只验证 completion，chat 真实特征桥未验收；原版 chat 转发成绩不继承为启用扩展的成绩。生产还必须在接入层阻止其他推理路由绕过账本。默认原生 channel 保留；显式 pull 选项不创建 producer。两种模式的客户端 drop 都不证明后端已经取消。

provider 回调必须本地、有界、无阻塞、不抛 panic；完整决策与特征用同进程 Arc 传递。不透明 trait 是实验工具，不能直接冻结成团队生产类型。一次 `serde_json::to_value`、候选 URL 收集和 body wrapper 都有成本，尚无收益/热路径开销数据。

## 复现与回退

先用 `bash tools/router_build_probe.sh preflight` 复核固定源文件集合/内容/链接与私有 SDK，再用 `build` 准入原版和执行原生二进制 HTTP probe。内容核查不将共享目录权限重映射误判为源码修改，其他归档/Cargo/RPM hash 守卫保留。该入口只接受已审计的项目私有工具链/归档/SDK，前置材料来源见本轮证据。不是新服务器的一键安装器。

在项目 artifacts 下新建独立实验目录，从已校验原 tar 解包；应用前核对归档 SHA256 `eba53db252b19decc7d239a4b2f8c3b0e354db899793555cb1dad05f3c71156f`。该固定提交部分源码是 CRLF，patch 是 LF：在非 Git 的独立副本使用进程级 `GIT_CEILING_DIRECTORIES` 阻止父仓库发现，`git -c core.autocrlf=true -C "$task_prototype" apply --check --whitespace=error <绝对 patch 路径>`，通过后同设置 apply；不改全局 Git 或原版源码。随后仅对 registry.rs、router.rs 运行 `rustfmt --edition 2021 --config newline_style=Unix`，不对 mod.rs 递归格式化子模块。本轮已核对复放输出与实际编译输入逐字节一致，新增/删除行数为规范化后的逻辑增量。本原型 patch 不应应用到原版准入目录或 HUST。把 helper `prefix_extension.rs` 复制到 `src/routers/http/prefix_extension.rs`，把两个 probe `.rs` 复制到 `examples/` 下各自同名文件。共享已锁构建环境/target，运行：

本轮还需将 `lifecycle_bridge.rs` 复制为实验宿主的 `examples/lifecycle_bridge.rs`，并使用更新后的 admission example；它不修改宿主模块注册或引擎。原生 helper 快速检查可独立执行 `cargo test --locked --manifest-path experiments/router-extension/checks/Cargo.toml`。

```bash
cargo build --release --locked --example prefix_extension_probe --example stream_extension_probe --manifest-path "$task_prototype/Cargo.toml"
python -m tools.router_extension_probe --binary "$task_target/release/examples/prefix_extension_probe" --output artifacts/router-admission-20261011/prototype-http-new
python -m tools.router_stream_probe --binary "$task_target/release/examples/stream_extension_probe" --output artifacts/router-admission-20261011/prototype-stream-new
```

`task_prototype` 和 `task_target` 是操作员核验过的本项目私有绝对路径，输出目录须新建。首次 example 会启用原锁中的 dev-dependency 图，离线 cache 不全可能失败；保留失败，再允许原锁下载，不改依赖。Rustfmt 同样安装在私有工具链，不改系统 Rust。

默认关闭测试在**未安装 provider 的独立 AppContext** 上执行；显式 fallback 在安装 provider 后执行，两者不可混淆。实际 HTTP 后端记录发送数，验证被拒绝的请求没有到后端、发送后没有重复尝试。测试同时覆盖 16 并发竞争 8 个额度；未知占满后拒绝新请求，fixture 用显式合成 ENDED 证据对账，不用 HTTP EOF 清账。

禁用方式是下次启动不安装 provider；运行中拆卸未实现。完整回退保留原版源码/原版可执行文件，重建干净原提交，不在活动实验目录中盲目撤销更改。正式回退还必须先排空/恢复未决账本，不能用重启抹掉未知工作。

## 维护与退出条件

升级前重新检查三处宿主上下文、原锁构建与全套 default-off/发送门/drop/重试测试；不放宽 hash/版本范围去接受未知源码。当前不能承诺固定维护行数或原 wheel 无需重编译。

索引 E5 的单分区规范化 CPU 恢复/父链/预算与并发读写已有实验；真实 M7 接线、多分区总预算和大工作集重建延迟仍需验收。E6 新 pull 选项评估流式资源；请求入口、非流式和并发响应的总预算仍需补齐。完整 LifecycleEvent 桥、真实实例身份/可靠对账及生产制品/Manager 启停未完成。只冻结证据足够的接口，不把 example 当生产组件交付。
