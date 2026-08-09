# Boss 求职助手 Win-Web 环境部署包

此目录用于给全新的 Windows 10/11 x64 电脑安装或复用运行环境。它不包含 Python，也不包含项目源码；正式程序由父目录中的两个 Nuitka EXE 提供。

推荐流程：先把整个 `requests-packages` 复制到目标父目录并双击 `一键部署.cmd`，部署完成后再把 `Boss求职助手.exe` 和 `Boss登录浏览器.exe` 复制到同一父目录。

最终结构：

```text
目标目录\
  Boss求职助手.exe
  Boss登录浏览器.exe
  config\
    gui_defaults.txt
    model_api.local.json
  resume_inbox\
  requests-packages\
```

## 最快用法

1. 双击 `验证安装包.cmd`。
2. 双击 `一键部署.cmd`，接受管理员权限提示。入口会先复用现有 PowerShell 7；未检测到时校验并解压官方 7.6.4 LTS 离线包，再用 `pwsh.exe` 执行正式部署。可指定位置的软件默认跟随 `一键部署.cmd` 所在盘符。
3. 新装 MySQL 时按提示输入并确认至少 8 位的 root 密码；输入不会回显，密码不会写入模板或日志。
4. 编辑父目录 `config\gui_defaults.txt` 和 `config\model_api.local.json`，填写当前电脑自己的凭据。
5. 如使用 Codex 主导模式，双击 `安装Codex（可选）.cmd`。已有 Codex 会直接复用，不会降级或覆盖；登录仍由用户本人完成。
6. 将两个 EXE 放到父目录，双击 `验证环境.cmd` 后按《首次使用向导》继续。

部署和验证阶段不会打开 Boss、调用模型、填写草稿或发送消息/简历。

## 随包组件

| 组件 | 随包版本 | 处理规则 |
| --- | --- | --- |
| Microsoft PowerShell x64 LTS | 7.6.4 | 检测到任意 PowerShell 7 即跳过；没有时离线静默安装 |
| Microsoft Edge Enterprise x64 | 151.0.4129.59 | 检测到任意现有 Edge 即跳过 |
| Microsoft Visual C++ x64 Runtime | 14.50.35719.0 | 检测到现有 x64 运行库即跳过 |
| MySQL Community Server | 8.0.36 | 检测到 MySQL 服务、3306 占用或本包实例即跳过，不改现有数据库 |
| Navicat Premium x64 | 17.3.11 | 检测到任意 Navicat 即跳过；可用 `-SkipNavicat` 取消安装 |
| Codex CLI Windows x64 | 0.133.0 | 独立可选安装；PATH 或默认目录已有版本即复用 |

清单共 6 项，总大小 826,296,129 字节（788.02 MiB）。文件来源、大小和 SHA-256 见 `manifest.json` 与 `SHA256SUMS.txt`。本包不含 Python、wheelhouse、ADB 或 Android 组件。

Navicat 是可选查看工具，随包只含官方未修改安装器，不含许可证、激活状态、连接或密码。

## 安装磁盘与位置

脚本从 `requests-packages` 的实际路径读取盘符。例如一键部署文件位于 `D:\某目录\requests-packages\一键部署.cmd`，可指定位置的软件会使用以下目录：

```text
D:\BossJobAssistant\PowerShell\7\
D:\BossJobAssistant\MySQL\
D:\BossJobAssistant\Navicat\17.3.11\
```

- PowerShell 7 ZIP、MySQL 程序和数据、Navicat 跟随一键部署文件所在的本机磁盘。
- Edge 和 VC++ 运行库属于系统管理组件，继续由官方安装器使用 Windows 默认位置，通常在 C 盘。
- 已检测到的现有软件保持原位置，不迁移、不覆盖、不降级。
- `config`、`resume_inbox` 和日志仍按本文所述创建在部署目录中。
- Codex 是独立可选安装，不属于一键部署；仍安装到当前 Windows 用户目录，以便保留用户级登录和权限边界。
- 为保证 MySQL Windows 服务可靠运行，请先把 `requests-packages` 复制到带盘符的本机磁盘；不从 UNC 网络共享直接部署。

## 一键部署会做什么

1. 先检测 PowerShell 7；不存在时校验并离线安装随包的 7.6.4 LTS 到当前部署盘，然后只用 PowerShell 7 执行正式部署。
2. 校验全部离线安装资源。
3. 安装或复用 Edge、VC++ x64 运行库、MySQL 和 Navicat；其中 MySQL 和 Navicat 新安装时跟随当前部署盘。
4. 新装 MySQL 时仅监听 `127.0.0.1:3306`，创建 `boss_job_assistant` 数据库，并要求用户现场设置密码。
5. 在 `requests-packages` 的父目录创建 `config` 和 `resume_inbox`。
6. 仅在配置不存在时复制脱敏模板；已有配置绝不覆盖。

模板中的 API Key、MySQL 用户名和密码故意为空；API 模型保留为 `deepseek-v4-flash`。本包不会复制 Boss/Codex 登录态、真实简历、MySQL 数据、Edge Profile 或 Navicat 许可证。

## Codex 检测和模型

`安装Codex（可选）.cmd` 与程序都会先查找 PATH 中的 `codex`，再查找 `%LOCALAPPDATA%\BossJobAssistant\Codex\*\codex.exe`。找到后执行版本与 `codex login status` 检查：

- 已安装且已登录：直接使用。
- 已安装但未登录：明确提示执行 `codex login`。
- 未安装：可从随包 ZIP 安装 0.133.0；不会自动替用户登录。

项目的 Codex 主导模式固定使用 `gpt-5.5`。大模型 API 模式则使用 `config\model_api.local.json` 中实际填写的模型。

## 可选参数

```powershell
pwsh -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipMySql
pwsh -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipNavicat
pwsh -ExecutionPolicy Bypass -File .\requests-packages\scripts\Setup.ps1 -SkipEdge
```

## 生成无源码分发目录

先完成 Nuitka 构建，再执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\requests-packages\scripts\Build-Distribution.ps1 -Destination D:\Boss-AI-Job-Agent-Win-Offline
```

目标必须是不存在且位于当前项目外的新目录。脚本只复制两个 EXE 和 `requests-packages`，并扫描 `.py/.pyc/.pdb/.c/.h`、本机配置、PDF、数据库、缓存和日志等不应分发的内容。源码项目本身不会被修改。

2026-08-08 已在初始未安装 PowerShell 7 的干净 Windows Sandbox 完成一键部署验收：入口成功使用随包 ZIP 安装 PowerShell 7.6.4，环境部署完成，Navicat 可正常启动并成功连接新部署的 MySQL 数据库。验证未使用真实 API Key、Codex/Boss 登录信息或简历，也未执行投递。
