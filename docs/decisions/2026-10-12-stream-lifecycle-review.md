# ADR：流式审查修复与有限标准生命周期实验

日期：2026-10-12。状态：CPU 研发实验，生产接口仍待验证及另一成员审查。

## 问题与选择

审查发现：持续返回 Ready 的空数据块可绕过原 idle 检查；Windows 大响应 fixture 因未读请求体就关闭连接偶发 reset；流式 ADR 的队列描述与直接拉取代码不同。修正这些具体问题并补回归，不放宽超限断言或版本守卫。

下一接缝是 M5 → M3 标准生命周期。沿用 component-v0.1 的字段和既有账本 oracle，在真实 Router Lease 上附有限记录，比另写生产账本更容易先核对事件语义。每尝试最多两个事件，只支持 SEND_STARTED、NOT_SENT/NOT_ACCEPTED 与 OUTCOME_UNKNOWN/NONE；headers 不等于接受，传输结束不等于后端结束。

记录使用 Router 进程共同单调时钟、实例身份及唯一尝试 ID。当前这些身份由 CPU fixture 提供；记录结束后重放，不能宣称生产有可靠投递。实验将每次回调事件与旧 oracle 的准入、序号、重复及未知保留语义核对；原 fixture 的合成 ENDED 清债另行说明。

## 维护边界与验收

继续复用三处宿主接缝、原 HTTP client/body 和索引。新增 lifecycle helper 只用于共享 CPU 检查和原生 example，不注册成引擎插件；实验文件、测试 crate/锁和工具均计入维护范围。新增 CI 直接编译共享 helper，完整宿主 aarch64 构建及实际 HTTP fixture 手工执行。

空块测试必须在旧代码上失败、新代码上通过；同时检验让出执行权、非空进度重置、慢消费者、限制及关闭恰一次。标准生命周期记录须通过现有 oracle，真实 EOF/错误/drop 不产生 ENDED。保留首次失败、复测及确切提交证据，MOD 继续 import_only。

生产接线仍需可靠在线投递/去重、可靠完成与取消确认、满额行为、实例映射、留存及重启恢复。固定 HUST 的 `[DONE]`、前端 finished 和 abort send 都不能单独满足此条件，见[来源审计](../planning/backend-terminal-evidence.md)。这些门槛通过后再冻结生产接口；需要超出小型观测的引擎变更时先讨论。
