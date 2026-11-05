# Evals 项目说明

## 一、项目目标

Evals 是基于 Inspect AI 开发的独立 AI 应用评测与持续改进平台。**已确定将选定版本的 Inspect AI 内核源码纳入 Evals 主仓库，由本项目维护、修改、测试和发布。** Evals 拥有自己的程序入口、数据模型、API 和版本体系，工作台直接扩展原有 Inspect View。目标应用可以是 Python 或 Rust 编写的 Harness、RAG、Agent 或其他服务。

产品有两层目标。第一层是帮助接入应用找出问题，提出多个可执行的改动，通过独立复测给出版本建议，并核验采用后的行为。第二层是改进 Evals 自身的诊断和候选生成策略。后者须在新的任务上用相同预算对照旧策略，衡量有效改进数、误推率与总成本。

## 二、项目功能

| 评测维度 | 要回答的问题 | Evals 需要编写的判定逻辑 |
| --- | --- | --- |
| 结果 | 用户要的业务动作真的完成了吗？ | 核对答案、数值、生成物、业务前后状态与副作用。为每题写验收条件。 |
| RAG 检索 | 正确资料在哪一步丢失？ | 记录检索请求、文档与索引版本、召回、重排及送入模型的片段。用相关性标注计算召回率和排序质量。 |
| 证据 | 结论是否有依据？ | 将主张对应到具体来源，检查引用支撑、覆盖、时效以及来源可见性。 |
| Agent 过程 | 工具选择、参数、顺序、重试和停止是否合理？ | 按事件还原轨迹，定位冗余调用、提前结束和错误交接。允许不同的有效路径。 |
| 稳定性 | 同类任务重复执行，结果和代价波动多大？ | 绑定样本与重复序号，统计成功率、波动和分组退化。 |
| 异常处理 | 出错后能否恢复并保持业务状态一致？ | 注入超时、限流、工具或数据库失败，检查重试、幂等、补偿与清理。 |
| 成本效率 | 正确完成一次任务花了多少？ | 总运行费用除以成功任务数，同时报告耗时分位数、Token 和工具次数。无成功样本时该比值不可计算。搜索与评测开销单列。 |
| 安全权限 | 在给定身份下，哪些行为必须被允许或拒绝？ | 测试越权、跨租户、敏感信息泄漏、权限撤销和危险副作用。违规独立判定。 |

**接入深度决定可评价范围。** 只有问答接口的应用可评输出。上报检索、工具或业务状态后，平台才能核验对应维度。数据库拒绝访问，在越权测试中可能是通过，在合法查询中可能是故障。若只是评测方无权核验，就记录为“无法核验”。报告分别列出通过、失败、不适用、未观测和受阻样本数。

**一次改进如何形成。** 目标应用声明可调整的资产、参数类型、范围和执行权限。Evals 从轨迹提出相互竞争的原因假设，组合原版、文本候选、配置候选及应用内部 RSI 提交的候选。冻结入围版本后，运行反例、全量适用维度回归和独立留出验证。用户或内部 RSI 根据证据选择，应用回传生效版本，后续新任务检验收益。例：RAG 答错时先注入已知正确的片段重跑，能区分检索遗漏与拿到证据仍答错两种情形，分别指导召回配置或 Prompt 的修改。

## 三、基座项目现状以及组件整合

当前主仓库 [Felix-0625/Evals](https://github.com/Felix-0625/Evals) 已备好三个参考检出，原目录继续由 Git 忽略。纳入自有内核是已定开发路线，源码导入、产品编码和端到端验证尚未开始。版本依据见[上游清单](upstream-sources.json)。

| 模块 | 直接继承或复用 | 本项目开发的部分 |
| --- | --- | --- |
| Inspect AI 内核 | Task、Dataset/Sample、Solver/Agent、Scorer、模型调用、样本并发、运行日志。 | 将固定源码放入主仓库受跟踪的内核目录，从本地源码构建。用 Solver 执行原应用，用 Scorer 读取业务断言。需要改调度或日志语义时，在自有内核内修改并做上游兼容回归。[组件机制](https://inspect.aisi.org.uk/extensions-components.html) |
| Inspect Scout | Inspect 原生支持在线挂载 scanner，也支持扫描已有执行日志。 | 编写重复调用、检索遗漏、证据无支撑等分析器，把发现锚定到样本与事件。用标注样本校准误报、漏报，诊断假设由后续实验验证。[原生扫描流程](https://inspect.aisi.org.uk/scanners.html) |
| GEPA | 根据逐样本分数和反馈搜索多个文本候选。`evaluate()` 为同步方法。 | 实现 GEPAAdapter，在隔离副本上用自有 Inspect 内核运行候选。因 GEPA 入口同步、Inspect 内核 async 且单活跃 `eval_async`，候选评测在隔离进程中执行以桥接两者。检索参数、工具策略由配置搜索或目标系统提供。所有来源共用独立验证规则。[适配器协议](https://gepa-ai.github.io/gepa/guides/adapters/) |
| Evals 产品层 | 复用原任务调度、运行控制、日志、Viewer、Scout 发现和 GEPA 候选。 | 补齐应用声明、八维业务规则、跨运行的改进控制器、验证门槛、版本回执和对应页面。 |

**工业界做法与我们的实现对应关系**

| 参考 | 已有实践 | Evals 采用的设计 |
| --- | --- | --- |
| 阿里云 AgentLoop | 逐题评分标准、私网执行、同题回测。经验注入用基线与消融实验验证。 | 样本保存业务断言，在目标环境核验真实结果，针对具体改动做对照。[逐题回测](https://help.aliyun.com/zh/agentloop/use-cases/iv-experiments-back-testing-offline-experimental-platform-and-topic-level-rubric) · [消融实验](https://help.aliyun.com/zh/agentloop/use-cases/v-experience-base-automatic-mining-of-experience-assets-ablation-experiments-to-verify-real-returns) |
| 字节 Coze Loop | 一场实验绑定数据集、评测对象和评估器版本，支持逐例比较与人工纠正评分。 | 实验固定三类版本，分别追查应用执行错误与裁判误判。[开源实验定义](https://github.com/coze-dev/coze-loop/blob/v1.5.1/idl/thrift/coze/loop/evaluation/domain/expt.thrift#L32-L60) · [实验分析](https://docs.coze.cn/cozeloop_create-experiments) |
| Langfuse | 从线上轨迹及其步骤关联反馈，样本保留来源，实验按案例查看哪些由通过变为失败。 | Evals 自己实现步骤级证据、样本来源和逐例版本对比。Langfuse 在本项目中的角色仅为设计参考。[实验数据模型](https://langfuse.com/docs/evaluation/experiments/data-model) · [实验对比](https://langfuse.com/docs/evaluation/experiments/compare-experiments) |

## 四、项目的实现及展现方式

**沿用 Inspect 的运行方式。** CLI 或 Python 函数组装 Task，Inspect 执行样本并写入 `.eval` 日志，`inspect view` 读取日志。已有运行控制接口负责查询样本、暂停、恢复、取消和重排。Evals 在这条路径上增加跨运行的实验、候选、独立复测与版本采纳记录，外部 API 将相应操作交给原执行和控制接口。需要访问私有数据库时，可把同一套程序运行在目标网络，凭据留在当地。[批量运行与恢复](https://inspect.aisi.org.uk/eval-sets.html) · [运行控制源码](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8cef181c1f04d3fae70db95c6e7bef5715df7260/src/inspect_ai/_control/server.py)

**沿用已有前端。** Inspect View 已能展示运行、样本、对话、工具事件和评分。源码来自固定版本的 `ts-mono/apps/inspect`，使用 React、TypeScript 和 Vite。编译后的静态资源在 Python 包的 `_view/dist`，由 FastAPI/Uvicorn 同源提供。本地当前只有构建产物（`_view/dist`），前端源码子模块尚未初始化、`_view/ts-mono/` 为空目录；需在 M0 先展开该源码后才能重建或修改前端，在此之前以预编译 `dist` 透传。Evals 保留原有样本和轨迹页，在相同界面补上八维覆盖、基线与候选逐例对比、独立验证与版本回执。[前端源码及共享组件](https://github.com/meridianlabs-ai/ts-mono/tree/91f2be5875fe2ee1d7201fe2290020c344d9bde7) · [构建脚本](https://github.com/meridianlabs-ai/ts-mono/blob/91f2be5875fe2ee1d7201fe2290020c344d9bde7/apps/inspect/package.json) · [查看器](https://inspect.aisi.org.uk/log-viewer.html)

**给程序的结果。** API 和页面读取同一份版本建议，包含基线版本、候选差异、收益与波动、退化样本、硬约束、证据引用和采用条件。应用内部的 RSI 可按该协议提交自己的候选、取回验证结果、回报生效版本。所有候选退化或证据不足时，可保留原版。

技术路线、源码目录、剩余的实现选择和开发顺序见 [Plan.md](Plan.md)。
