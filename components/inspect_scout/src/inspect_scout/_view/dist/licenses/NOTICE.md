# 第三方来源与许可

Evals 自有代码采用 [MIT](LICENSE)。继承源码仍属于其原作者，保留各目录的版权和许可证：

| 组件 | 位置 | 许可 |
| --- | --- | --- |
| Inspect AI | `engine/inspect_ai` | MIT |
| Inspect Scout | `components/inspect_scout` | MIT |
| GEPA | `components/gepa` | MIT |
| Inspect View ts-mono | 内核 `_view/ts-mono` | MIT |

固定提交见 [来源记录](upstream-sources.json)。Evals 自有部分负责外部应用协议、业务评分、预算与版本治理、独立验证及 RSI 流程；运行内核、日志、Viewer、Scout 与 GEPA 能力来自上述组件。

## 前端字体

正文使用系统字体栈，不分发系统字体文件。发行包保留的图标字体：

- Bootstrap Icons：The Bootstrap Authors，MIT；保留原字体文件及许可。[原始许可](https://github.com/twbs/icons/blob/main/LICENSE)。
- Codicons：Microsoft，CC BY 4.0；字体未修改，保留署名及许可链接。[原始许可](https://github.com/microsoft/vscode-codicons/blob/main/LICENSE)、[CC BY 4.0 条件](https://creativecommons.org/licenses/by/4.0/)。该许可允许商用，要求署名、保留许可并说明修改。

开发工具携带的 Inter 和 Bricolage Grotesque 已从生产构建排除。`bootstrap-icons.woff` 是 woff2 的兼容回退，并非另一套未用字体。构建产物中的图标字体仍用于原 Viewer。

## 依赖清单与再分发

[依赖清单](docs/licenses/inventory.json) 和 [原始声明汇编](docs/licenses/THIRD_PARTY_LICENSES.txt) 由锁定环境生成，包含运行和开发工具依赖。MIT、Apache、BSD、ISC 等依赖仍保留各自条款；CC BY 依赖保留署名；MPL 组件保留文件级条款及源码来源，不能统一宣称所有第三方代码都属于 Evals 的 MIT。

重复生成：在前端运行 `pnpm licenses list --json > <清单路径>`，然后在根目录运行 `uv run --no-sync python scripts/license_inventory.py <清单路径>`。发行源码保留原始上游许可；Viewer 二进制附带 notices。
