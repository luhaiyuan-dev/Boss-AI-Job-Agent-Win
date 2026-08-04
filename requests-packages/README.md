# Boss 求职助手 Win-Web 离线部署包

此目录面向全新的 Windows 10/11 x64 电脑。保持整个项目目录结构不变，双击 `一键部署.cmd`，即可离线安装 Win-Web 项目真正需要的电脑环境，不必再分别寻找 Python、Edge、MySQL、Navicat、Codex 或 Python 依赖。

`requests-packages` 名称沿用 Android 版项目，便于两个项目保持一致的部署入口。此 Win-Web 包不含 ADB、Platform-Tools、ADBKeyBoard 或任何 Android 组件。

## 最快用法

1. 双击 `验证安装包.cmd`，先确认全部文件大小和 SHA-256 正常。
2. 双击 `一键部署.cmd`，接受管理员权限提示。
3. 部署末尾会分别询问是否配置 API Key、是否安装并登录 Codex；两项都能直接回车跳过，窗口会显示稍后配置的绝对文件路径。
4. 双击 `验证环境.cmd`，按提示补齐个人配置。
5. 双击 `打开Boss登录Edge.cmd`，在项目专用 Edge 中手动登录 Boss直聘并保持窗口开启。
6. 在 `resume_inbox/` 中只放一份带文本层 PDF 简历。
7. 双击 `启动Boss助手.cmd`。首次验证建议选择“仅填充不发送”，目标公司数设为 1。

部署脚本不会启动 Boss 自动化、不会打开职位、不会填写草稿，也不会发送消息或简历。

## 随包组件

| 组件 | 版本 | 是否运行必需 | 用途 |
| --- | --- | --- | --- |
| Python x64 | 3.13.14 | 是 | Tkinter GUI 与项目运行时 |
| Python wheelhouse | 与本机锁定版本一致 | 是 | Selenium 类型兼容、原生 CDP WebSocket、MySQL、PDF、OpenCC 等完整依赖 |
| Microsoft Edge Enterprise x64 | 151.0.4129.59 | 是 | Boss Web 登录与项目原生 CDP 通道 |
| MySQL Community Server | 8.0.36 | 是，或使用已有实例 | 运行记录、投递结果、30天去重和统计 |
| Microsoft Visual C++ x64 Runtime | 14.50.35719.0 | MySQL 新安装时需要 | MySQL Windows 运行库 |
| Navicat Premium x64 | 17.3.11 | 否 | 可视化查看 MySQL；官方试用安装器，不含许可证 |
| Codex CLI Windows x64 | 0.133.0 | 仅 Codex主导模式需要 | 离线安装原生 CLI，登录仍由用户本人完成 |

Navicat 注册表卸载项可能仍显示 17.0.4，但本机实际 `navicat.exe` 文件版本是 17.3.11.0，因此随包按实际程序版本 17.3.11 整理。Navicat 不是脚本运行依赖。

完整文件来源、大小和 SHA-256 见 `manifest.json` 与 `SHA256SUMS.txt`。安装前 `Verify-Packages.ps1` 会逐项校验，不通过就停止。

当前清单共 34 项，总大小 773,466,055 字节（737.63 MiB）。

## 一键部署会做什么

1. 校验离线包。
2. 安装或复用 Python 3.13.14 x64，并验证 Tcl/Tk。
3. 在项目根目录创建 `.venv/`，只从 `wheelhouse/` 安装锁定依赖。
4. 安装或复用 Microsoft Edge。检测到其它版本时不会自动降级。
5. 若没有 MySQL/3306 冲突，部署 MySQL 8.0.36 为 `BossJobAssistantMySQL` 服务；检测到已有实例时保留现状。
6. 可选安装 Navicat，不复制许可证、激活状态、连接或密码。
7. 缺少本机配置时，从脱敏模板创建 `config/gui_defaults.txt` 与 `config/model_api.local.json`；已存在时绝不覆盖。
8. 分别询问 API Key 和 Codex 安装/登录，均允许跳过。

## 部署落点

- Python 虚拟环境：项目根目录 `.venv/`
- MySQL 程序：`%ProgramData%\BossJobAssistant\MySQL\mysql-8.0.36-winx64\`
- MySQL 数据：`%ProgramData%\BossJobAssistant\MySQL\data\`
- MySQL 服务：`BossJobAssistantMySQL`
- MySQL 网络：只监听 `127.0.0.1:3306`
- 新安装 MySQL 默认本机凭据：`root/root`，数据库 `boss_job_assistant`
- Codex：`%LOCALAPPDATA%\BossJobAssistant\Codex\0.133.0\`
- API 配置：项目根目录 `config\model_api.local.json`
- API 模板：项目根目录 `config\model_api.example.json`
- GUI/MySQL 配置：项目根目录 `config\gui_defaults.txt`
- Edge 登录资料：项目根目录 `data\edge_profile_boss\`，首次登录后才生成
- 安装日志：`requests-packages/logs/`

`root/root` 只用于绑定回环地址的个人电脑，并与本项目当前本机配置保持一致。不要把 3306 开放到局域网或互联网；如修改密码，要同步更新 `config/gui_defaults.txt` 或 GUI。

## API Key 和 Codex 都可以跳过

大模型 API 与 Codex 是二选一的审核方式，部署时可以先全部跳过：

- API：稍后编辑 `项目根目录\config\model_api.local.json`。程序使用其中实际填写的 `model`，不会替换成固定模型；推荐通过 `api_key_env` 引用环境变量。
- Codex：稍后运行 `requests-packages\scripts\Install-Codex-Optional.ps1`，安装后执行 `codex login`。登录状态在当前 Windows 用户目录，不在项目文件夹。

两者都跳过不影响电脑环境部署和 GUI 启动，但点击“开始”进入模型审核前，必须至少完成其中一种。

## 可选参数

从管理员 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipMySql
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipNavicat
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipEdge
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipApiConfig
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipCodex
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipAiSetup
```

`-SkipAiSetup` 等同于同时跳过 API 配置交互和 Codex 安装/登录。

## 分发项目时必须脱敏

当前开发目录可能含真实 API Key、MySQL 密码、简历、运行日志和 Boss/Edge 登录资料。不要直接压缩整个开发目录发给新用户。可在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Build-Distribution.ps1 -Destination D:\Boss-AI-Job-Agent-Win-Offline
```

脚本只创建一个不存在的新目录，并排除 `.venv`、`data`、本机配置、PDF、缓存和日志；不会修改当前开发目录。生成后仍应人工检查再分发。

## 不能随包预装的个人状态

- Boss 账号登录状态；必须由新用户在项目专用 Edge 手动登录。
- API Key、Codex 登录、MySQL 旧实例密码。
- 真实简历、历史投递记录、SQLite/MySQL 数据和 Edge 用户资料。
- Navicat 许可证、激活信息与保存的连接。

完整首次使用顺序见 `首次使用向导.md`。完整一键安装仍应在一台干净 Windows 电脑上做最终验收；在已有开发环境上运行校验不能替代干净机安装证明。
