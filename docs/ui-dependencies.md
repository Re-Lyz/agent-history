# 桌面 Markdown 预览依赖

核心历史 CLI、MCP、导入和回收站继续使用 Python 标准库。桌面 UI 使用 Tkinter；Markdown 语法解析和代码高亮通过下面的依赖完成，在现有 Text 控件中直接绘制，不使用 HTML／JavaScript 或浏览器控件。

使用启动 UI 的同一个 Python 安装：

```shell
python -m pip install -r requirements-ui.txt
```

`integrations/install.ps1` 在修改配置前检查 Python 3.11+、Tk 和这两个模块。直接从源码运行时，缺少依赖会回退到原文模式；安装后重新打开查看器即可选择 Markdown。

| 组件 | 用途 | 许可 | 早期 Windows 验证 | beta 双平台新 venv |
|---|---|---|---|---|
| markdown-it-py | CommonMark 语法解析，启用表格和删除线扩展 | MIT | 2.2.0 | 4.2.0 |
| Pygments | 按代码块语言生成高亮片段 | BSD-2-Clause | 2.15.1 | 2.21.0 |
| mdurl | markdown-it-py 的 URL 解析依赖，由 pip 自动安装 | MIT | 0.1.0 | 0.1.2 |

解析接口见 [markdown-it-py 文档](https://markdown-it-py.readthedocs.io/en/latest/using.html)，高亮接口见 [Pygments 文档](https://pygments.org/docs/quickstart/)。许可按上述验证版本随包附带的 LICENSE 核对；markdown-it-py 另包含上游 markdown-it 的 MIT 许可。安装范围为 markdown-it-py 2.2 至 4.x、Pygments 2.15 至 2.x；表中版本是此次实际验证环境，范围内其他版本仍需运行项目检查。

源码发行只声明依赖，不复制这些包到仓库。未来制作含运行环境的便携包或安装包时，需包含实际分发组件的许可与版权声明，验证 Python／Tk／字体、上述模块及 Pygments 语言词法器是否完整收集。项目采用 MIT 协议，允许商业及非商业使用、修改和分发；第三方组件仍使用自己的许可证。详见 [LICENSE](../LICENSE) 和 [第三方清单](../NOTICE.md)。

Markdown 配置关闭 HTML 解析及自动资源加载。图片显示替代说明，HTTP(S) 与邮件链接只在用户点击后交给浏览器；相对路径和其他 URI 不作为命令执行。每条消息分别解析，代码复制保留解析后的代码内容，不附加围栏、语言标签或控件文字。

解析缓存最多保留 128 条、每条不超过 20 万字符的消息。超过 20 万字符的代码仍可阅读和复制，但不做语法高亮；没有声明语言或未知语言也按等宽原文显示。宽表格与代码使用原生文本标签及横向滚动，内容仍支持文本选择和搜索。Markdown／原文切换按消息及相对阅读位置恢复，不承诺两种排版的屏幕行完全一致。
