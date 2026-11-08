# 0.1.0 发布检查

发布范围：GitHub 公开源码与 Release，应用保留本机运行。支持 Windows、单可信操作者和本地文件存储。

## 检查与修复

| 用户要求 | 结果与范围 |
| --- | --- |
| 真实使用验收 | 真实 `deepseek-flash`；VeraAgent 和 NexRAG 各预检 1 次、基线 4 轮通过。生成任务/资料，不使用生产数据库；[脱敏记录](evidence/live-acceptance.json) 保留源代码身份、费用与失败重跑。 |
| 漏洞扫描 | Python 已安装依赖扫描无已知漏洞；自有/派生本地包另行源码检查。前端最初 4 条中危公告，修复后官方 npm 审计无已知公告。不是完整继承源码安全认证。 |
| 逻辑与边界 | 修复经验条目类型错误导致的 500 与错误启用状态。保留费用未知、写操作状态不明、版本冲突和证据不足的阻断行为。 |
| 前端清理 | 开发面板按需加载，生产包移除 Inter/Bricolage 字体。独立逐运行证据组件；原 Viewer 和 Scout 共享组件仍有使用。临时构建记录不提交。 |
| 字体与许可 | 实际发行字体为 Bootstrap Icons MIT 与 Microsoft Codicons CC BY 4.0；署名和原始许可随包。系统字体未复制分发。依赖包含文件级 MPL 许可，保留对应条款和来源。 |
| 后端路由 | 核对页面、CLI、接入及诊断用途；没有证据确认可以删除的失效自有路由，保留公开本机 API 契约。 |
| 维护与体积 | 拆出 adoption、strategy_studies 和 RunEvidence；自有注释英文、主要文档中文。补充关键约束和函数类型，未引入新的通用工作流层。 |
| 仓库与示例 | 短 README、便携路径、示例分类、贡献规范、安全范围、MIT 及第三方声明；排除凭证、日志、环境和缓存。 |
| 评测与 RSI | [独立章节](evaluation-and-rsi.md) 说明数据分区、评分、选优、采用回执、后续观测和策略提升边界。 |

四条前端公告及修复版本依据：
[Seroval typed array](https://github.com/advisories/GHSA-f86p-h4j2-7qgj)、
[Seroval RegExp](https://github.com/advisories/GHSA-9wx9-5pv9-3349)、
[glob](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4)、
[minimatch](https://github.com/advisories/GHSA-g3hv-75wg-m249)。
采用发布修复版本，未自行编写依赖漏洞补丁；import-x 4.18 支持 minimatch 11，保留显式的安全升级冷却期例外。

## 复验记录

本机门禁通过了 105 项产品回归、6,226 项前端测试、36 项前端静态任务及 Python 格式检查。四个 wheel 在独立环境安装，103 个运行依赖兼容。安装后 8 轮原生评测与 8 条 Scout 重扫发现核对通过。浏览器从安装版创建实验并启动，刷新后重新打开已完成实验，核对独立推荐和安全候选拒绝结果。

远程 CI 暴露了干净检出中的两处差异。导入排序改用显式的第一方、第三方配置。Viewer 的 12 个原版兼容性测试样本恢复为受跟踪文件，仓库继续忽略运行产生的日志。最终远程结果见 [GitHub Actions](https://github.com/Felix-0625/Evals/actions/workflows/ci.yml)。

回归初次出现的超时、区域格式断言错误，以及实模运行中的失败样本均保留在本机 `artifacts/`。公开仓库提供脱敏证据和结论。

## 发布边界

首版不承诺生产负载稳定性或长期无人值守改进。真实模型基线通过，不等于真实模型候选提升或持续 RSI 收益已验证。发行轮子依赖同一 Release 中的三个派生组件轮子；安装需使用 `--find-links` 指向该目录。源码安装推荐 `uv sync --locked`。
