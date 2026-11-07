# 使用与接入说明

基于自有 Inspect AI 内核的应用评测与受控改进平台。目标和阶段验收见 [Guide](../Guide.md)、[Plan](../Plan.md)、[development](../development.md)；实际完成情况见 [开发与验收记录](implementation-status.md)。

## 安装与运行

需要 Python 3.12、uv。仓库已包含派生内核及固定组件，不依赖三个被忽略的参考克隆。

```powershell
uv sync --locked
uv run --no-sync evals --workspace evals-workspace serve
```

浏览器打开 `http://127.0.0.1:7575/#/evals`。首版使用本机单可信操作者，监听回环地址，延用 Inspect 的主机、浏览器来源和日志访问限制。一个工作目录仅允许一个写入进程；服务运行时通过 HTTP/网页操作，独立 CLI 会拒绝同时取得写锁。

无需模型凭证的完整夹具演示：

```powershell
uv run --no-sync evals --workspace runs/rag demo --kind rag
uv run --no-sync evals --workspace runs/harness demo --kind harness
uv run --no-sync pytest tests -q
```

夹具结果与真实应用实验分别标记。默认文本提案仅搜索应用声明的候选值；真实模型反思通过 `Plan.text_proposer` 配置隔离插件，插件返回 `changes`、实际 `cost`、`cost_source`、`pricing_version`，并受反思预算控制。候选仍需独立验证。

## 接入与 API

应用声明记录固定版本、入口、身份引用、可观测字段、可调资产、超时及费用估算。凭证放在身份引用对应的环境变量中。Python 函数接收 JSON 字典；HTTP 服务接收同样的 POST JSON；CLI 从 stdin 读取 JSON，向 stdout 输出 Observation JSON。

CLI 的 `entrypoint` 推荐填写 JSON 字符串数组，例如 `["C:/Program Files/Python/python.exe", "C:/apps/adapter.py"]`，避免 Windows 路径空格和参数引号歧义。调用不经过 shell。每次尝试保留开始/结束时间、错误与费用，失败费用未知时不会自动记零。

声明为可写的 HTTP 应用在响应缺失时暂停后续派发，同一应用的其他实验也受约束。先核对远端状态与实际费用，再通过 `POST /api/evals/experiments/{id}/recover` 提交冻结指纹、核验依据和逐笔费用。恢复时复用已经保存的原生日志，保留超时和跳过的轮次。

证据引用须包含非空来源、定位、版本及带时区的采集时间。Scout 的工具去重核对 `process.calls` 中的 `name` 和 `arguments`，仅有工具名称不会断言参数重复。`id`、`result`、`error` 保留事件和恢复依据。外部反思只能收到应用声明允许共享、且具体引用也允许共享的证据；本地证据不会因单个引用的共享标记而自动外发。

检索适配器可在 `facts.retrieval` 中记录业务标注的 `required`，以及实际观测的 `recalled`、`reranked` 和 `context`。各阶段使用可比较的文档或片段标识，保留实际顺序。省略未观测的阶段，空列表表示已观测且没有片段。Scout 对已召回但未进入上下文的必要片段定位丢失阶段，缺少重排证据时标为阶段未观测。

准备冻结方案前，通过 `POST /api/evals/calibrations` 登记业务标注校准。创建实验使用 `POST /api/evals/experiments`；提交候选、启动、控制、采用请求及回执使用同一服务入口。创建/提案/采用/回执请求需要 `Idempotency-Key`，创建可带工作目录 `If-Match` 版本。完整结构见 `src/evals_app/models.py` 与 `src/evals_app/api.py`，机器可读 API 在 `/api/openapi.json`。

可选的 `Plan.semantic_analyzer` 使用 Scout 原生模型扫描和标注验证。冻结问题、证据字段、模型实际版本、部署位置、费用来源/费率版本、覆盖率及校准门槛。创建实验后，先向 `POST /api/evals/experiments/{id}/analyzer-calibration` 提交独立的 `cases`（正例 `yes`、负例 `no`、缺失证据 `unverifiable`），通过后才能启动；网页提供相同入口和结果。校准样本不得与评测输入或业务组重叠，校准身份及数据不可覆写。每次模型生成均受分析预算、暂停和取消控制；缺失用量/费用须先核对。模型解释只作为原因假设，不代替八维业务规则。

`Plan.semantic_grader` 使用独立定义和校准标识，供非硬约束维度的 `operator: "semantic"` 规则使用。每个用例最多声明一条此类规则，并指定预期的 `yes` 或 `no`。用 `POST /api/evals/experiments/{id}/grader-calibration` 提交独立标注，或在网页提交业务评分校准。原生 Inspect 评分保留模型版本、校准标识和证据引用，费用归入 `grading`；缺失证据保留为无法核验，覆盖不足时阻断自动选优。安全及其他硬约束由确定性业务证据判定。

本地语义模型须显式使用回环地址的 Ollama/OpenAI 兼容接口；外部模型须登记凭证环境变量引用，并同时获得应用及具体证据的共享授权。仅发送冻结字段及其有效引用，缺失或未授权证据不发送。方案的 `log_retention` 当前固定为 `retain_all`，保留原日志及校准记录。

运行依赖包含 Ollama/OpenAI 兼容接口所需的客户端。Ollama 适配器接受 SDK 重试次数设置，Evals 语义调用将其设为零，失败请求的费用保留在原预留中。

无需外部模型的语义分析样例可用 `uv run --no-sync python examples/semantic_fixture.py artifacts/semantic-demo` 生成。追加 `--grader` 可生成业务评分样例。将 `manifest.json` 粘贴到网页准备实验，再将 `calibration.json` 粘贴到对应校准入口。样例使用脚本化回复，校准仅适用于夹具应用。

采用请求只代表待执行操作。执行方实际应用配置后，以匹配的版本、配置指纹和业务状态核验依据提交回执。平台据此记录生效版本；失败、部分失败和待确认分开保存。

活动评测中的原生错误样本可通过 `POST /api/evals/experiments/{id}/samples/requeue` 提交 `sample_id` 和 `epoch` 重新入队。平台核对拥有者、任务和样本身份，原内核保留先前错误；已经排队或正在运行的重复请求不再执行一次。该接口用于仍活动的原生任务，业务断言未通过仍保留其原判定。

已完成真实应用实验中的失败样本可通过 `POST /api/evals/regression-cases` 提交脱敏后的 `case`、来源实验/样本与 `redaction_note`，生成经过操作者确认的回归案例。工作目录状态的 `regression_cases` 保存案例、原日志和确认记录，重复内容不重复入库。后续方案可在案例 `metadata.regression_case_id` 引用记录；输入、规则和业务元数据改变后须重新确认。经过审阅的案例不能再用作未见留出题；用于训练或选优时，方案必须更换数据版本及独立留出输入、业务组。创建和启动均检查这些条件。

采用后观测通过 `POST /api/evals/observations` 提交 `operation_id`、新的 `plan` 和可选的 `compare_previous`。默认是描述性观测，不能确认配置收益。设为 `true` 时，在同一实际生效代码版本上对新旧配置进行配对复测，沿用原评分规则和采用门槛；应用费用分别统计，并在四个新数据分区上形成决定。这会增加对照调用消耗，仍受冻结预算约束。测试只能证明配置在登记任务中的效果，报告不据此声称生产因果收益。

搜索策略可在实验创建请求中通过 `policy` 冻结，例如 `{"version":"search-v2","use_gepa":true,"configuration_candidates":true}`。当前支持 GEPA 文本搜索开关和声明范围内的单资产配置生成；业务评分、授权与预算仍由 Plan 决定。同一策略版本不能改写其含义。省略 `policy` 和 `use_gepa` 时采用该应用当前的默认策略；显式设置 `use_gepa` 保留手动选择。

平台策略对照使用 `/api/evals/strategies` 冻结旧策略、简单规则和新策略三个实验臂。可提升的实验须绑定当前应用默认策略、`simple-rule-v1`（关闭 GEPA、生成有限配置）及声明的新策略，并冻结配对新任务和相同预算。每个实验只能加入一个策略对照。派发前汇总所属实验臂各单元的已花费和在途预留，余额不足或存在未知费用时停止新调用。报告记录独立验证收益、后续误推率上界、费用和已记录的操作者介入，缺失历史保留为空。`POST /api/evals/strategies/{id}/promote` 需要 `expected_default_version` 与幂等键，服务会重新核对最新证据。满足所有门槛的策略可作为**已验证应用范围内**后续实验的默认值。夹具验证覆盖预算和采用协议，实际策略收益仍待验证。

用 `uv run --no-sync python scripts/acceptance_strategy_study.py artifacts/strategy-study` 可执行一轮小型三组对照。该例实际运行 Inspect 和 GEPA，并检查样本量和后续观测不足时默认策略保持原值，费用采用夹具模拟费率。

离线重扫已登记的开发日志（服务停止并释放写锁后运行）：

```powershell
uv run --no-sync evals --workspace runs/rag rescan <experiment_id>
```

## 前端构建

使用固定源码要求的 Node >=22.13 和 pnpm 11.22.0：

```powershell
Set-Location engine/inspect_ai/src/inspect_ai/_view/ts-mono
pnpm install --frozen-lockfile
pnpm exec turbo run generate:css
pnpm --filter @meridianlabs/log-viewer typecheck
pnpm --filter @meridianlabs/log-viewer build
```

构建产物复制到原 `_view/dist`，随内核 Python 包发布。`engine/inspect_ai/` 保留原测试与许可证，`components/` 包含 Scout 和 GEPA 的固定源码；来源记录见 [upstream-sources.json](../upstream-sources.json)。

外部运行器在 Windows 上使用原 Inspect `eval_set` 的 selection 模式时，可显式传入 `acp_server="127.0.0.1:0"`，从 ACP 发现文件读取实际绑定端口。

发行物安装检查（先按以上步骤完成前端构建）：

```powershell
uv build engine/inspect_ai --wheel --out-dir artifacts/wheels
uv build components/inspect_scout --wheel --out-dir artifacts/wheels
uv build components/gepa --wheel --out-dir artifacts/wheels
uv build --wheel --out-dir artifacts/wheels
uv venv artifacts/clean-install --python 3.12
uv pip install --python artifacts/clean-install/Scripts/python.exe --find-links artifacts/wheels artifacts/wheels/evals_app-0.1.0-py3-none-any.whl
uv pip check --python artifacts/clean-install/Scripts/python.exe
artifacts/clean-install/Scripts/python.exe -I scripts/verify_install.py artifacts/installed-check
uv run --no-sync python examples/semantic_fixture.py artifacts/semantic-demo
artifacts/clean-install/Scripts/python.exe -I scripts/verify_semantic_install.py artifacts/installed-semantic-check artifacts/semantic-demo/manifest.json artifacts/semantic-demo/calibration.json
```

检查脚本核验所有组件来自安装环境，并运行原 Inspect、八维评分和 Scout 离线重扫。其结果属于确定性安装回归。

## 真实应用的本机隔离验收

用户选定 VeraAgent 与 NexRAG。脚本在现有项目的虚拟环境中启动临时适配服务，调用实际应用代码，以临时数据和确定性依赖验证链路，不读取生产业务数据库或用户模型配置：

```powershell
uv run --no-sync python scripts/acceptance_applications.py --kind vera --source C:/projects/VeraAgent
uv run --no-sync python scripts/acceptance_applications.py --kind nexrag --source C:/projects/NexRAG
```

在同一隔离约定下，可以运行安全、故障和候选反例专项。NexRAG 主应用专项使用项目根目录的 `.venv`，检索反例使用其检索组件的 `.venv`。

```powershell
uv run --no-sync python scripts/acceptance_boundaries.py --kind nexrag --source C:/projects/NexRAG
uv run --no-sync python scripts/acceptance_applications.py --kind nexrag --source C:/projects/NexRAG --targeted-counterexamples
uv run --no-sync python scripts/acceptance_applications.py --kind nexrag --source C:/projects/NexRAG --rerank-stages
uv run --no-sync python scripts/acceptance_boundaries.py --kind vera --source C:/projects/VeraAgent --provider-retry
uv run --no-sync python scripts/acceptance_boundaries.py --kind vera --source C:/projects/VeraAgent --compare-tool-budget
uv run --no-sync python scripts/acceptance_prompt_rule.py --source C:/projects/VeraAgent
```

本机测试使用实际应用代码及生成业务案例，模型回复/查询向量是测试替身；实际模型质量和持续生产收益仍需新任务与后续观测。

独立推荐后，可运行 `scripts/acceptance_lifecycle.py --kind <vera|nexrag> --source <应用目录> --acceptance <本次 artifacts/applications-... 目录>`，在同一本机隔离网关中演练实际配置采用、生效核验、新任务观测与回滚。增加 `--paired-control` 会执行新旧配置对照并核对收益结论。两个应用仓库不作代码、配置或依赖修改。

## 真实模型验收

仅在明确授权模型调用后运行。将自己的临时凭证配置到进程环境变量 `DEEPSEEK_API_KEY`，模型由 `EVALS_LIVE_MODEL` 指定。切勿将密钥写入实验 JSON 或提交到仓库。

```powershell
uv run --no-sync python scripts/acceptance_live.py --kind vera --source C:/projects/VeraAgent
uv run --no-sync python scripts/acceptance_live.py --kind nexrag --source C:/projects/NexRAG
```

每个应用预检 1 次、基线 4 轮；每题最多 6 次模型请求。费用采用脚本中标注日期的费率快照，复用前必须核对当前供应商定价。该脚本使用生成业务数据；真实检索服务和生产数据不在其验证范围内。
