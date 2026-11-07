# 维护与贡献

使用 Python 3.12、uv、Node 22.13 以上及 pnpm 11.22.0。安装、构建和验收命令见 [使用说明](docs/usage.md)。

- 自有 Python 代码放在 `src/evals_app`。函数参数和返回值标注类型；名称描述实际行为。
- 自有代码的注释和 docstring 统一英文，说明约束、失败语义和设计原因。用户界面及主要文档使用中文。继承组件保留原有语言和版权。
- 实验编排由 `service` 负责，进程和预算由 `runtime` 负责；配置生效由 `adoption` 负责。策略对照放在 `strategy_studies`，避免重新塞入编排器。
- 前端网络状态使用 React Query；只读展示与操作控制分开。未知 JSON 先收窄类型，不用强制转换掩盖错误。
- 复用 Inspect、Scout、GEPA 的能力；修改继承代码时保留来源，记录必要的行为差异。
- 修复需有具体触发条件，补充能防止复发的测试。格式整理不引入逻辑变更，不为了行数增加抽象层。
- 不提交凭证、模型真实业务数据、运行日志、虚拟环境、依赖缓存和临时构建记录。

提交前执行：

```powershell
uv run --no-sync ruff check src tests scripts examples
uv run --no-sync ruff format --check src tests scripts examples
uv run --no-sync pytest
```

前端修改还需在 `engine/inspect_ai/src/inspect_ai/_view/ts-mono` 运行 `pnpm check`、`pnpm test` 和 `pnpm --filter @meridianlabs/log-viewer build`。真实模型验收需显式配置自己的模型和预算，CI 不使用付费模型。

提交按可审阅的主题组织；Release 对应已通过 CI 的明确提交。漏洞请通过 GitHub 私密漏洞报告提交，勿在公开 issue 中粘贴密钥或真实数据。
