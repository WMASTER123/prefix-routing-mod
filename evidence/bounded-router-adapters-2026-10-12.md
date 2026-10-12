# 有界目录与流式适配：CPU 实验记录

日期：2026-10-12。用户批准继续当前分析并要求审查；本轮服务器为新 CPU-only 分配，aarch64、16 核配额、64 GiB RAM、0 NPU。只操作 `/root/work/prefix-routing-mod`，项目私有工具链与 SDK 沿用已验证材料。G2/G3 真机结果不由本轮替代。

## 目录适配及原索引复用

[目录实验](../experiments/router-index/README.md)继续编译 hash 守卫的原 `indexer.rs/types.rs`，不修改匹配算法。新增一分区 adapter，规范化输入、完整拓扑快照/共同排他水位、连续增量、恢复任务 fencing、实例/publisher/身份隔离、预算、原子可见性和历史重建。元数据仅用于身份/拓扑验证与重建，不另写匹配树。

49 项 Rust 测试通过：原 24 项、缺父链反例 1 项、新目录 24 项。覆盖缺口恢复输入、空快照、缺父/循环、非驻留祖先、旧连接/旧任务、身份/布局不符、缓冲重复/冲突、clear、多操作失败、容量/祖先/总计费、序号溢出、查询预算、分支/128 层父链、深度拒绝和并发读写。没有真实 HUST 消费者，不将规范化合成输入称为 wire 接线或真实超窗通过。

固定 64 个保留驻留项、group 0/device/单实例；seq/local hash 均为 64 字符，与原模式保持相同长度。下表是父链深度保护加入前的第一轮，各模式独立进程，单次执行：

| 模式 | 更替次数 | 结束 RSS KiB | VmHWM KiB | 重建次数 |
|---|---:|---:|---:|---:|
| 原索引 | 200000 | 108392 | 108392 | 0 |
| 原索引 | 800000 | 422260 | 422260 | 0 |
| 合成完整快照周期重建 | 200000 | 7536 | 7536 | 48 |
| 合成完整快照周期重建 | 800000 | 7288 | 7600 | 195 |
| 新目录 adapter | 200000 | 11500 | 11500 | 49 |
| 新目录 adapter | 800000 | 11572 | 12036 | 198 |

adapter 最后分别保留 2447/1530 条元数据，均在 4096 限额以内；有效保留项查询通过。4096 是实验变量。adapter 除原索引外增加验证/恢复元数据，不能拿约 7 MiB 的纯重建实验当交付内存。峰值为进程 RSS，不是索引独占分配；字节上限是逻辑计费，未提供 allocator 硬 RSS 保证。

执行耗时原模式 0.740/3.103 秒，adapter 1.530/6.250 秒；同机 Router 编译仍在进行，不能作为隔离线上开销或吞吐结论。该观察说明资源检查有成本，需要更大工作集、并发查询与重建延迟评测，不能宣称无性能影响。

最终深度保护代码编译后，在 Router 编译结束时重测：

| 模式 | 更替次数 | 结束 RSS KiB | VmHWM KiB | 耗时秒 |
|---|---:|---:|---:|---:|
| 原索引 | 200000 | 108400 | 108400 | 0.722 |
| 原索引 | 800000 | 421972 | 421972 | 3.111 |
| 合成周期重建 | 200000 | 7712 | 7712 | 0.783 |
| 合成周期重建 | 800000 | 7632 | 7908 | 3.145 |
| 完整目录 adapter | 200000 | 10380 | 10380 | 1.627 |
| 完整目录 adapter | 800000 | 9728 | 10508 | 6.563 |

adapter 重建次数和保留量仍为 49/2447、198/1530。内存具有有界方向的证据；额外一致性/元数据维护也有 CPU 成本。这里是单次合成循环，不是独占物理 CPU 的统计服务压测，也不是请求热路径耗时；不能将内存减少直接换算成 NPU 达标容量提升。

## 审查中修正

- 去掉初版每个事件批次的全目录复制/全树检查，普通增量只处理本批有界操作；全树工作只在恢复和限额重建执行。
- clear 后旧索引可能仍留存到重建结束，预算保留其 overlap 计费，不因元数据 clear 就把其成本记为零。
- 同一父链位置出现不同引擎锚点时，原索引无法安全区分；新增拒绝/失信反例，不能静默当合法别名导入。
- 枝干仍需要的非驻留祖先保留拓扑，但在安装前去除其临时驻留 claim；压缩移除的孤立父节点被后续 store 引用时要求恢复，不回退到 root。
- 父链深度显式限制，恢复构建前及 live store 均拒绝超限；不以有限节点数推导树销毁栈深度已经可靠。

## 流式实验与剩余边界

最终固定依赖、项目私有 SDK 的 aarch64 release 编译通过，两个 example 分别为：

- stream：`7d50556525d6f46a6c39c3a9351e266a722695c82b42fd36e9e686ee6dce69b2`
- admission：`2bd11e0b57a49789ed687a2e33b613f35770f451e3a387f1b1055559756364c3`

`tools.router_stream_probe` 实际执行新 native library：无消费零 pull、慢消费者、drop 释放 source、idle、chunk/累计量、源错误、正常 EOF 均通过；实际 HTTP 两个 policy 分支分别走正常、超限、停滞、drop，共 8 次后端 POST，未重发。每次传输回调恰一次，原生连接计数回零；这不代表 M3 后端工作负债归零。错误配置在发送前拒绝，后端 POST 总数保持 8。两个 example 正常退出，无强制清理。

同一个新构建的 admission example 复验通过：prepare/select 37、reserve/close 24、sent 22、headers 20、not-sent 2，fixture 最后仍保留 8 笔未知负债；default-off、显式 fallback、发送门、503/drop/cancel、并发额度均通过。原生 mock 不含真实取消确认、chat 特征、请求/非流式总预算或性能收益。

从原 tar 重新应用 tracked patch 和特定 Unix 格式化后，三个原文件与实际编译输入逐字节一致；新增 helper/example 也一致。原版准入源码与另一次干净解包 `diff -qr` 一致。初次 tar metadata compare 因新分配目录权限不同失败，后续用内容比较核实未改源码，没有修改目录权限来伪装通过。系统 runtime 仍为 openssl-libs 3.0.12-52.oe2403sp3，SDK 隔离路线继续适用。

本机 Python CPU 270 passed / 4 platform skips、integration 3 passed。初次 scope guard 仍预期旧的两处错误回调，随新增错误出口更新为三处后复验通过；失败记录保留。初次误用 no-isolation 构建因测试 venv 无 hatchling 失败，旧 wheel 的检查不计入本轮新制品成绩；改用正常隔离 build（hatchling 1.32.4）后，新 sdist→wheel 和源码目录外干净安装通过。没有安装全局依赖。

服务器同步 0011f70 后 CPU 274、integration 3，新 sdist→wheel/干净安装通过；同步前本轮临时验证修改保存为具名 stash，再快进到同一提交，没有覆盖其他项目。Windows 跳过的是 Linux 权限/清理条件，不能计作已运行。

## CI 与可重复审计修正

0011f70 的 Python 3.11/3.12 CI success；新增原生 CI 首次失败于源文件 hash：GitHub blob 为 LF，历史 Windows 归档为 CRLF。已从固定 GitHub API 重读并核实规范化内容一致；仅增加这两个固定文件的精确 LF/CRLF hash，不关闭守卫或扩版本范围。两种输入各 49 项通过，故意修改的源文件仍被拒绝，失败 CI 保留。

新分配共享目录权限使原 build probe 的 tar metadata 比较误报；修正为固定归档解包后的精确文件集合/内容/符号链接比较，继续检查 Cargo/归档/RPM hash 与平台配套。新增 `preflight` 入口在新机器通过，无需重新完整编译；未修改系统或源目录权限。Git 推送先有连接失败，采用进程级 HTTP/1.1/OpenSSL 设置后成功，不改变仓库依赖或全局 Git 配置。

代码 `ab42d61a0e113650d4972ebb60f03c011520be33`：修正后的[原生索引 CI](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38242718189) success，[Python 3.11/3.12 CI](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38242718191) 均 success；Linux aarch64 同提交 CPU 274 pass。后续文档提交不自动继承该 head 的 CI 记录。已完成代码和文档自审，[草稿 PR #11](https://github.com/WMASTER123/prefix-routing-mod/pull/11) 以 PR #10 分支为 base，等待另一成员独立审查，不自动合并。原生制品不随 Python wheel 自动交付。

生产 M2 仍需 M7 wire/身份/控制面新鲜度接线、多分区总预算及更大拓扑性能验证。M3 仍需标准生命周期桥、可靠对账/留存/重启恢复。MOD 保持 import_only；NPU、策略收益、生产 ABI 冻结和独立人工审查未通过。
