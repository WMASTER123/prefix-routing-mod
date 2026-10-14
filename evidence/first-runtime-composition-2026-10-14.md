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
- 新增 `runtime_provider_probe.rs` 与有界runner，并在固定Router typed公开请求路径上用两个loopback origin通过正命中、基础负载回退、异步准入、真实HTTP转发及终态回调。报告显示cache请求仅到worker B、fallback请求仅到worker A，两个请求均携带Router分配的attempt载体，进程正常退出且无强制组清理；二进制SHA256为`88bc729b875f8ff3a9d2d8cfbe762998f1296e92b800093da0f1807f39f73849`。
- 新增 `request_feature_port.rs` 作为真实M1候选：宿主注入一次分词，接受固定模型、无额外KV身份修饰或后端截断/会话语义的字符串completion prompt，以token小端字节SHA256生成与原M2一致的完整块摘要，尾部不完整块不进入查询；模型/路由/输入形态、cache salt/LoRA/多模态或嵌入修饰、截断/会话参数、字节与token预算、分词失败均显式拒绝，请求ID含显式进程代次且单调不复用。首轮隔离Rust检查增至30项通过；最终新增截断/会话与可打印身份守卫后服务器失联，须恢复后重跑同套检查。固定Qwen tokenizer宿主绑定及实机token一致性仍待验。

首次远端检查因命令PATH未保留 `/usr/bin` 而找不到`cc`；纠正为显式系统PATH后继续。第一次完整宿主检查误把OpenSSL前缀写成`build-sysroot`，实际完整前缀为`build-sysroot/usr`。后续release链接发现该隔离OpenSSL静态库依赖zlib，而镜像只有`/lib64/libz.so.1`、没有开发包提供的`libz.so`链接名；仅对此目标传入确切共享库路径后构建通过，未安装系统包、未修改全局Rust/OpenSSL或设备环境。

探针首次运行还发现候选worker集合的顺序不构成身份契约：固定Router交给端口的顺序可不同于启动参数顺序。夹具曾按数组下标绑定cache/base身份，导致两次选点反转并由后端计数断言捕获；修正为按稳定URL身份验证集合及构造候选后通过。生产M2必须同样按显式worker/instance身份连接目录记录，禁止依赖容器遍历或注册顺序。

本地隔离sdist/wheel首次构建在临时环境获取hatchling时遇到PyPI TLS EOF；第二次已成功构建sdist，但从sdist创建wheel的新隔离环境再次在相同TLS阶段失败；无隔离重试因当前venv未安装hatchling而明确失败。旧制品没有被覆盖，不能以网络错误判断包代码不兼容。草稿PR23随后在GitHub隔离环境完成Python3.11/3.12的sdist、wheel与干净安装验证；Rust helper及两组HUST源码方法检查也通过，共五项CI成功。

## 尚未证明

真实FeaturePort核心逻辑已有候选，但固定Qwen tokenizer适配、同进程M2 SnapshotPort和生产M3 owner尚未装配；已通过的宿主探针只证明fixture端口的调用和转发，不等于公开HTTP已经消费在线目录做双副本选择。随后另行完成的新服务器双TP1复验记录在在线目录证据中，不改变本证据的公开链范围。本阶段没有运行容量测试或冻结ABI。终态消费者游标持久化和M3重启恢复仍是接口冻结门槛，当前回收原语不能单独解决进程崩溃后的债务恢复。
