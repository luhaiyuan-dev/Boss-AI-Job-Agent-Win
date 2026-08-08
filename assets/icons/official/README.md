# 正式应用图标

此目录保存方案 A 的长期正式资产：

- `boss_assistant.png` / `.ico`：Boss求职助手，四向 AI 罗盘与智能星芒。
- `boss_login.png` / `.ico`：Boss登录浏览器，连接环与双节点。
- `concept-a-preview.png`：用户选定的双图标概念预览。

两张 PNG 使用白色圆角底板和透明外角；ICO 由 `tools/make_icons.py` 从 PNG
生成，包含 16、24、32、48、64、128、256 像素七种尺寸。构建脚本只从本目录
读取正式图标，避免后续打包继续使用旧资源。

生成方式：Codex 内置 Image Gen。参考图仅用于蓝、青、橙、红的配色语言，图案
为重新设计，不含文字、公文包或参考图原符号。
