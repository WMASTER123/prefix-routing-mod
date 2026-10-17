# 实际 Router 库→HUST→同一账本：组合验收

日期2026-10-14，Asia/Shanghai；原日志UTC。分支codex/router-hust-terminal-chain，基于375a047；最终功能代码 **cc2478bc69aab0cd6068d9d9755a8fe3949df8b7**。后续文档提交不改变本次NPU资格。[执行边界](../docs/planning/router-hust-terminal-chain.md)和[ADR](../docs/decisions/2026-10-14-router-live-chain.md)先于资格结论。

## 运行环境与实际复用

32068，aarch64、8核CPU quota、64GiB RAM、2×910B2物理5/6；本轮逻辑0→物理5。公共Qwen2.5-14B-Instruct，原FP16/eager/TP1/DP1配置，max-model-len1024、max-num-seqs2、70%HBM、APC/chunked-prefill、block_size128/sha256。不重新安装引擎/CANN/torch或修改公共模型。

服务环境继续HUST Core ced6857afa0ea7b2e3f0846a62e1394e90f15607（0.30.0+empty）/ Ascend b47795c63a8e63e7aed4d311f951428d94e045ab（0.25.1rc2.dev766+hust.20260903.4.gb47795c63）。服务沿用上一轮已验MOD wheel；本轮仅tools、接收器接缝、example和CI改变，没有新增HUST包装/设备同步/源码fork。

Router固定91b65ee4bdfbe98863a32a2205dced5dbb249ffe、vllm_router_rs0.1.16。原归档SHA eba53db252b19decc7d239a4b2f8c3b0e354db899793555cb1dad05f3c71156f；全新解包重放原三文件patch后，registry/router/mod与上一已审宿主逐字节一致。原Cargo.toml SHA 082883aba29f8ac912596325e2114a0e33d001bd23579084b05bd3c87faa1735，Cargo.lock SHA 8675caa4ef6227d0f2cb273ac921e9dd0a6989cdad304b5600887a9795355156，构建前后未改锁。仍用项目私有Rust1.99/OpenSSL SDK，不改全局。

新example复用原 typed HTTP 转发、AdmissionClient/Outbox/LifecycleTrace/Controller；Python supervisor持有同一个DurableLedger。AdmissionReceiver只增加显式实例/域及准入前检查，原默认fixture回归不变。不是新的proxy/index或生产远程账本，也不是已经装配外部HTTP入口的Router产品。

## 构建与实机结果

新宿主release全量构建9m10s；增加明确响应结果断言后example增量构建2m50s，两个build log均保留。上游deprecated fetch_update及复用helper未用方法警告未被当作错误或偷偷修改上游。编译时间不是请求或性能成绩。

实际binary SHA256 **55449ba0dc3f1d93e74576c52dab2eed0d65caeb4ca660f95b0f0042dec8c96d**。server artifacts/router-live-chain-20261014/npu-01，report/子报告均pass。

真实PID：Router204196、HUST frontend200461、core202127，Python owner为独立实验进程。instance=641ce1fb41a54db789db7b997e78c88d，observer=a06a1423267647cf90fb37a4ad2a9c92。

| 顺序 | 实际结果 |
|---|---|
| 普通completion n2 | owned attempt prm-d151fdbff439-1，2/2/2 child，核心LENGTH_CAPPED×2 |
| HTTP完成、未消费核心证据 | 下一个请求503，原M3仍占1；两个客户端X-Request-Id被自有载体替换，真实family与预留对应 |
| 核心入账后继续 | 原M3结清；下个流式请求成功，不依赖手工合成ENDED或清空账本 |
| 首个非空生成输出后主动drop | attempt prm-d151fdbff439-3，1/1/1 child，实际核心ABORTED；只靠drop仍占1，后续准入503 |
| 终态入账与重投 | 通用Ended与业务取消结果分别组合，首次applied、再次duplicate，无重复释放 |
| 准入提交后丢ACK | Rust未取得许可，两个尝试均503；HUST仅有上述两个受管family，无额外受管请求 |
| 进程结束后重开账本 | 2条终态/9次持久操作，另1笔预留保留，admission_ready=false；没有自动续派发 |

故意保留的1笔预留属于“提交成功但Router没拿到许可”的未知状态，不是NPU仍有请求运行，也未宣称自动回收已解决。实际Router/core/front均正常退出，backend parent returncode0、无强制group cleanup，最终两张NPU均无进程。

原生先验仍通过：449 completion/478 chat输入，重复cached0→384，2 live/2 replay batches、6 stored blocks规范化一致。该采集未进入在线M2。顶层routing_verified/full_recovery/performance仍false；新增router_terminal_chain_verified仅指本页有限库路径。原4case终态harness本轮被组合case替换，不用其旧成绩假装本轮重新执行。

## 回归和审查

功能代码cc2478b：Windows CPU **545 pass/7 skip/3 deselect**，aarch64 CPU **549 pass/3 skip/3 deselect**，integration各3；ruff/format通过。7skip仍含4项Windows链接限制和专门源码job另执行的3项。新增11项CPU反例/组合：真实worker映射、先占额度、健康拒绝、身份/核心PID/序号/坏批尾/关闭状态、业务结果缺失、终态早于传输等待、晚到close、丢许可重开保债。

Python3.11/3.12 [CPU CI38513607903](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38513607903)四job全部success；专用源码也执行固定HUST方法和26/26 checks。[共享helper CI38513607926](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38513607926)success，另校验新example格式，不冒称该job完整编译Router。真实完整release是上述aarch64手动证据。

新制品经隔离hatchling1.32.4构建，Windows与Linux x86_64 CI干净安装通过；新wheel的aarch64干净安装未执行，见下方服务器同步状态。初次错误选--no-isolation且开发venv未装hatchling失败，随后按原隔离构建流程成功；不往服务/全局环境补依赖。Windows wheel SHA fc7ecf793a217b77abd2e427a86010860e381b4b45b8ac8deee3978cf2b6377e，sdist SHA 9892a70033e1fe70ee3796046c808d5c48bf2006c513c1127c0840e1f9c21fa3；不混同实际后端沿用的旧MOD wheel。

自审增加完整批次family校验后才应用、迟到close反例、有界脱敏stdout、真实非空流输出/正常finish_reason检查、最终后端family集合检查、异常后准入fence。未扩兼容、生产schema或性能声明；另一成员独立人工审查/合并待完成。

## 未完成与下一步

当前通过的是一个实际Router库到真实HUST的有限控制实验，不是生产外部HTTP宿主/一体M3装配。没有在线KV目录、tokenizer精确路由、双副本容量/配对开销、GC、图、全链任意崩溃恢复或MOD Manager启停资格。源故障CPU测试不冒称NPU网络故障已执行。有限64 family/256 child、SQLite FULL和控制RPC不是生产性能方案。停服日志与上游可选算子警告保留，未因exit0宣布完整G3通过。

下一步按批准顺序接M7→原M2索引，先受控空起点/连续覆盖/控制新鲜度与全池预算；已热/超窗恢复仍须新版共同边界。再用现有两卡开展2×TP1强基线与分层开销，不加新算法或额外卡。生产HTTP装配和长期恢复/GC随真实接缝收敛，不提前冻结ABI。

原始archive双端SHA **6fbdef99e26fca5336bc052eee0d96b7aae151630fb1589ec81bfef3446e9f4f**，server artifacts/router-live-chain-20261014/evidence-01.tar.gz，本机同相对路径。包含npu-01报告/脱敏Router输出/backend日志/SQLite、两次build log和Linux最终checks，排除cache/socket/权重/整个源码树。report.json SHA fa4c85ffdc4f93d8ee26b0f15cf822720d4572007c184ee89beb560e9f212dcd；router-chain-report.json SHA 1e688d926f63d1bea731df854b6378e8373642bb2032c250dd3abf300646a241。交付[草稿PR21](https://github.com/WMASTER123/prefix-routing-mod/pull/21)，基于未合并PR20；最终记录head的CI另核验。

## 收尾与未完成同步

功能代码cc2478b已在服务器运行；0ea2615及后续本轮记录仅文档/说明修正，未产生新硬件资格。0ea261577e8d76fa4f00b9813e4c2aa061827846的[CPU/源码CI38514511969](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38514511969)与[helper CI38514511920](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38514511920)全部success，后续记录提交以对应head另查。

原始archive拷回并核SHA后，32068在最后同步时拒绝SSH，两次读取均未能进入；原因未知，不能擅自判断资源回收。故服务器目前最后确认功能head为cc2478b，**最终文档同步、新wheel拷贝/aarch64干净安装和本轮结束时再次pip check未执行**，不写成三端全同步。原模型/Router退出及NPU无进程是在失联前已核验的事实。未连接其他目标、扩大凭据或运行保活负载。

本地/GitHub保留全部代码、活文档和校验一致的实机archive，workspace三份规划同步当前范围。待有效分配恢复后，先确认原目录/dirty状态/版本，再ff同步和补缺少检查；不因为SSH失败就换一套引擎。正式双副本容量的业务SLO数值仍待用户确认，多阈值/长度分组方案已提出，不据此擅自选有利阈值。
