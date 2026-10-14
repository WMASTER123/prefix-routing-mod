# 原生KV在线目录与双TP1隔离：验收记录

日期2026-10-14，分支codex/online-kv-directory，基于050b7bd。功能提交2a45bd2；接收限额/共用环境1579f41，幂等fence审查与格式修正后的最终功能代码24d78d3。后续文档提交按各自head核验，不自动产生新硬件资格。

## 环境与上阶段收尾

新分配32054：aarch64、2×910B2物理0/7、各65536MiB；CPU cgroup800000/100000（8核）、内存68719476736（64GiB）、共享内存16GiB。逻辑0/1分别指向本次可见设备；没有连接其他项目SSH别名。

沿用项目隔离服务环境：Python3.12.13，HUST Core ced6857afa0ea7b2e3f0846a62e1394e90f15607 / Ascend b47795c63a8e63e7aed4d311f951428d94e045ab；包vllm0.30.0+empty / vllm-ascend0.25.1rc2.dev766+hust.20260903.4.gb47795c63。CANN/torch栈保持原候选；清除SDK注入的PYTHONPATH后pip check通过。未升级全局环境或修改公共模型。

050b7bd的5个检查已核验success：[CPU/源码](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38520440591)、[helper](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38520440429)。服务器由cc2478b同步050b7bd，补上一轮wheel（SHA fc7ecf793a217b77abd2e427a86010860e381b4b45b8ac8deee3978cf2b6377e）的aarch64干净安装；服务仍用原已验安装，不因诊断工具变化替换设备栈。

原Router binary SHA55449ba0dc3f1d93e74576c52dab2eed0d65caeb4ca660f95b0f0042dec8c96d，在两张卡顺序复验普通n2、实际非空流后drop、核心终态、确认前拒绝/确认后准入、丢许可无额外family和重开保债均pass。首次第二卡启动复用18164端口报address in use，检查在启动模型前退出；改独立18165端口后pass，未杀占用端口的其他进程。该失败不是模型/环境不兼容。

## 新增接线与范围

`tools.online_kv_directory`显式启动一个原索引owner；`experiments/router-index/src/online.rs`在同一个Pool/Directory中处理受控cold、live、控制票据、确认、查询和断连。继续编译91b65ee原indexer/types及hash守卫，Cargo.toml/lock和原索引算法不改。

每个订阅任务持有自己的ZMQ socket、真实owned frontend/core绑定；M7原生三帧/重放解码复用adapters.kv。在线事件立即更新常驻索引，M1查询来自同一本地Qwen tokenizer的完整块digest，未通过离线trace重建。去重窗口只保存128个完整规范化批次digest，不持有第二套驻留树；冲突/超窗旧帧/缺口失败撤销目录。Pool限制2分区、各8MiB/总16MiB逻辑注册额度，并保留既有记录/父链/批次/重建预算；这不是allocator RSS保证。

只支持独占受控空起点、私有IPC、TP1/DP1/group0/full_attention/block128/FP16/eager。原生END_SEQ不是完整快照；本地会话ID不冒充后端publisher epoch。目录表示已发布事件的逻辑观测，不提供生产者排队已清空或设备实时强一致保证。已热接管/超窗/任意重启恢复不在本次资格内。

## 实机结果

单卡首轮`online-card-0-01`/`online-card-1-01`均pass：原生2批/6store，449-token输入查询3个完整块；同一服务同时跑实际Router终态实验，普通n2/取消/丢许可语义保持。关闭订阅后索引返回null（unknown）。首轮包含较早monitor实现，最终接收限额改动以下述双副本复验为准。

1579f41的`two-replica-final`同时启动两个Qwen14B TP1，使用同一个常驻原索引owner，两个真实worker/实例及独立订阅：

| 检查 | 结果 |
|---|---|
| 两副本同时存活 | pass；不是TP2或两卡顺序冒充多副本 |
| 仅副本0存入前缀 | 0匹配3块，1仍可信miss，隔离通过 |
| 两端并发生成各自前缀 | 正确完成，实际prompt_tokens449/450 |
| 后端重复复用 | 两副本分别cached_tokens0→384 |
| 断开0的目录 | 0返回unknown，1仍匹配且继续完成生成 |
| 关闭 | 两个backend parent returncode0，无forced group cleanup |

第一次双副本`two-replica-01`亦pass，最终复验覆盖新增的解码前multipart帧数/总字节限制。原索引可执行SHA81e5235191fda43d21ceaf6b4ac836141ca181d9c320c2d6bc0338e64cf69a3f。最终记录字段full_routing_verified/performance_verified保持false：尚未通过公开HTTP入口按M1/M2/M3选择这两个副本。

幂等失效修正后，24d78d3重新通过62项原生测试、5组进程检查和双TP1实机`r2`。最终binary SHA df695e2e670d298ef7910a3a8e4c2778446e20753e2f7f2ae338e63df1828993；`r2/report.json` SHA e43a8cde896b3ac9f073305d6d78a1682a3f878af33ad8b60e64e4a281043ca1。两侧仍实际cached_tokens0→384，一侧订阅退出后另一侧继续服务，两个backend正常退出；随后npu-smi确认两卡均无进程。未运行空载保活任务。

在首版请求链分支`codex/first-runtime-assembly`的0f05ae8上，又于新分配32064（2×910B2，可见物理2/3）重建受源码哈希守卫的原索引并复跑`two-replica-current-20261014a`。索引SHA为`ff0f3999ab0c7bc6307fdaf84648e1d5186347250f93f6f214cfe200c8e6139c`，报告SHA为`afda0c05fe5b7a9d6b0135738378ed1f4433d7837a7cea4a746ad742bc47cbb6`。两个真实TP1副本同时运行；449/450 token请求分别从cached_tokens 0升至384，单边驻留隔离和断开撤销信任通过，两个backend均returncode 0且无forced cleanup，结束后两卡无进程。该复验确认当前服务器和固定HUST数据面仍可用；报告继续保留`full_routing_verified=false`与`performance_verified=false`。

## 审查、测试与制品

新增17项Python反例涵盖去重冲突/超窗、序号/原索引拒绝、真实实例/核心PID变化、另一个终态消费者ACK后只读身份检查、双副本环境与继承hook清理、接收前multipart数量/总字节限制。原生62项测试包含新增4项owner反例，复用原目录与原索引测试；另有真实stdio进程的5组双分区/预算/在线更新/身份/过期/缺口检查。不是独立人工审查完成。

1579f41 Linux aarch64 CPU566 pass/3 skip/3 deselect、integration3通过。Windows最终检查和制品/新CI以本页后续记录为准；初次本地文档链接检查在新证据未落盘时失败，补记录后需完整重跑。未降低断言或把暂时缺文件写成代码环境故障。

最终功能不变的b35ffef：Windows CPU562 pass/7 skip/3 deselect、aarch64 CPU566 pass/3 skip/3 deselect，integration各3，ruff/format与文档链接通过。Windows多4项skip为既有链接限制，另外3项源码执行在专用job显式运行。b35ffef的[CPU/固定源码CI38527391352](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38527391352)四job及[原生索引CI38527391466](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38527391466)全部success。原索引CI明确Python3.12并运行新进程检查；本PR未改HTTP helper，因此不冒称重跑完整Router构建或helper专用CI。

新wheel SHA498179546cb5fb5d2b36e89528cd69800e4bd28a184051cb7345df8f826f795c、sdist SHA4dc6924403087e972d8811903228a9721c6c3cd33263de39267f07d170aa7b55；隔离hatchling1.32.4构建，Windows/aarch64干净安装与惰性/默认关闭/规范描述检查通过，服务环境再次pip check通过。包内runtime、pyproject/锁与050b7bd无差异，本轮新增工具/原索引接线，不更换服务wheel或扩大兼容范围。

审查修正：目录只读检查允许另一终态owner推进ACK，不要求其仍处于replay模式；控制租约以索引owner发起检查的本地时刻计算，一次性nonce隔离迟到确认；完整坏批尾撤销整个视图；multipart限制在构建整条消息前执行；所有自有backend逐项清理，某个索引关闭失败不能跳过其他服务清理。

同步期间把本轮已知SCP暂存文件存入服务器Git stash后再切换规范分支，保留可恢复副本；不覆盖用户改动，不连接未授权目标。最终源码、证据和CI状态另补。

其他失败均保留：一次直接调用rustfmt未设置项目RUSTUP_HOME而找不到默认工具链；随后项目隔离cargo fmt指出新guard格式差异，按建议修正并重过。一次双副本输出目录过长触发既有IPC路径≤100-byte守卫，在模型启动前失败；改用短目录`r2`，未放宽socket限制。初次tar把输出写在被归档目录内，引发目录变化告警；改写到目录外并重新归档，旧试件保留，不把失败archive当最终证据。

最终原始archive：服务器`artifacts/online-kv-evidence-20261014.tar.gz`，本地`artifacts/online-kv-20261014/evidence-verified.tar.gz`，两端SHA **f50b0da120f6c4b64d759db3b3112dfa0fbcfbb6b8ac872b2494566bf92383f9**一致。含各轮结果/日志/原M3数据库及Linux最终检查，排除cache/socket/bundle/wheel/权重；原日志不入Git。代码文档已同步服务器、GitHub和workspace现行入口，后续记录head同步/CI另查。

交付[草稿PR22](https://github.com/WMASTER123/prefix-routing-mod/pull/22)，基于未合并PR21；代码自审/自动检查已做，独立人工审查与合并仍待完成，不自动合并。

## 剩余与计划修正

M2在线子集及当前服务器复验成立；公开HTTP请求链、生产M1/M2/M3本地绑定、MOD完整启停和容量收益尚未成立。stdio/TCP/SQLite FULL仍是资格验证接线，不冻结成生产热路径。

发现必须提前处理的依赖：当前64family/256child及账本/outbox历史不回收，不能直接用于可持续容量测试，否则累计留存会造成假准入瓶颈。下一实现包先装配公开HTTP基础链并完成最小去重/回收和恢复语义，再做正式容量；无需等待M8或所有长期跨节点加固。具体代码归属、冻结清单和强基线见[首版装配](../docs/planning/first-runtime-assembly.md)。三人正式生产分工暂不冻结；接口/fixture/评测准备可独立开展。
