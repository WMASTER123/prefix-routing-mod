# R1：固定 Router 的窄幅扩展范围评估

2026-10-12 后续：[有界适配证据](../../evidence/bounded-router-adapters-2026-10-12.md)补上 E5 单分区规范化输入的恢复/父链/预算与并发 CPU 原型，继续调用原索引。E6 评估显式 pull 选项，仍复用现有 reqwest/HTTP Body；patch 仍三处宿主文件，逻辑增量 202/3。下方 147/3 是上一轮记录。是否通过以新证据的最终原生结果为准；多分区、真实 wire、新鲜度、请求/非流式总预算和标准可靠生命周期仍待实现，不因一个受限原型提前冻结生产交付。

2026-10-12 更新。用户已选择先评估窄幅 Router 扩展；不是授权大面积 fork，也不是修改 HUST 推理引擎。候选 `91b65ee4bdfbe98863a32a2205dced5dbb249ffe` 的原提交完整构建及 CPU/mock 转发通过；另在独立实验副本评估接口接缝，不修改原版准入源码或 HUST。状态及局限见[最新证据](../../evidence/router-cpu-admission-2026-10-12.md)。

## 基本判断

HTTP 服务、worker registry、健康/熔断、客户端、协议序列化与大部分流式转发仍值得复用。**不能承诺“加一个 policy 就够了”，也不能说安装 Python MOD 后原版 Router 自动支持新的接口。** 现有普通 policy 接口缺 token/尝试上下文、条件预记账和精细终态；KV 索引也未进入在线链。

推荐评估的交付形态是固定宿主库/构建制品 + 显式的扩展入口 + 本项目适配/优化模块。若选择 Rust 底座，热路径在一个进程内完成，不通过 Python/Rust 逐请求互调。是否需要定制二进制、如何将其装入 wheel/MOD 并保留原版回退，必须在 R1 原型和 ADR-002 决定，当前不宣称稳定插件 ABI 已存在。

## 六个接缝：哪些可窄幅，哪些有风险

| 接缝 | 原源码位置 / 当前语义 | 候选最小增量 | 风险与验收 |
|---|---|---|---|
| E1 可注入扩展 | policies/registry.rs、server.rs AppContext；model/default policy 内部按 PolicyConfig 构造 | 可选 builder/registry 注入入口，默认不装 provider；不借 P/D setter 冒充普通路由扩展 | 控制面改动相对集中；禁用模式须复现原版基础策略，不承诺现有 wheel 无重编译可用 |
| E2 特征与决定 | http/router.rs route_typed_request/select_worker_for_model，HTTP 普通路径是 text/headers | 一次解析/特征化、只读 context、扩展决定；传给 E3 的实例/版本与尝试关联 | M1 分词/模板必须与 HUST 对齐；不能重试时再次分词或拷贝整份 token；fallback 不伪装精确 |
| E3 发送门 | retry closure → send_typed_request 的 request builder/send await | M3 条件预记账和 M5 before-send/after-send 标记；控制/配置版本与准入限额检查 | reserve 到网络发送不能跨进程原子化；排空/重启竞争、确定未发送与模糊发送分别验收 |
| E4 生命周期与重试 | LoadTrackedBody Drop/EOF、send error、HTTP 状态重试、ProgramCompletion finish/Drop | 独立 attempt guard/有限事件，drop/error 标 unknown；首轮配置关闭模糊自动重试，可靠终态才清债 | 不照搬原 worker.load 释放当后端结束；需检查所有普通 HTTP 流/非流出口。真实取消证据不足时对账方案不能留空 |
| E5 可信索引绑定 | kv_index 的公共 KvBlockIndexer/TieredMatchProvider 与 subscriber/discovery | HUST 协议 adapter、staging 构建/完整验证/原子安装、query adapter；source 绑定当前实例/publisher | 原 store 缺 parent 时落 root、anchors 明示 never trimmed。可在 adapter 拒绝不完整输入；长期 GC/重建与预算可能不是“小改”，不能跳过风险门槛 |
| E6 资源边界 | 普通 HTTP 流路径存在 unbounded_channel；索引 anchors 无界保留；Cargo 无 HTTP-only feature | 有界流缓冲/可中止任务、元数据/恢复限额；独立构建 SDK 前置检查 | 缓冲 API 无界是源码事实，尚无实测内存退化；测慢客户端、背压和关闭。不要把所有 native worker.load、program scheduler 重写成新账本 |

证据来自[固定 HTTP Router](https://github.com/vllm-project/router/blob/91b65ee4bdfbe98863a32a2205dced5dbb249ffe/src/routers/http/router.rs)、[policy registry](https://github.com/vllm-project/router/blob/91b65ee4bdfbe98863a32a2205dced5dbb249ffe/src/policies/registry.rs)、[KV indexer](https://github.com/vllm-project/router/blob/91b65ee4bdfbe98863a32a2205dced5dbb249ffe/src/kv_index/indexer.rs)。这里是改动边界评估，不是代码行数/工期承诺或收益实验。

## 构建问题已定位，不改推理版本

原锁源码的 cargo check 已进入 native 编译，最终 exit 101：openssl-sys 0.9.117 找不到 headers/pkg-config SDK。openEuler 24.03 LTS-SP3 装有 openssl/openssl-libs `3.0.12-52.oe2403sp3.aarch64`，`openssl-devel` 未装；runtime libssl/libcrypto 存在。不能说“系统没有 SSL”，也不能把它归为 HUST/NPU 运行不兼容。

正常依赖树确认 native-tls 经 reqwest/hf-hub，以及 vLLM Rust chat/text 和 multimodal 引入 OpenSSL；顶层存在 rustls 并不会自动关闭这些 native-tls features。三个 Git 依赖已经按锁中原提交转送，网络失败已与此 SDK 缺口分开。Cargo.toml/lock hash 与本机原提交相同。

上述是首次失败根因，现已通过完整前置核查和项目隔离 build sysroot 解决：开发 RPM 与运行库版本匹配，签名/hash 通过，头文件和开发链接仅在项目目录中。首次 release 链接又暴露开发链接的 `.so.3.0.12` 目标未复制，导致误用静态 archive 的 zlib 未解析符号；修正真实目标并显式动态链接后 release 通过，不补装其他系统库。原依赖锁、HUST/CANN/Torch 不改。proto 构建实际使用 Rust protox，未安装 protoc；它不构成本候选要求。若后续必须系统级修改或改变候选，先讨论。

原提交 release、动态库解析、loopback API/SSE 及正常关闭已有 CPU 证据，健康时 RSS 约 24 MiB 是一次启动观测，不是 Host 开销或容量评测。生产制品、升级/禁用回退、慢客户端、可信索引恢复仍单独验收，不能把原版基本转发通过当全部准入。

## 下一轮评估退出条件

2026-10-12 最小接缝结果：三处宿主逻辑 patch 为 147 新增/3 删除行；独立 release example 与真实 HTTP fixture 连续三次通过，default-off、条件准入、错误/drop/取消保留未知及并发限额有证据。启用扩展仅测 completion，token/实例身份与对账为合成 fixture；不是稳定 ABI。原索引 80 万次变动 RSS 约 412 MiB，完整重建实验约 7.7 MiB；E5/E6 和生产生命周期/制品仍开放，不能因为 patch 行数少就宣布窄幅维护已验收。

1. 原提交完整可重复构建并检查默认关闭路径；如果有其他真实根因，保存首个失败，不按出错顺序无目的升级库。
2. 只做一个普通 HTTP/单池 extension seam CPU 原型，不实现优化算法，不引入 P/D/program 全套。用 component-v0.1 等价行为测试证明 E1–E4 接线，而不是另造代理。
3. E5 必须给出正确恢复和有界索引的可行实现及测量预算。若需要长期维护索引内部算法/大范围 transport 分支，标记超出窄幅范围，再与用户讨论是否换底座。
4. 提交改动文件/API 清单、禁用差异、上游升级守卫、制品和维护成本，再冻结宿主/生产语言及三人的实际代码目录。此前只分配独立语义/测试工作。

**否决线**：需要改引擎 scheduler/allocator；复制整套代理/索引；用请求历史冒充实际 KV；靠取消确认缺失时无限清零负债；扩展强制改变原版基础策略；需让普通路由走 P/D/program 功能才能注入；或资源边界无法在可维护范围内补齐。触发任何一项先讨论，不因为已做构建而继续投入。
