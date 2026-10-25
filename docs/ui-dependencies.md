# 桌面 Markdown 预览依赖

核心历史 CLI、MCP、导入和回收站继续使用 Python 标准库。桌面 UI 使用 Tkinter；Markdown 语法解析和代码高亮通过下面的依赖完成，在现有 Text 控件中直接绘制，不使用 HTML／JavaScript 或浏览器控件。

使用启动 UI 的同一个 Python 安装：

```shell
python -m pip install -r requirements-ui.txt
```

安装器在修改配置前检查 Python 3.11+、Tk 和下列桌面模块。直接从源码运行时，缺少解析／高亮依赖会回退到原文模式；只有公式绘制依赖缺失时，Markdown 仍可阅读，公式保留原式。安装后重新打开查看器即可生效。

| 组件 | 用途 | 许可 | 早期 Windows 验证 | beta 双平台新 venv |
|---|---|---|---|---|
| markdown-it-py | CommonMark 语法解析，启用表格和删除线扩展 | MIT | 2.2.0 | 4.2.0 |
| Pygments | 按代码块语言生成高亮片段 | BSD-2-Clause | 2.15.1 | 2.21.0 |
| mdurl | markdown-it-py 的 URL 解析依赖，由 pip 自动安装 | MIT | 0.1.0 | 0.1.2 |
| mdit-py-cjk-friendly | 中文标点相邻的加粗／斜体解析 | MIT | — | — |
| mdit-py-plugins | `$`／`$$` 公式解析 | MIT | — | — |
| Ziamath、ziafont、latex2mathml | 将 TeX 转为 MathML 并以数学字体绘制 SVG 路径 | MIT | — | — |
| resvg-py / resvg | 本机将生成的 SVG 路径转为 PNG | MIT / MPL-2.0 | — | — |
| STIX Two Math（Ziamath 随包提供） | 数学字体 | SIL OFL-1.1 | — | — |

解析接口见 [markdown-it-py 文档](https://markdown-it-py.readthedocs.io/en/latest/using.html)，高亮接口见 [Pygments 文档](https://pygments.org/docs/quickstart/)。中文扩展见 [mdit-py-cjk-friendly](https://pypi.org/project/mdit-py-cjk-friendly/)，数学接口见 [mdit-py-plugins](https://mdit-py-plugins.readthedocs.io/en/latest/)、[Ziamath](https://ziamath.readthedocs.io/en/latest/) 和 [resvg-py](https://resvg-py.readthedocs.io/en/latest/api.html)。许可按随包附带的 LICENSE 核对；markdown-it-py 另包含上游 markdown-it 的 MIT 许可。

数学预览改动在 Windows 当前环境验证 markdown-it-py 2.2.0、Pygments 2.15.1、mdit-py-cjk-friendly 0.3.0、mdit-py-plugins 0.6.1、Ziamath 0.13、ziafont 0.11、latex2mathml 3.81.1 和 resvg-py 0.5.0。由于中文扩展要求 markdown-it-py <4，安装范围收窄为 2.2 至 3.x；此前 beta 的 4.2.0 记录仅代表当时版本。具体依赖边界见 requirements-ui.txt，范围内其他版本仍需运行项目检查。

另在全新 Windows venv 中安装 requirements-ui.txt，验证 markdown-it-py 3.0.0、Pygments 2.21.0 和 mdurl 0.1.2，其余新增组件与上述版本一致；解析、真实 Tk 阅读／复制／搜索和安装器预检通过。这不等同于全新操作系统验收。

源码发行只声明依赖，不复制这些包到仓库。未来制作含运行环境的便携包或安装包时，需包含实际分发组件的许可与版权声明，验证 Python／Tk／字体、上述模块及 Pygments 语言词法器是否完整收集。项目采用 MIT 协议，允许商业及非商业使用、修改和分发；第三方组件仍使用自己的许可证。详见 [LICENSE](../LICENSE) 和 [第三方清单](../NOTICE.md)。

Markdown 配置关闭 HTML 解析及自动资源加载。图片显示替代说明，HTTP(S) 与邮件链接只在用户点击后交给浏览器；相对路径和其他 URI 不作为命令执行。每条消息分别解析，代码复制保留解析后的代码内容，不附加围栏、语言标签或控件文字。

支持 `$…$`、`$$…$$`、`\(…\)`、`\[…\]` 以及标为 math／latex／tex 的代码围栏，包含分式、上下标、求和、矩阵和 aligned 多行对齐。公式解析保留加粗、列表、引用和表格上下文；转义、普通代码围栏和行内代码不转换。中文加粗由解析器扩展识别，不改写会话正文；软换行继续使用原来的 Tk 显示规则。

公式在后台生成 PNG，不需要浏览器、Node、系统 LaTeX 或在线服务。只栅格化数学库生成的路径，不接受外部 SVG／图片／字体资源。块公式使用“复制公式”，行内公式点击或聚焦后回车可复制 TeX；选中正文按 Ctrl+C 同样保留公式标记。对话内搜索包含公式源码，并突出显示匹配公式。切换原文可查看完整消息。

这不是完整的 LaTeX 文档引擎：超出库支持的命令、含当前数学字体未覆盖的中日韩文字、过长或过大的公式保留原式；块公式附原因，不中断后续正文。单式最多 4000 字符、64 层大括号嵌套、4096×1024 且不超过 100 万像素；PNG 缓存最多 128 项／8 MB，一个阅读窗口最多 800 万公式像素。缓存仅在内存中，不写会话、数据库或磁盘图片。

Markdown 解析缓存最多保留 128 条、每条不超过 20 万字符的消息。超过 20 万字符的代码仍可阅读和复制，但不做语法高亮；没有声明语言或未知语言也按等宽原文显示。宽表格与代码使用原生文本标签及横向滚动，内容仍支持文本选择和搜索。Markdown／原文切换按消息及相对阅读位置恢复，不承诺两种排版的屏幕行完全一致。
