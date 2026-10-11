# 普通 HTTP 接口接缝 CPU 原型

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

当前接缝接受范围仅 HTTP、DP=1、无 program scheduler、completion/chat；范围不符明确拒绝。fixture provider 本轮只验证 completion，chat 真实特征桥未验收；原版 chat 转发成绩不继承为启用扩展的成绩。生产还必须在接入层阻止其他推理路由绕过账本。本原型没有改原生流式 channel，客户端 drop 仅证明本地 guard 收到关闭，**不证明 producer 或后端已经取消**。

provider 回调必须本地、有界、无阻塞、不抛 panic；完整决策与特征用同进程 Arc 传递。不透明 trait 是实验工具，不能直接冻结成团队生产类型。一次 `serde_json::to_value`、候选 URL 收集和 body wrapper 都有成本，尚无收益/热路径开销数据。

## 复现与回退

先用 `bash tools/router_build_probe.sh build` 准入原版，再执行原生二进制 HTTP probe。该入口只接受已审计的项目私有工具链/归档/SDK，前置材料来源见本轮证据。不是新服务器的一键安装器。

在项目 artifacts 下新建独立实验目录，从已校验原 tar 解包；应用前核对归档 SHA256 `eba53db252b19decc7d239a4b2f8c3b0e354db899793555cb1dad05f3c71156f`。该固定提交部分源码是 CRLF，patch 是 LF：在非 Git 的独立副本使用进程级 `GIT_CEILING_DIRECTORIES` 阻止父仓库发现，`git -c core.autocrlf=true -C "$task_prototype" apply --check --whitespace=error <绝对 patch 路径>`，通过后同设置 apply；不改全局 Git 或原版源码。随后仅对 registry.rs、router.rs 运行 `rustfmt --edition 2021 --config newline_style=Unix`，不对 mod.rs 递归格式化子模块。本轮已核对复放输出与实际编译输入逐字节一致，新增/删除行数为规范化后的逻辑增量。本原型 patch 不应应用到原版准入目录或 HUST。把本目录两份 `.rs` 分别复制到 `src/routers/http/prefix_extension.rs`、`examples/prefix_extension_probe.rs`。共享已锁构建环境/target，运行：

```bash
cargo build --release --locked --example prefix_extension_probe --manifest-path "$task_prototype/Cargo.toml"
python -m tools.router_extension_probe --binary "$task_target/release/examples/prefix_extension_probe" --output artifacts/router-admission-20261011/prototype-http-new
```

`task_prototype` 和 `task_target` 是操作员核验过的本项目私有绝对路径，输出目录须新建。首次 example 会启用原锁中的 dev-dependency 图，离线 cache 不全可能失败；保留失败，再允许原锁下载，不改依赖。Rustfmt 同样安装在私有工具链，不改系统 Rust。

默认关闭测试在**未安装 provider 的独立 AppContext** 上执行；显式 fallback 在安装 provider 后执行，两者不可混淆。实际 HTTP 后端记录发送数，验证被拒绝的请求没有到后端、发送后没有重复尝试。测试同时覆盖 16 并发竞争 8 个额度；未知占满后拒绝新请求，fixture 用显式合成 ENDED 证据对账，不用 HTTP EOF 清账。

禁用方式是下次启动不安装 provider；运行中拆卸未实现。完整回退保留原版源码/原版可执行文件，重建干净原提交，不在活动实验目录中盲目撤销更改。正式回退还必须先排空/恢复未决账本，不能用重启抹掉未知工作。

## 维护与退出条件

升级前重新检查三处宿主上下文、原锁构建与全套 default-off/发送门/drop/重试测试；不放宽 hash/版本范围去接受未知源码。当前不能承诺固定维护行数或原 wheel 无需重编译。

索引 E5 需完整共同水位恢复、父链验证、预算及并发视图寿命；转发 E6 需慢客户端背压、producer 停止、请求/响应/队列预算。另需完整 LifecycleEvent 桥、真实实例身份/对账及生产制品/Manager 启停。上述未过时不冻结生产 ABI/目录/交付形态，不把这个 example 分配成八组件生产实现。
