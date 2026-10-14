# cedb477 入口、终态与同一账本的 CPU 接线

日期：2026-10-14（Asia/Shanghai）。基于 8997ae8，分支 codex/cedb477-terminal-boundaries；代码 e9f3a5b92a61c7eb1ef7340ecc8ab4cfa4779cc5。本轮服务器 root@36.140.239.91:32099 两次均在 SSH 握手前连接失败，未登录、同步或运行 NPU。用户已被告知，并已请求可用分配；未访问其他 SSH target。

## 改动与范围

复用原 audit_backend_terminal/audit_http_entry、EntryCapture、TerminalJournal、OnlineConsumer 与 DurableLedger；抽取共用 scheduler/output fixture，没有新建代理、索引、生产账本或安装引擎 hook。新增源码 audit profile 仅供 tools 执行，不改变运行 profile/MOD 能力声明。

十个完整 HUST 源文件先校验 SHA，再抽取完整方法。新版适配 BaseServing 路径、准入调用、异步输入处理、EC 输出、Request 对象返回与清理辅助方法；旧 hash 守卫和旧默认 profile 保留。显式下载工具及独立 CI job 不导入引擎、不用卡；网络/hash/大小失败均失败并保存报告。

## 执行证据

| 项目 | 结果与范围 |
|---|---|
| 新版终态方法 | 26 checks 全通过；普通取消缺确认、stop string 映射早删、清理失败不发布、逻辑结束/blocks 分离；新增输出任务取消转 EngineDeadError 的 CPU 复现 |
| 新版入口→账本组合 | 26 checks 全通过；完整 family、multi-prompt×n、流式入口、部分提交失败/取消、第二组后端准入拒绝、清理失败、stop string、core abort、ACK 丢失/恢复幂等 |
| 旧固定源码 | 原归档终态 25 checks、扩展后的入口账本 25 checks 全通过；不表示旧环境新实机复验 |
| Windows Python3.12 CPU | 499 passed、7 skipped、3 deselected；4 项既有跳过，3 项固定源码测试由专用 gate 显式运行 |
| 显式固定源码反例 | 3 passed；正向组合、意外异常必须传播、去掉 family seal 必须使 gate 失败 |
| integration | 3 passed；仍是原 loopback 合成后端 |
| 格式/静态/差异 | ruff check/format、git diff --check 通过 |
| 制品 | 离线 sdist/wheel 构建、源码外干净安装、默认关闭及包资源检查通过；运行 wheel 字节与上一轮相同，因为本轮只改 tools/tests/CI |
| Linux x86_64 CI | Python3.11/3.12各CPU503 passed、3 skipped，integration各3；构建/干净安装均通过 |
| 专用源码 CI | Python3.11/3.12下载/校验十文件、3项显式测试、终态26 checks与入口26 checks全部通过；本地TLS故障未在CI复现 |
| aarch64/NPU | 本轮未运行；服务器不可连接，不继承上一轮成绩 |

26 个入口 checks 来自单进程调用完整源码方法；IPC、真实 detokenizer、业务结果、健康与时钟都为替身。正常完成从 core cleanup 入口开始，不包含真实 scheduler step/model output。consumer ACK 丢失是调用级注入，不冒充跨进程网络故障。当前仍缺新版真实 HTTP/终态跨进程链路、在线 KV 目录和性能证据。

| 文件 | SHA256 |
|---|---|
| terminal-reviewed.json | 0826355f1d3bfb4747611c513d39ff9e6bbe774ed3a8cf2b499be501ca576c90 |
| entry-ledger-reviewed.json | 56a371fe0ec63b0a8f8553812d0408face4ccbe2f96af592cd1b5ac5db2d04a8 |
| wheel | 7e392fc6cc637353fa7576e6ac2a0cd81bbe594958fc7db76865b2172d092f29 |
| sdist | ce7275d25e897e67feb548299253b96fd4b81a0a49921821ca4e02d9ea4ca19b |

原始文件在本项目 artifacts/cedb477-terminal-20261014；报告和失败不覆盖。文档收尾若重建制品，另记 hash，不改写上述代码制品。

草稿[PR19](https://github.com/WMASTER123/prefix-routing-mod/pull/19)已建立并附任务，基于未合并PR18；[CI run38503332456](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38503332456)的head已核实为3517c745de2b7773dd5a5bed1a74b7b913dc6641，四个job全部success。后续记录提交以其head另核验，不自动继承成绩。源码job的3.12产物已下载到本机artifacts/cedb477-terminal-20261014/ci-py312。

本地主要报告/两个TLS失败/最终检查已归档为artifacts/cedb477-terminal-evidence-20261014.tar.gz，SHA256为59c846655c4fec15b988d0ac97e39207d4ca7d3dab6ae840701f6d08a5f52c59。该归档未包含模型/提示词/凭据，不在Git中。服务器同步待可用分配。

## 失败、自审与判断

- 初始源码下载命令的 URL 拼接失败和旧路径 404 已纠正，按固定 HUST 实际文件路径取源；无源码版本替换。
- 首轮复用 fixture 缺新版 _request_blocks_can_be_freed；补充提取原完整方法。账本 fixture 的 local_outstanding 来源不符合同而失败，改为 local_ledger/router_session，未放宽合同。
- 本机 urllib 通过代理下载时两次遇到 TLS EOF；第二次有限重试后仍失败，fetch/fetch-reviewed 的失败报告保留。curl 取得的十文件经同一 SHA 守卫后执行通过。未关闭 TLS；新 CI 下载路径需按实际运行判断。
- 干净 wheel 中 pip 更新检查发生 TLS 提示；本地 wheel 安装与全部离线探测 exit0。
- 自审补充重复方法定义拒绝，避免提取时静默覆盖；负向测试确保缺 seal 和意外异常不能得到 pass。预期故障必须实际触发且异常来源对应。
- 完整方法复现的取消→EngineDeadError 路径与上一轮停服日志时序相符；尚未验证完整停服因果或资源无泄漏。保留当前可用隔离环境，先现场验证，暂不换依赖组合或吞异常。
- 代码自审完成；另一成员独立审查待完成，不自动合并。

下一步与候选安装范围见[现场接线计划](../docs/planning/cedb477-terminal-boundaries.md)。先补单后端真实关联/终态和启停，再在线 KV；受控闭环成立即进行双副本强基线与开销测量。MOD 仍 import_only，生产 ABI、长期 GC 和发布尚未验收。
