# 建仓验证摘要

验证代码提交：8e9e559c5a9f16f77dbad9558890bc00f1107ee2。后续仅补文档的提交不改变这些测试的适用代码。日期：2026-10-10。

| 环境 | 实际执行 | 结果 |
|---|---|---|
| Windows / Python 3.12.13 | CPU 28 项、loopback mock 3 项、sdist/wheel、干净 venv 安装 | 通过 |
| GitHub Linux / Python 3.11、3.12 | 两组 CPU、mock、构建、干净安装、产物归档 | success |
| 新服务器 Linux aarch64 / Python 3.12.13 | 同一代码 CPU 28 项、mock 3 项、构建、干净安装 | 通过 |
| 真实 Manager 固定提交 | manifest 0.3 解析、入口发现、拒绝 enable、无配置落盘 | 符合 import_only 预期 |
| NPU 推理 / 路由 / 性能 / 正式发布 | 未实施，保留入口返回 3 / not-run | 未通过该阶段验收，非失败冒充通过 |

[GitHub Actions 原始运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38054039836)；本地 artifacts/checks、artifacts/*-junit.xml、artifacts/wheel.json，以及服务器同名 artifacts 目录保存详细结果。CI 产物保留 14 天，正式实验需要更长期归档。

服务器系统包清单与项目隔离环境分别保存在 artifacts/environment-system.json 和 artifacts/environment-venv.json。仅观察硬件和安装元数据；没有启动模型、占用 NPU 计算或改动全局环境。

复现安装依赖时服务器到 GitHub 超时，使用已在本地安装验证的同一 Manager 提交的完整 Git bundle；校验导入 commit 为 00d1b940dc6ce8d60e2cd28708b80d80e9c33a09。项目私有源码同样用 bundle 克隆，未向服务器复制 GitHub token。

局限：mock 仅验证夹具协议，未经过生产 Router；未测试取消/背压、KV 事件、精确匹配、强基线性能或完整 MOD 运行启停。不能以本摘要宣称任何吞吐/延迟优势。
