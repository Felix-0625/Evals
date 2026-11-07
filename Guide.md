# Evals 项目说明

## 一、项目目标

以 Inspect AI 为基座，开发独立的 AI 应用评测与持续改进平台。内核源码纳入 Evals 自有仓库维护，直接延续原有后端、前端和运行机制。

平台面向 Harness、RAG、Agent 等应用，通过评测发现问题，提出多个候选改法，独立验证后支持目标系统采用新版本。应用可接收建议，也可通过 API 与自身的 RSI 配合完成改进。

## 二、项目功能

| 评测维度 | 关注内容 |
| --- | --- |
| 结果 | 答案、生成物和实际业务状态是否满足任务要求。 |
| RAG 检索 | 正确资料是否召回、排序并进入模型上下文。 |
| 证据 | 引用是否支撑结论，来源是否有效且有权访问。 |
| Agent 过程 | 工具选择、参数、步骤、重试和结束判断是否合理。 |
| 稳定性 | 重复执行和输入变化后的成功率、波动与退化。 |
| 异常处理 | 超时、限流、工具失败后的恢复与状态一致性。 |
| 成本效率 | 每次成功任务的费用、耗时和调用量，以及优化本身的开销。 |
| 安全权限 | 越权、泄漏、跨租户访问、权限撤销和危险副作用。 |

评测结论必须关联证据。区分正确拒绝、应用误拒和评测方无权核验，缺失证据记为无法核验，不适用的维度单独标记。

改进流程为 **接入应用 → 运行基线 → 分析证据与原因假设 → 生成多个候选 → 独立复测 → 采用与回滚 → 观察后续收益**。候选可以修改 Prompt、工具策略或检索配置，也可由目标应用自身的 RSI 提交。报告同时呈现收益、成本、退化样本和不确定性，允许保留原版。

## 三、基座项目现状以及组件整合

| 组成 | 已有能力 | 改造方向 |
| --- | --- | --- |
| Inspect AI | 任务、模型调用、评分、并发、运行控制、日志和 Viewer。 | 在原框架中补充应用适配和八维业务规则，保留已有执行与展示能力。 |
| Inspect Scout | 可直接扫描 Inspect 运行轨迹，也可离线分析历史日志。 | 编写问题分析器，将发现定位到具体样本与事件，为候选生成提供证据。 |
| GEPA | 根据评分和反馈搜索文本候选。 | 通过适配器调用 Inspect 复测 Prompt 等改法，与配置候选共同进入独立验证。 |
| Evals 自有部分 | 基于以上组件开发。 | 组织跨运行的实验、候选对比、验证结论、版本采用和外部 RSI 协作。 |

复用关系依据上游提供的组件、Scanner 和适配器接口。[Inspect 组件](https://inspect.aisi.org.uk/extensions-components.html) · [Scout 接入](https://inspect.aisi.org.uk/scanners.html) · [GEPA 适配器](https://gepa-ai.github.io/gepa/guides/adapters/)

工业实践用于指导设计。借鉴阿里 AgentLoop 的逐题业务标准和对照实验，Coze Loop 的数据、对象、评估器版本绑定，以及 Langfuse 的轨迹证据和逐例版本比较。[AgentLoop](https://help.aliyun.com/zh/agentloop/use-cases/iv-experiments-back-testing-offline-experimental-platform-and-topic-level-rubric) · [Coze Loop](https://docs.coze.cn/cozeloop_create-experiments) · [Langfuse](https://langfuse.com/docs/evaluation/experiments/compare-experiments)

## 四、项目的实现及展现方式

后端沿用 Python、Inspect 执行器与 FastAPI，前端沿用 React/TypeScript 的 Inspect View。在原页面增加八维覆盖、候选差异、退化案例和版本记录，对外 API 返回相同的结构化结果。[原前端源码](https://github.com/meridianlabs-ai/ts-mono/tree/91f2be5875fe2ee1d7201fe2290020c344d9bde7)

采用后以实际生效版本和新任务检验收益。平台自身的 RSI 另用固定预算、新任务和冻结的旧策略作对照，检验有效改进数、误推率和费用。

当前已实现 M0–M5 的登记能力；已验证范围及未验证事项见 [当前状态](docs/implementation-status.md)。开发设计见 [Plan.md](Plan.md)，上游版本见 [upstream-sources.json](upstream-sources.json)。
