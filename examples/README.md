# 示例

| 示例 | 依赖 | 用途 |
| --- | --- | --- |
| `evals demo --kind rag` / `harness` | 本仓库，无凭证 | 完整确定性闭环演示 |
| `semantic_fixture.py` | 本仓库，无凭证 | 独立语义校准和评分协议 |
| `kernel_smoke.py` | 本仓库，无凭证 | 原 Inspect 执行冒烟 |
| `integrations/serve_local.py` 等 | 本机 VeraAgent / NexRAG | 生成数据、脚本化模型的隔离接入 |
| `integrations/vera_live.py`、`nexrag_live.py` | 两个目标应用及临时模型凭证 | 真实模型、生成数据的隔离验收 |

接入示例由 `scripts/acceptance_*.py` 调度，直接操作各自临时工作目录。目标应用目录通过 `--source` 传入；当前隔离脚本针对 Windows 虚拟环境布局。示例不包含真实业务数据或凭证。

真实模型验收显式运行 `scripts/acceptance_live.py`，费用采用注明日期的供应商费率快照，需要自行核对后使用。不要将确定性回复的示例当作真实模型质量证明。
