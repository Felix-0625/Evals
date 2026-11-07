# 架构与维护入口

Evals 面向外部 AI 应用。应用通过 Python、HTTP 或 CLI 提供 Observation；Inspect 运行 Task、Solver 和 Scorer，保存逐题结果。浏览器和 CLI 调用同一个 Service，工作目录只有一个写入进程。

| 位置 | 职责 |
| --- | --- |
| `models.py`、`validation.py` | 冻结协议、资产范围、数据分区和执行身份 |
| `adapters.py`、`worker.py` | 外部应用调用、原生任务与逐题业务评分 |
| `service.py` | 实验编排、候选选择、独立验证和恢复 |
| `runtime.py`、`coordination.py` | 工作进程、原生控制、派发前预算与费用对账 |
| `scoring.py`、`verification.py` | 证据判定、覆盖分母、配对分组统计和采用门槛 |
| `analysis.py`、`rescan.py` | Scout 轨迹分析与离线重扫 |
| `evolution.py`、`reflection.py` | GEPA 适配及隔离文本提案插件 |
| `semantic.py`、`semantic_worker.py` | 授权字段、独立校准、语义分析与评分 |
| `adoption.py` | 实际配置采用、失败回执和回滚一致性 |
| `monitoring.py`、`strategy_studies.py` | 采用后观测、回归案例、三臂策略对照 |
| `store.py` | 排他写锁、幂等请求、乐观版本与原子持久化 |
| `api.py`、`cli.py` | 同一业务服务的 HTTP 与命令行入口 |
| 前端 `src/evals` | Evals 操作页面、边界 JSON 转换和独立证据展示 |

自有编排与原生日志通过 run、sample、epoch、candidate、应用版本和配置指纹关联。工作目录状态保存索引和治理信息，完整事实由原生 `.eval` 日志持有。恢复前检查冻结指纹、构建身份、工作进程及远端状态。

## API 是否有无用入口

登记路由位于 `api.routes`，OpenAPI 为 `/api/openapi.json`。页面覆盖实验、候选、控制、版本回执、观测和策略；CLI/接入工具还使用应用登记、业务校准、经验登记、回归案例和恢复。诊断与结果查询接口为公开本机 API，不能仅凭网页没有引用就删除。

首轮核查保留这些对外契约，没有证据确认存在可安全删除的自有后端路由。原 Viewer 的日志、活动样本及控制接口仍由其页面和内核使用；不能因 Evals 页面没有直接引用而删掉。路由级请求参数和权限边界由 API 及控制回归测试覆盖。

## 修改原则

优先在职责所在模块修复，避免重复实现原内核。配置采用和策略研究已从大型编排模块拆出，前端逐运行证据已独立；暂不为了文件行数引入混合继承或通用工作流抽象。注释解释关键约束，完整业务流程见 [评测与 RSI](evaluation-and-rsi.md)。
