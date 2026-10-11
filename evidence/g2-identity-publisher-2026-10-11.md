# G2：纯文本身份与可选 publisher 恢复实验

日期：2026-10-11。结果只覆盖下述子集；完整 G2、生产 Router、Manager 激活和性能提升均未通过。用户已批准先实验可选 hook，未批准默认交付启用。此前原生生命周期/恢复证据仍独立有效，不被本轮结果覆盖。

## 来源、代码和制品

- 实验分支 `codex/g2-identity-adaptation`，基于 e070e4e；身份/共同边界实现 ab2b190，恢复来源审计 aaa7659，hook 实现 88e5a3b，回归边界 58392b3，重启连接修正 8a461fd。后续文档提交不改变该 hook 实现。
- core `13f11da44455d2f85f00f810a04e1bd2eaf30ba6`；Ascend `3a61053d8b4133eae7c468c1567648048750be21`。恢复目录的本地 clone origin 逐级追溯到指定 GitHub 仓库，完整历史/固定提交/工作区清洁逐级检查。严格原始来源校验函数未放宽。
- 守卫要求 core metadata `0.23.1.post1.dev733+g13f11da44.empty`、Ascend distribution `vllm-ascend-hust` metadata `0.19.1.post1.dev563+g3a61053d`，native publisher SHA-256 `9eca5310ada1d90c4740c1cec06fbdb18aff65fc9d2abb536098fcbd0ddf7d14`。
- hook01/02 使用 wheel SHA-256 `29315aeb74ba54b7d22236b0df633c72fd4952efe5f7508ddddb0845a8009ea3`。后续交付检查 wheel 为 `1cf9f2cf3ed4ee9c80773b512ad1be58af1fae0da446dde12bda1a24afe98a48`，重新构建 sdist/wheel 和干净安装通过；二者 hook 模块字节一致，SHA-256 `7eab8ac96ec6a17fe53908f7acdee396af35d9ec0c1fba8f061a5501b2e35d61`。后者含描述类注释修订，不能混同两份完整 wheel hash。
- 模型为只读公共 Qwen2.5-14B-Instruct，BF16/TP=1/eager。config SHA-256 `0f2085dbbe2ee251bd6a6a0797d84a6ce34436044d629aa3cba793b43d311a9e`；tokenizer.json `c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539`；tokenizer_config.json `5b5d4f65d0acd3b2d56a35b56d374a36cbc1c8fa5cf3b3febbbfabf22f359583`。模型 Hub revision 未取得，不伪造。

新分配仍是 aarch64/4×910B2/64 GiB HBM、16 核 CPU 时间配额、128 GiB RAM；物理枚举 0/1/2/7。本轮主回归使用逻辑 0，不把旧分配四卡成绩算作这台四卡复验。保留 CANN 9.1.0、torch/torch_npu 2.10.0、NumPy 1.26.4 的项目隔离 HUST 配方；Transformers 实际为 5.5.4。原预装栈、两份引擎源码、驱动、公共模型均未修改。

## 测试与结果

真实入口为 `tools/regress_kv_identity.py`，操作命令见 [运维](../docs/operations.md)。使用同机私有 IPC、loopback HTTP、1024 context、1 GiB KV 和 4 批重放窗口，复用项目私有 bytecode cache；这是事件放大配置，不是性能基线。每次输出目录独立，已有报告不覆盖。

| 记录 | 结果和范围 |
|---|---|
| 新服务器 inventory/dependencies | 只读清单及 HUST Python 依赖审计 pass；不是完整 G1 重建复测 |
| common-cut.json | 固定 native 镜像/重放方法的 CPU 原型 5 项 pass，未安装 hook、未运行 NPU；队列未提交变化不进快照 |
| id01 | fail；真实 3 块/384 tokens 对应正确，但测试模板返回类型及 native 整数 hash 验收假设有误，见下节 |
| id02 | pass；completion 分词、chat template tokens、真实完整块对应 3 项通过；484 输入 tokens，block size 128，3 个完整存储块；重复文本无新增存储事件 |
| hook01 | fail；已热/超窗/增量/clear/epoch 子项通过，重启后的旧 SUB 未收到新事件，保留原失败 |
| hook02 | pass；上述子项及同地址真实重启、新 epoch 实时消息通过；两次服务均正常退出，未强制清理进程组 |
| id03-disabled | pass；安装最终检查 wheel 后，关闭 opt-in 并移除 hook allowlist，重新启动原生服务；三帧事件、分词/模板/3 个完整块对应通过，服务正常退出 |

hook02 已热快照：排他 next_seq=7、21 个驻留块、21 个元数据块、2688 tokens、10842 bytes。恢复 oracle 在暂存状态中完整应用快照/增量，比较同一最终水位的驻留与元数据；不向生产流量开放命中。保留的真实旧 wire frame 在解析/应用前按新 epoch 拒绝，不是合成注入；这是测试接收器验证，不是生产消费者实现或真实网络主动延迟注入。

CPU：8a461fd 对应 Windows 183 passed/2 skipped、mock 3；服务器在 58392b3 对应 Linux aarch64 185 passed、mock 3。最终文档提交后另跑全套检查，记录在 PR/最终状态；不能把此前数字替代最后提交的 CI。干净 wheel 验证默认 general plugin 不导入 vllm/torch、manifest 仍 import_only、doctor 不宣称 routing_ready。

## 失败定位与修正

1. 恢复的 editable 源码 origin 是项目内原始 clone 路径，并非身份不符。新增严格 provenance 链核查：只允许项目私有 artifacts 下的干净固定提交，限制深度/循环，末端必须为指定 canonical origin；不改 origin、不接受任意来源。
2. Transformers 5.5.4 的 chat template 默认返回 BatchEncoding，而非 token list；测试显式 `return_dict=False` 后与服务端一致，未换依赖版本或模板。
3. native 默认事件 hash 是整数不透明锚点，无法用它重算原生 bytes 父链。固定候选 token 索引用本地 SHA-256，不要求统一随机首块 seed。本次报告保留 `native_chained_hashes_validated=false`，不静默调整引擎 hash 或 seed。
4. reset 的 HTTP 200 可以没有 JSON body，且清空事件在后续调度发布；回归按 HTTP 状态和真实 AllBlocksCleared 分别验证。快照/增量只在共同水位比较，不能靠短暂空闲证明发布完成。
5. hook01 重启后推理 HTTP 200、控制面新 epoch 正常，但旧 IPC SUB 没收到新消息；无法据此宣布 hook 恢复通过。改为撤销旧暂存目录、关闭旧 SUB、重建新 SUB 并握手新已知起点；hook02 完整复测 pass。
6. 本地隔离构建曾因 PyPI SSL EOF 失败；保留失败，使用已有进程级代理重试后成功。未改 TLS 安全设置、全局 pip 或版本约束。

## 复用判断与计划变更

同一固定 [vLLM Router 91b65ee](https://github.com/vllm-project/router/tree/91b65ee4bdfbe98863a32a2205dced5dbb249ffe) 已本地读取并检索，未编译/运行 Rust：

- token 索引按每块 u32 little-endian token 字节计算 SHA-256，engine hash 是 parent/removal 锚点；不必为接线修改 xxhash/首块 seed。
- native tokenless 快照不能重建该索引；legacy DEALER 三帧与候选至少四帧回复假设不兼容。
- 索引 supervisor/query 在模块以外在线链路未被调用；已有 cache-aware 策略不是事件精确驻留策略。需补 M2→M4 接线与请求 token 合同。
- 缺父链回落根节点及 anchor 未回收不能直接继承为精确模式的正确行为；须暂存/拒绝和有界 GC。
- Cargo 的 vLLM v0.29.0 CPU 库依赖不等于要求升级 Python 引擎；还需评估 aarch64、Wasmtime/Kubernetes 等依赖与 Host 成本。候选仍未冻结，不引入第二套代理/索引。

接下来先验证 hook 自然淘汰/祖先依赖、交错发布与恢复、断连超窗、限额故障与清空恢复；测队列/元数据/RSS、快照耗时及开关对照。条件/预算在运行前固定，不以功能 pass 推导低开销。随后 G3 目标图/并发/取消/持续运行，再 G4 两个 TP=1 副本。当前 1 张卡足够下一轮 G2，G4 用 2 张，现有 4 张无需再申请。

## 自检与未完成边界

代码自检检查了默认关闭、双重 opt-in、固定版本/文件守卫、重复加载、owner 提交顺序、PUB 失败保留重放、编码/元数据失败撤销资格、上限、ghost 祖先不误报驻留、socket/自有进程组清理及原生回退。原生引擎工作区保持清洁，无请求热路径设备同步/轮询/profiler。另一成员独立审查仍待 PR，不自动合并。

hook 约 330 行，通过内部 zmq 工厂绑定/继承实现；native 文件指纹变化即拒绝启用，升级须复审，不是跨版本稳定 API。验证范围为 TP=1、DP rank 0、纯文本/group 0/完整块/IPC/eager；非零 DP、LoRA/extra keys/部分块有拒绝/降级，其他 TP、模型和图配置未验收。默认元数据/重放 payload 限额不等同峰值 RSS 或有效工程开销已测。

尚未完成：hook 压力/自然淘汰真机重验、恢复竞态/生产 trust 状态机、过载与关闭边界、Host 开销、目标图、跨节点 TCP、legacy hook 恢复、Rust 构建与在线索引接线、策略/性能和完整 MOD。M1–M8 仍为部分基础工作，不能按工具文件数量宣称组件完成。

原始私有报告位于服务器项目 `artifacts/g2-identity-20261011/`；不提交 token-bearing payload、prompt/输出或完整日志。最终工作树的 Windows CPU 183 passed/2 skipped、mock 3 与文档链接/围栏/差异检查通过。关闭回退 id03-disabled 已真实复验；最后提交的远端 CPU 与 GitHub CI 在同步/PR 后另补，未运行的项目不自动记 pass。
