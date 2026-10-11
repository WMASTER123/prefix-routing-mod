# CPU 组件契约与 Router 准入证据

日期：2026-10-11。变更基线 `feaeddc1c21ec6ec084dfda8dda7da27bace4090`；本轮是 CPU 工作，不继承此前 NPU 分配的通过结论。

## 范围和资源

- 用户授权的新 CPU 服务器，项目 checkout `/root/work/prefix-routing-mod`；aarch64、Python 3.12.13，cgroup CPU `1600000/100000`、RAM `68719476736` bytes、shm 16 GiB。当前 0 NPU。
- 持久工程 HEAD/分支与本机基线相同，起始干净。未连接受限 SSH alias、旧分配或旧 prefix-router；未启用模型、改变推理 venv、公共模型或全局环境。
- 只在本项目 `artifacts/router-admission-20261011` 安装 Rust 1.99.0 / Cargo 1.99.0，rustup 1.28.2 aarch64 安装器官方 SHA256 `e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c` 校验通过；`--no-modify-path`、minimal、项目私有 CARGO_HOME/RUSTUP_HOME。它是构建探针工具，不是 HUST 基线升级。

## 本轮实际交付

`component-v0.1` 标准库类型与五个端口；六组版本化生命周期正例；有界/带锁账本 oracle、静态目录及模拟 M1/M2/M3/M4/M5 链。新增 73 项 CPU 用例，涵盖未知/精确、连续完整块、新鲜度、域/模板、服务与 publisher 代次、配置/排空竞争、并发准入、幂等/冲突、发送前撤销、发送后未知和晚到确认、实例更换、来源/留存上限。

参考 ledger 和静态目录只在 tests/fixtures，不是生产 M2/M3；FixturePolicy 只取首个合法目标并标记证据，未实现/选择新优化策略。生产宿主语言、ABI/绑定、可靠本地生命周期和长期 GC 仍未交付；MOD import_only 保持。

## 固定 Router 的源码检查

候选提交 `91b65ee4bdfbe98863a32a2205dced5dbb249ffe`。本机 clean clone 的 HEAD 导出原始 tar，SHA256 `eba53db252b19decc7d239a4b2f8c3b0e354db899793555cb1dad05f3c71156f`，远端验证后解包，未改 Cargo.toml/lock/source。

| 接缝 | 实际源码事实 / 对本项目的影响 |
|---|---|
| 普通 policy | `src/policies/mod.rs` 的 LoadBalancingPolicy 输入 text/headers，输出 worker index；无完整 RequestFeatures/尝试上下文或原子预记账合同 |
| 注入 | `src/policies/registry.rs` 普通 model/default policy 由具体 PolicyConfig 创建，未找到公开 setter；P/D setter 存在但不应为了注入普通策略启用 P/D |
| 选点与发送 | `src/routers/http/router.rs` select_worker_for_model → retry closure → send_typed_request；这里需要精确特征与条件预记账接缝，不能只新建 Python policy |
| 转发负载 | LoadTrackedBody EOF/Drop release 本地 worker.load；这是代理计数回收，不等于确认后端已终止。不直接映射成新合同 CANCEL_CONFIRMED |
| program 路径 | 已有具体 ProgramScheduler/ProgramCompletion，finish/Drop 做其自有调度记账；不是可直接注入 M3/M4 的公共 trait。可作为生命周期实现参考，不为了本项目带入完整 program 调度功能 |
| KV 索引 | 已导出但未接普通在线策略；HUST framing/replay/snapshot/epoch 和有界 anchor 适配仍须准入，详见活复用评估与此前 G2 证据 |
| 构建依赖 | Wasmtime/Kubernetes 和 vLLM Rust chat/tokenizer/text 等无 HTTP-only feature gate；锁定三份 Git 来源。不能把依赖的上游 Rust tag 当必须替换 HUST Python 引擎 |

primary source：[固定候选源码](https://github.com/vllm-project/router/tree/91b65ee4bdfbe98863a32a2205dced5dbb249ffe)。这是接入缺口，不是现有 Router 性能差或已被本 MOD 超越的证据。

## 构建探针过程

命令 `cargo check --lib --locked`，4 jobs、600 秒单次上限、项目 target/cache，原源码/锁不修改。尚不等价于 release 二进制、aarch64 请求转发或生产准入。

1. 直接 GitHub 依赖拉取超时，exit 101；末尾 revision-not-found 是伴随网络错误，不能解读为版本不存在。
2. 本机已核查/转送锁中原提交：vLLM Rust `98dff2a81d747d1dba01a47f939f48c3526d4206`；llm-multimodal `15adba5e025d8636ba4a334fb379b1371f6196a1`；harmony `76e849426cc092f84509e31a17027755f67d662a`。转送仅构建源码，用进程级 Git insteadOf，不改全局配置，不安装这些 Python 引擎。
3. 浅克隆用普通 bundle 不带 shallow 边界，远端 clone 失败；保留失败对象，改为校验后的原 bare Git 归档，fsck 通过。Cargo 能继续读原提交，fetch 有 shallow-ref warning；不通过改 commit/锁消除 warning。
4. 对第二/第三 Git 依赖的直接网络失败分别保留 `cargo-check-02.log`、`cargo-check-03.log`；完整原锁重试 `cargo-check-04.log` 的结果另行补记。不把网络失败算宿主否决或构建成功。

构建状态暂记 pending，不继承工具链安装成功。raw 日志/归档留在 ignored artifacts；不提交大源码包或内部访问资料。

## 测试和审查

| 检查 | 本轮结果 |
|---|---|
| Windows Python 3.12 CPU | 256 passed、2 Windows skips；ruff/format pass |
| Windows loopback integration | 3 passed；不是生产 Router |
| Windows sdist→wheel→干净安装 | pass；合同、manifest 和默认关闭插件导入无 torch/vllm |
| Linux aarch64 CPU / integration / wheel | 待本轮代码同步后执行，不能继承旧 185/3 |
| Actions 3.11/3.12 | 待本轮草稿 PR 的确切 head 验证 |
| NPU / 真实 Router / 性能 | not-run，无卡，不宣称支持或提升 |

代码自检实际修正：负载变化不作为全局控制版本失效；同请求未知禁止再试；未知不擦除已有接受证据；本地账本计数不接受后端来源；配置切换要拒绝旧未预记账决定。收紧本地计数时钟后，一项旧 foreign-clock fixture 在构造阶段失败，已保留首个失败报告并修正 fixture 的自洽时钟，完整复测通过。这不是改验收条件迁就实现。

自检不替代另一成员独立审查。PR 保持草稿、不合并；进入生产适配前必须复核 ABI、Host 开销、配置/排空线性化、恢复和限额。继续申请卡：真实 G2/G3 1 张；真实 G4 2 张、两个 TP=1，届时反馈。
