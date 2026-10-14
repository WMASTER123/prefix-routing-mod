# 首版请求链组合与最小回收：CPU证据

日期：2026-10-14。分支 `codex/first-runtime-assembly`，基于在线目录最终记录 `87f0729`。本记录只覆盖本次候选接口与回收原语，不生成NPU性能或公开HTTP端到端资格。

## 实现与来源

- `runtime_provider.rs` 实现固定Router内的M1、M2/M3快照、基础M4和异步M3准入组合；不创建代理、索引或网络服务。固定Router仍为 `91b65ee`。
- HUST后端范围保持 Core `ced6857` / Ascend `b47795c`。本轮没有修改、构建或替换HUST引擎，也没有使用官方vLLM接口文档扩大支持范围。
- `online_bridge.rs` 在完整回调得到绑定ACK后移除已交付slot；未确认slot和断连记录继续保留并关闭新准入。
- `TerminalJournal` 只允许owner生成的不可复用attempt/child在“源ACK边界=消费者持久游标”时回收。任意外部ID禁止回收，恢复时丢失持久游标明确失败。

## 检查结果

- Windows完整Python回归：569项通过、7项按平台条件跳过；其中terminal journal目标回归33项通过，owner身份回收以容量1连续运行300轮。
- aarch64隔离Rust检查crate：格式检查通过，27项通过；新增覆盖正命中/基础负载回退、unknown与可信miss、不一致/越界快照、外部worker、异步准入及ACK后slot复用。
- 更新后的三文件Router patch可在91b65ee干净源码上 `git apply --check --whitespace=error`。
- 将 `prefix_extension.rs` 与 `runtime_provider.rs` 装入固定Router源码后，aarch64 `cargo check --locked --offline`通过。仅保留原源码已有的Atomic方法deprecated warning。
- 新增 `runtime_provider_probe.rs` 与有界runner，目标是在固定Router typed公开请求路径上用两个loopback origin验证正命中、基础负载回退、异步准入、真实HTTP转发及终态回调。代码已完成本地静态/格式审查；远端release构建在依赖编译后段因服务器端口被回收而中断，未产生可执行文件，因此本项仍为待验，不能计入通过项。

首次远端检查因命令PATH未保留 `/usr/bin` 而找不到`cc`；纠正为显式系统PATH后继续。第一次完整宿主检查误把OpenSSL前缀写成`build-sysroot`，实际完整前缀为`build-sysroot/usr`；旧的完整`cargo check`已用正确前缀通过。本次release example在改正前缀后继续编译，但服务器随后拒绝SSH，结果记为中断而非代码失败。未安装系统包、未修改全局Rust/OpenSSL或设备环境。

本地隔离sdist/wheel首次构建在临时环境获取hatchling时遇到PyPI TLS EOF；无隔离重试因当前venv未安装hatchling而明确失败。旧制品没有被覆盖，本项留给CI/恢复后的隔离环境重跑，不能以网络错误判断包代码不兼容。

## 尚未证明

真实FeaturePort、同进程M2 SnapshotPort、生产M3 owner尚未装配；宿主探针即使补跑通过也只证明fixture端口的调用和转发，不等于公开HTTP已经消费在线目录做双副本选择。本次没有启动模型、运行容量测试或冻结ABI。终态消费者游标持久化和M3重启恢复仍是接口冻结门槛，当前回收原语不能单独解决进程崩溃后的债务恢复。
