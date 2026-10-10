# 合成截图

`images/demo.png` 是实际 Tk 桌面 UI 的列表与 Markdown 区域截图，不是效果图。历史文字、会话 ID、连接名称、项目位置及显示配置均由 `tests/make_demo_screenshot.py` 在临时目录生成；不读取个人会话、不登录、不执行 Agent。右侧启动预览包含临时运行路径，所以截图裁掉该区域；不捕获桌面和其他程序。分享前人工检查窗口中的来源路径和日志。

使用有桌面的 Windows Python（额外安装开发依赖 Pillow）在仓库根目录生成：

```text
python tests/make_demo_screenshot.py
```

默认写入 `docs/images/demo.png`。Pillow 不属于产品运行依赖，也不打入源码发行包。截图生成脚本会临时显示窗口，完成后关闭；界面中文，英文 README 也展示同一真实界面。没有桌面时不要生成或假称完成视觉验证。
