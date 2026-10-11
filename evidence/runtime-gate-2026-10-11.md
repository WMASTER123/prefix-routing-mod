# 运行环境门槛复核：2026-10-11

状态：**第一步未通过，后续真实回归未放行。** 当前分支由 `77a0344551730c10aa236d392760f169d008fdca` 继续，PR #2 尚待人工审查；不自动合并。本文是脱敏摘要，原始报告位于私有 artifacts。

## 已做的工作

- 只读复核预装核心包、完整依赖、源码提交及官方制品元数据。预装 vLLM/Ascend 源码未改动，公共权重未写入。
- 在项目 `artifacts/runtime-lock-20261011` 建立独立服务 venv（未安装推理依赖）和单独的构建 venv；后者为诊断使用 system-site-packages，不称为隔离服务环境。
- 从预装 vLLM 的固定提交 `0fc695fc6d1d82e9a5ac6835ac8e4e1c83703665` 复制 Git 源码到项目私有目录，构建 empty-target wheel。第一次缺 setuptools-rust 失败；补充私有构建工具后成功，最终构建固定 setuptools 80.10.2 / setuptools-rust 1.12.0。没有修改系统包，也没有引入 CUDA 构建。
- 最终本地构建 wheel 的 SHA256 为 `fbb85cfd62fb61068e3eeec75a1882c140c585525153d9b91e694c4a5013d86e`。这只是诊断制品，尚未安装或完成完整供应链锁定，不代表服务基线。
- 用 uv 0.11.19 完整候选解析发现冲突，再缩小为两个发行制品重复解析，返回 1。新增只读审计工具和 CPU 测试；不新增 Router/事件索引实现。

## 不可同时满足的约束

| 来源 | 要求 / 当前情况 |
|---|---|
| 固定 vLLM 0.23.0+empty METADATA | opencv-python-headless >=4.13.0 |
| PyPI OpenCV 4.13.0.92，Python 3.12 | numpy >=2 |
| 官方 Triton Ascend 3.2.2 aarch64 CPython 3.12 wheel | numpy ==1.26.4 |
| vLLM-Ascend 0.23.0 / 0.23.0.post1 | triton-ascend ==3.2.2 |

初次尝试 NumPy 2.2.6 也被同一 Triton 约束拒绝；没有安装该候选。升级 NumPy、降级 OpenCV或更改安装顺序，都不能同时满足上述原始元数据。Triton 的安装顺序问题与此约束冲突是两件事。

预装 `pip check` 另报告 instrumentator 8.1.0 要求 Starlette >=1，但 FastAPI 0.123.10 要求 Starlette <0.51，当前为 0.50.0。instrumentator 7.1.0 是该局部冲突的待验证修正候选，未安装、未称为完整修复。profiling 工具还有缺失包、OTel/Pandas 冲突和 te 平台标签问题；服务插件 allowlist 排除 profiling，不等于全局依赖检查通过。

系统审计 JSON 的 SHA256：`9d597421660a72df88aaa99e9748c2130b33a4d04d2c20fbb7dcc14b3e52ecdd`。结果 fail，核心包齐全；不是因为空环境或 pip 缺失而失败。

## 官方候选核查

经用户同意，只读检查官方发行候选：0.23.0.post1、0.26.0rc1、0.27.1rc1 的元数据仍要求 Triton Ascend 3.2.2。当前官方主干 requirements 也保留此固定依赖。本次没有安装它们，没有给主干或 RC 作兼容保证，也没有证明全部历史组合不可行。

一手来源：[Ascend 0.23 安装说明](https://docs.vllm.ai/projects/ascend/en/v0.23.0/installation.html)、[Triton Ascend v3.2.2 官方发行](https://github.com/triton-lang/triton-ascend/releases/tag/v3.2.2)、[OpenCV 4.13.0.92 元数据](https://pypi.org/pypi/opencv-python-headless/4.13.0.92/json)、[Ascend post1 元数据](https://pypi.org/pypi/vllm-ascend/0.23.0.post1/json)。移动文档仅作入口，以实读制品和本地固定源码为判断依据。

## 对上轮 KV 诊断的补记

以下是先前预装环境的诊断证据，**不是本轮隔离环境测试，也不是生产恢复验收**：

- 原 TCP `PUB connect / SUB bind` 探测未收到事件；不是据此认定 Ascend 不支持事件。确切传输根因尚未确定。
- 同机 IPC `PUB bind / SUB connect` 的真实 HTTP 服务探测通过：收到原生 BlockStored，5 blocks、640 tokens、block size 128；非流式、重复请求和 24-token SSE 完成，关闭无强制组清理。
- 离线诊断使用 TCP 发布端绑定动态 loopback 端口并由 SUB 连接，收到 seq 0 的 BlockStored，以及 seq 1 的 AllBlocksCleared + BlockStored；清空操作成功。该诊断临时包裹内部方法用于计数，不是产品 hook，未修改引擎文件。
- 这些成功收窄了接线问题，但没有验证 BlockRemoved、重放窗口、断连、epoch、重启或异步可复用语义。不能宣布 `kv_events_validated=true`。原始记录分别位于 `qwen14b-events-ipc-20261011-01` 和 `kv-compatibility-review-20261011`。

## 变更与审查边界

本轮只提交审计工具、CPU 回归、拒绝候选的重现输入、文档和证据；不提交权重、原始日志、构建源码、wheel 或凭据。未启动新的推理服务；四卡检查无 NPU 进程。新构建与下载仅在项目私有环境及包管理缓存，不修改驱动或全局 Python。

下一步需要选择可接受的环境处理路线；在有通过证据前不接 Router、不跑双副本或策略收益实验。

本地自检：CPU 60 项、mock integration 3 项、sdist/wheel 构建和干净 wheel 安装检查均通过。服务器已同步实现提交 `18929fe`，aarch64 项目开发 venv 的 CPU 60 项、mock integration 3 项也通过。人工审查仍必需，不称为独立审查通过。审查重点包括：只读元数据而不导入引擎、使用同一解释器检查、空环境不能通过、超时非成功、证据不可覆盖、URL 凭据脱敏、运行能力标志不由 pip check 自动提升、原环境与共享数据未改。完整 NPU/路由/性能套件仍未运行。
