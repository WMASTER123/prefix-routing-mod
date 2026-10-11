# Router CPU 准入与参考账本修正

2026-10-11 开始，2026-10-12 续验。本轮按用户批准的四步执行；当前为研发分支，自检不代替人工合并审查。

## 已完成的修正

- `fac7773` 修正参考账本：uncertain 后的新 FAILED/NONE 不构成状态转换，拒绝且不消耗序号或留存；M5 在事件产生处收敛重复诊断。相同事件仍幂等，可靠结束确认和既有接受证据保持有效。增加三种可靠终态及最长合法路径四项反例。
- 修正三份活规划、操作/支持/Ascend 文档和工作区副本的旧四卡“当前”描述；当前 CPU-only 16 核配额、64 GiB RAM、16 GiB shm、0 NPU，历史 NPU 成绩保留原范围。澄清 R1 可在 CPU 阶段执行、真实接线等 G1–G3。
- `f8354fb` 增加手动隔离构建入口；`579146a` 增加实际原生二进制的 loopback HTTP/SSE 验证入口，复用已有 mock backend 与自有进程组清理函数。

## 固定原提交构建

候选 `91b65ee4bdfbe98863a32a2205dced5dbb249ffe`，Cargo.toml/lock 与既有证据相同，源码和依赖锁未修改。项目私有 Rust/Cargo 1.99.0，4 jobs。openEuler 24.03 LTS-SP3 的运行库是 `openssl-libs-3.0.12-52.oe2403sp3.aarch64`。

从 [openEuler 官方 update 仓库](https://repo.openeuler.org/openEuler-24.03-LTS-SP3/update/aarch64/Packages/)取得同版本 openssl-devel；RPM digests/signatures OK，SHA256 `0c5eedb36f98e2961c97a0e84901535030a97ea1f3c5610bf789ba29bb329e40`。开发包解压到项目私有前缀，配对系统 libssl/libcrypto 的副本；pkg-config/OPENSSL 路径仅进程内生效。没有全局安装、改变 HUST/CANN 或修改 Cargo 依赖。

前置核查发现 proto 构建使用 Rust protox，缺少 protoc 不构成本候选要求；gcc/g++、glibc/libstdc++ 开发包、cmake/make/perl 已有。OpenSSL 开发包要求参考 [rust-openssl 构建说明](https://docs.rs/openssl/latest/openssl/)。

原锁 `cargo check --lib --locked` **通过**，耗时 5m24s；仅原源码 Atomic::fetch_update 弃用警告。首次 release 在最终链接 exit 101：SDK 的开发链接指向 `.so.3.0.12`，先前副本只有 `.so.3`，链接器误用静态 `.a` 后留下 inflate/deflate 等未解析引用。没有补装 zlib-devel 或更换依赖；核对 RPM 的真实链接目标、复制匹配的 runtime 目标、显式 `OPENSSL_STATIC=0`，独立重试 release **通过**（10m52s，复用前次编译 cache，不是冷构建时长）。脚本增加真实目标检查与整个源码归档 compare；首个链接失败保留。

原生可执行文件 SHA256 `14bbcd793651e681fc9bd115bf55e0c2dd7d1fd6b872b9836111e2d346b5d272`，约 52 MiB；ldd 指向配套系统 libssl/libcrypto.so.3，没有 SDK 静态链接替代。`--help`、冷进程启动健康、completion/chat 各 JSON/SSE、关闭重试后的 503 单次后端请求，六组检查 **通过**。总后端 POST 5 次；退出码 0，无强制清理。健康时 VmRSS/VmHWM 24572 KiB、17 threads 是一次观测，不是吞吐/Host 开销评测。

最初手动 rpm2archive 命令误以为工具默认写文件，输出进入终端而解压未执行；改为显式接收其 stdout 后正确解压。可重复入口已采用正确方式。该错误未修改系统文件。

## 验证范围

579146a：Windows CPU 262 passed / 2 skipped，loopback 3 passed；Linux aarch64 CPU 264 passed，loopback 3 passed。静态检查和格式检查通过。新增 HTTP 探针响应上限和错误响应保持测试也在上述 CPU 数中。

Windows 首次隔离制品构建在第二次取得 hatchling 时因 PyPI TLS EOF 失败；此为下载失败，未改项目依赖。使用进程内代理和 uv 安装器重试，sdist→wheel→源码目录外干净安装检查通过，builder 为 hatchling 1.32.4；未关闭证书验证。Router 运行结果在完成后补记，不能继承前一轮运行成功。

NPU、生产 M1–M5/M7、性能收益、MOD 激活未验收。扩展原型在原版启动/转发通过后实施；生产接口、代码目录、二进制交付仍待 R1 全部退出条件。

## 索引长期资源反例与重建可行性

[实验源码与原文件 hash 守卫](../experiments/router-index/README.md)直接编译固定候选的原 indexer/types，未复制或改索引算法。最小 crate 自有 Cargo.lock；固定保留 64 个实际索引条目，其他唯一块持续 store/remove，单 owner/group/device tier，hash 为 64 字符。原源码明确 anchors never trimmed；清驻留不等于释放全部历史拓扑。

| 模式 | store/remove 次数 | 完成后 RSS KiB | VmHWM KiB | 重建次数 |
|---|---:|---:|---:|---:|
| 原索引 | 200000 | 108328 | 108328 | 0 |
| 原索引 | 800000 | 422364 | 422364 | 0 |
| 合成完整快照重建，周期 4096 | 200000 | 7440 | 7440 | 48 |
| 合成完整快照重建，周期 4096 | 800000 | 7876 | 7876 | 195 |

重建前及 staging/安装后均采样，并报告内核进程高水位；不是索引独占内存。64 个保留项匹配始终通过。25 项 Rust 测试通过：上游 24 项 + 本项目缺失 parent 反例。后者确认上游会把未知 parent 的 child 挂 root，M2 不能未经完整性验证直接导入。Rustfmt 首次缺失，随后在项目私有工具链补入，并格式检查；没有改系统 Rust。

判断：原索引无长期历史上限，不能裸复用；完整重建具有资源可行性，但**未证明生产有界恢复**。实验没有并发读者、真实共同水位、复杂父链、多域/epoch、恢复队列/字节预算；周期 4096 不是默认配置。测试与 Router 构建同时运行，耗时不能用于性能或重建开销结论。

需要生产证明的总预算包括活动 claim/父链、历史锚点、hash/元数据字节、group/domain 数、恢复缓冲、staging 和旧视图读者。候选路线是完整 staging 校验后原子安装；达到预算先退出精确模式、限制恢复，不丢掉父链后仍宣称 trusted。若必须改索引内部算法或大范围 transport，先提交具体范围再讨论，不借此直接重写 M2。

## 最小接口原型范围与审查

原版转发通过后才在独立 artifacts 副本应用[三文件 patch 和本项目接口/example](../experiments/router-extension/README.md)。宿主增量为 3 文件、147 新增/3 删除行（规范化换行后的逻辑统计）；额外两份 Rust 文件属于项目实验，不是隐藏的宿主 patch。Cargo.toml/lock 不变。最初 example 离线构建缺 anes dev-dependency cache，exit 101；改用原锁下载后第一次 build 通过（12m26s），未替换依赖。审查补入网络错误与本地响应 EOF 区分、完整决定携带、并发准入和取消反例后，最终 release example 构建 **通过**（8m05s，warm cache），hash `673f1a074f378f2f82a521e99243f74d4db932c34c39e53d900dd0241df3f6ae`。

真实宿主库与两个 loopback HTTP fixture 回归 **通过**：未装 provider 的 JSON/SSE/503、重复安装拒绝、prepare 一次并传递合成完整 tokens/决定、陈旧/非候选目标拒绝、reserve 拒绝、reserve 后配置/控制变化导致发送前拒绝、JSON/SSE EOF 留未知、503 与连接断开不重发、body drop/future cancel 留未知、显式选点 fallback、满额拒绝和 16 并发争取 8 个额度。prepare/select 各 37 次，reserve/close 各 24 次，sent 22、headers 20、not_sent 2；fixture 未决债务最终 8，未被 EOF 清零。后端实际 26 次 POST：fixture 21、error 2、disconnect/stall/fallback 各 1，被拒绝的模式没有到后端。进程正常退出，无强制清理。**启用 provider 的原型测试仅 completion；chat 的原版转发通过不能替代其特征桥测试。**

补丁复放首次失败发现原归档有 CRLF，而项目 patch 为 LF；只在独立副本使用进程级 core.autocrlf=true 进行 apply，并对 registry/router 显式 Unix 格式化。原版源码整包 compare 仍通过，复放三处源码、项目两份 Rust 与 Cargo 文件逐字节等于实际编译输入。没有改全局 Git 设置；文档记录复放条件。统一 diff 的空 context 行必须保留前导空格，仓库仅对 `.patch` 的 blank-at-eol 作专用属性，实际 apply 使用 whitespace=error 检查代码。

自检重点：prepare 一次、M4 只读决定与 M3 reserve 分开；发送前重新检查、未发送撤销；HTTP 5xx/EOF/drop/取消不当终止证据；回调恰一次；未装 provider 与显式 fallback 分开；未知留存满额拒绝；日志解析预算。scope 测试最初按缩进检查失败，改为匹配实际语句，不通过删故障检查消除失败。没有改 HUST、启用 MOD 或发布生产二进制。另一成员独立审查待人工进行，本轮没有分派任务或自动合并。

同一最终 example 在三个新建 loopback fixture 环境重跑均通过，发送计数/未决负债一致，均正常关闭；原版二进制 hash 保持不变。手动 Router/Rust 测试不是 GitHub Actions 的 Python CPU 项，成绩分别记录。

代码 `2e37376`：Windows CPU 269 passed / 2 skipped，Linux aarch64 271 passed，integration 两端各 3；Ruff/格式/文档检查通过。两端 sdist→wheel→干净目录安装、静态 MOD 描述与 import_only 检查通过；Rust 原型不装进当前 wheel。Windows pip 新版本提示的网络检查出现 TLS EOF，但本地 wheel 安装/验证成功，未关闭证书或更新全局 pip。后续文档 head 的 Actions 独立核查，不继承旧 CI。

仍开放：M2 真实恢复和总预算；HTTP 原生无界 channel 的慢客户端/producer 中止；完整 LifecycleEvent 桥与可靠后端对账；真实身份及特征开销；升级/运行期禁用/排空和 MOD 制品。故本轮不能冻结生产 ABI、目录及交付形态，不能称生产组件已经验收完成。
