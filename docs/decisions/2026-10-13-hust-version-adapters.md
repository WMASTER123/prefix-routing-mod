# HUST cedb477 迁移与版本适配

日期：2026-10-13。状态：用户批准执行；新环境与适配逐门验证，尚未宣称生产支持。

## 输入与决定

目标 Core 为 vLLM-HUST/vllm-hust@ced6857afa0ea7b2e3f0846a62e1394e90f15607，Ascend 为 vLLM-HUST/vllm-ascend-hust@b47795c63a8e63e7aed4d311f951428d94e045ab。后者的 .github/vllm-main-verified.commit 精确指向前者。发行包分别为 vllm / vllm-ascend；包名不能代替 HUST 源码来源证明。

用户提供的另一项目记录包含这对提交的 Qwen2.5-14B、FP16/eager、两个 TP1 实机子集。此次允许只读核验并复制其固定 wheel/锁及来源记录到本项目 artifacts；不修改另一项目源码、venv、运行目录或服务，不整体移入其 Router/账本。独立安装、检查与模型验证只在 /root/work/prefix-routing-mod 下执行。已有 NPU 成绩不是本项目新分配的验收成绩。

M7 增加可分发的版本 profile 与统一 KV 规范化入口；M5 的 Router 宿主接缝独立管理。声明式映射负责包身份、字段/枚举、消息格式与能力描述；行为变化仍由有合同测试的少量适配代码处理。策略、账本及索引不散布引擎版本分支，不增加在线翻译服务。公开稳定 API 可按协议族复用；私有 hook 继续固定接口来源及源码守卫，不盲目放宽版本范围。

## 能力与退出条件

新原生 publisher 为三帧 PUB、整数起点的 sequence replay，没有权威 epoch/token snapshot。只能规范化消息，不能仅凭 END_SEQ 建立完整恢复或热接管资格。旧可选 publisher 依赖旧 HUST 镜像/结构化恢复方法，不得只替换常量在新版本启用。

此次先完成 profile/CPU 合同、新候选依赖与制品恢复、单卡真实请求及原生事件规范化。可靠终态、动态账本闭环、新版共同水位 hook、长期 GC 与 MOD 启停分别待后续验收；MOD 保持 import_only。未知版本、混装 Ascend 包、错误字段/来源/布局拒绝相应适配。新能力缺失不能伪造为零命中、已结束或空缓存。

目标实机首验为当前两张 910B2、aarch64、8 核配额/64 GiB RAM；先一张卡，FP16/eager、Qwen2.5-14B、TP1/DP1、纯文本 full-attention/group0。本轮不继承旧 BF16/图/性能档案。若固定制品、依赖、worker bootstrap 或模型失败，保留失败，先定位再选择同 HUST 仓库的候选，不改全局环境或伪造版本。

## 来源

- [固定配对](https://github.com/vLLM-HUST/vllm-ascend-hust/blob/b47795c63a8e63e7aed4d311f951428d94e045ab/.github/vllm-main-verified.commit)
- [固定事件协议](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/distributed/kv_events.py)
- [Ascend 自身加载/适配](https://github.com/vLLM-HUST/vllm-ascend-hust/blob/b47795c63a8e63e7aed4d311f951428d94e045ab/vllm_ascend/__init__.py)
- 用户提供的知识线程：01a0a417-25ae-7a72-a0b4-b718abeb47f7；核验其 owner-local wheel/lock 与来源记录，私有绝对路径不作为可移植安装接口。
