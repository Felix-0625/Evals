# Evals

基于 Inspect AI 的外部 AI 应用评测与受控配置改进平台。接入 Agent、Harness 或 RAG，保留逐题证据，对比候选配置，经独立验证后记录采用、回执和回滚。

运行内核、原生日志和 Viewer 来自 Inspect AI，轨迹分析复用 Inspect Scout，文本候选搜索复用 GEPA。Evals 自有代码负责业务协议、八维评分、预算、独立验证和版本治理。来源与修改边界见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 本机运行

支持 Windows、Python 3.12，需要 uv。前端发行构建已包含在仓库中，首次运行无需 Node。

```powershell
git clone https://github.com/Felix-0625/Evals.git
cd Evals
uv sync --locked
uv run --no-sync evals --workspace evals-workspace serve
```

打开 <http://127.0.0.1:7575/#/evals>。当前版本面向本机单可信操作者，工作目录使用排他写锁。

无需模型凭证的演示：

```powershell
uv run --no-sync evals --workspace runs/rag demo --kind rag
uv run --no-sync evals --workspace runs/rag serve
```

演示使用确定性夹具；真实应用使用自己的版本、适配器、证据与环境变量凭证。

## 文档

- [使用、接入、API 与构建](docs/usage.md)
- [评测与 RSI：流程、门槛和证据边界](docs/evaluation-and-rsi.md)
- [架构与维护入口](docs/architecture.md)
- [验收状态](docs/implementation-status.md) · [发布检查](docs/release-audit.md)
- [贡献规范](CONTRIBUTING.md) · [安全范围](SECURITY.md) · [许可证与字体](THIRD_PARTY_NOTICES.md)
- 原始设计：[Guide](Guide.md)、[Plan](Plan.md)、[development](development.md)

## 当前验证范围

已完成隔离回归与真实 DeepSeek 模型基线调用：VeraAgent 临时代码沙箱、NexRAG 原生知识工具与证据循环各 4 轮。真实模型验收使用生成数据，保留失败与重跑记录；长期稳定性、生产负载和持续 RSI 收益尚未验证。`completed` 表示执行结束，业务检查与推荐结论单独记录。

## 仓库结构

```text
src/evals_app/           自有协议、编排、评分与治理
tests/                  自有回归测试
examples/               夹具与隔离接入示例
scripts/                验收、安装与许可工具
engine/inspect_ai/      保留来源的派生内核及前端
components/             固定版本 Scout 与 GEPA
docs/                   使用、架构、评测与发布资料
```

自有代码采用 MIT，继承组件和依赖保留各自版权及许可，见 [LICENSE](LICENSE) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。
