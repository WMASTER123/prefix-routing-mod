# 首版运行时装配与接口冻结清单

2026-10-16。在线目录子集通过后的下一实现包。固定Router内候选组合层、同进程M3 owner以及原M2索引适配已实现并通过CPU组合：typed公开入口、M2命中/可信miss、基础选点、准入、真实loopback HTTP转发和可靠终态位于同一进程；实例替换、断连失效和全池配额回收也已覆盖。M1与归一化KV事件在该组合探针中仍为fixture，真实HUST subscriber、Qwen M1和终态尚未同时装入同一公开链。见[ADR](../decisions/2026-10-14-first-runtime-composition.md)、[本次证据](../../evidence/runtime-cache-owner-composition-2026-10-16.md)、[早期组合证据](../../evidence/first-runtime-composition-2026-10-14.md)与[在线目录证据](../../evidence/online-kv-directory-2026-10-14.md)。生产ABI仍未冻结。

## 运行形态与代码归属

采用一个外部Router宿主，优先在Rust侧组合原HTTP Router、原索引、状态快照与选点。现有Python包继续承担MOD描述/doctor、版本映射和必要的引擎侧可选适配；M6测试/校准在请求链外。用户通过预构建制品启动，不能要求最终用户自行编译完整Router。

| 职责 | 已存在的依据 | 首版归属与接口要求 |
|---|---|---|
| M1 | contracts.RequestFeatures、有界completion FeaturePort；固定Router/HUST的Qwen14B token及完整块摘要对照已通过 | 本地特征端口；一次解析/分词，完整身份与精度；当前只接受字符串completion prompt，其他形态明确拒绝。公开链安装/开销及chat/template仍待验 |
| M2 | 原KvBlockIndexer、Directory/Pool、online原型及InProcessCache适配 | Router内一个索引owner；异步更新，短只读查询；以worker URL、instance epoch、publisher/KV identity围栏，同URL换实例必须重新恢复；不把stdio进程作为生产服务 |
| M3 | ReferenceLedger、DurableLedger、真实终态闭环及RuntimeOwner | 一个状态/账本owner；AdmissionPort异步许可；HTTP关闭不清债，可靠终态一次释放；checkpoint/新进程保债语义已有，仍须确定持久介质、提交边界和启动对账 |
| M4 | PolicyPort、原Router基础策略 | 候选实现只读选择：正可信命中最大优先，否则最低在途/稳定URL；绑定实例/配置版本，优化算法后置 |
| M5 | 固定Router91b65ee、原三文件patch、实际typed HTTP例程 | 使用原build_app/HTTP入口和SSE/连接管理；在同一入口装配provider，禁止再包一个Python推理代理 |
| M7 | 固定profile、原生解码、已有HUST endpoint/utility终态观测 | 版本差异集中映射/窄幅适配；订阅和终态投递有各自连接/代次/留存；Manager生命周期另验 |
| M6 | CPU/源码/原生index/手动NPU/制品入口 | 公共脚本、受控配置与机器可读报告；测试替身复用现有fixtures，不各造一套字段 |

上表选定职责所在实现层。`runtime_provider.rs`、`runtime_owner.rs`和`runtime_cache_port.rs`已给出候选类型，但固定宿主探针通过不等于生产目录/ABI冻结；真实M7/M1/终态合并及故障测试后再记录最终位置。原宿主若需追加async特征端口等窄幅变化，要列出与原三文件patch相比的实际差异、默认关闭和兼容回归；出现大面积fork或引擎调度修改则先讨论。

## 必须真实接通的请求与控制路径

1. 原HTTP入口解析有界请求；M1输出真实模型/模板/tokenizer身份与完整块特征。不从诊断工具生成的attempt标识冒充特征。
2. M4读取本地M2/M3快照，按健康/隔离/排空筛选，再选点；M2 unknown可回基础策略，M3不合法不能绕过准入。
3. M3原子复核实例、控制/配置版本和额度；成功提交且M5取得本次许可后才发送。异步等待取消或丢确认保留未知，不由后台任务偷偷发送。
4. M5将原宿主的真实发送/关闭事件交给同一owner；完整family和核心结束与业务结果分别组合。HTTP EOF/断开不能清债。
5. M7后台处理真实KV事件、源身份与控制租约；错误失效对应M2视图，不清M3。恢复不能在请求链阻塞。
6. 先停准入、排空/记录未决、停订阅与控制任务、退出自有宿主，最后按生命周期约定关闭可选观测；启动/关闭失败均留可诊断状态。

## 需要修正的性能前置依赖

早期实验账本、Outbox、前端family和core seen有“有界但不回收”的限制（如64 family/256 child），不能直接做可持续容量曲线。候选RuntimeOwner改为有界active与有限retired墓碑：可靠终态回收active，未决请求保留；恢复时所有旧active转为uncertain。该候选仍须配套生产checkpoint介质和超出墓碑窗口后的保守语义，不能仅凭CPU探针宣布长期可持续。

因此在正式容量前须完成与实际运行时匹配的最小留存/回收协议。当前已经做到：完整ACK后回收outbox槽位；owner分配不可复用attempt；RuntimeOwner只由可靠终态回收active，重复终态不重复释放，checkpoint跨进程保留负债。仍缺实际持久游标、checkpoint/WAL选择、墓碑超窗处理和真实Router重启恢复。不能只增大常量或按TTL清除未知负债。长期跨节点/多版本加固仍可后置。

生产M3存储先以实际调用频率/持久化需求和配对开销决定。实验逐请求FULL事务已有毫秒级成本线索，优先评估本地owner/有界异步提交与必要批处理；未验证前不承诺热路径开销，也不牺牲发出前许可和重启保债语义。

## 可以冻结和分工的通过条件

- 提供一条可复现的公开HTTP启动命令、两个真实TP1配置、合法支持范围和默认关闭/原版回退。
- 明确每个端口的实际代码位置、类型、调用线程/并发规则、时钟、错误/超时/降级；正反例与替身随接口同一提交。
- 全链正常、重复、取消、断连、重启/配置竞争和预算故障通过；真实M1/M2进入选点，所有发送经过同一M3。
- 最小历史回收不会把未决工作删掉，重复投递不会重复释放；给出超过当前64/256阈值的持续请求证据与内存趋势。
- 原版/加接线基础模式完成开销诊断，排除逐请求远程查询或重复分词等明显回归。

随后按A(M7/M2)、B(M1/M4/M5)、C(M3/M6/交付)划分可独立代码范围，共享契约由集成负责人维护。每包自测、交叉审查；不要求M8或最终优化方法已经确定。本轮不提前指定人员。

## 强基线和资源

现有2张910B2足够：单后端1张、双TP1同时2张。8核/64GiB Host下先证明两个服务与Router/压测可运行，再做合理配置校准；当前1024上下文/max-seqs2/eager只用于功能放大，不能直接充当“调优充分”的主性能基线。

正式比较至少覆盖直连池、原宿主RR/P2C或实际支持的负载策略、原cache-aware强者、同底座仅加接线和最终增量策略。固定后端配置/公平调优，分别记录冷暖、输入/输出预算、到达过程和随机种子；多阈值/分组在基础校准后预冻结，保留失败分母。SSE chunk间隔与逐token停顿分开；短样本诊断不能写成稳定P99或可持续容量。
