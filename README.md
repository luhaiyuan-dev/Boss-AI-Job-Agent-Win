# Boss 求职助手控制台（Windows Web）

当前版本：`0.1.9`

本项目通过登录专用 Microsoft Edge 的最小原生 CDP 通道读取 Boss直聘 Web 页面，结合本地 PDF 简历和大模型审核岗位，并在 Tkinter 控制台中完成筛选、招呼语生成、受控填充/发送、未读消息巡检、断点保存和结果统计。

项目不会使用 Selenium/Playwright 驱动接管现有登录页；Selenium 依赖仅保留类型兼容。浏览器操作由项目自己的 CDP WebSocket 封装完成，避免向页面注入 WebDriver 标记。

## 主要能力

- 从 Boss 职位页读取一条或多条求职意向，可锁定单条意向或按页面顺序平均分配目标公司数。
- 按公司、岗位方向、排除方向、城市、薪资闭区间、公司规模、经验和周末休息条件筛选。
- 使用“大模型API”或“Codex主导”完成卡片初筛、详情审核和个性化招呼语生成。
- 招呼语必须为简体中文、长度 80–150 个 Unicode 字符，并通过简历事实接地校验。
- 支持“仅填充不发送”和“实际发送”两种模式；GUI 默认选择实际发送，启动前务必确认。
- 发送动作只执行一次，并以聊天记录中的真实我方消息气泡作为成功依据；确认失败时不会自动重发。
- 只在顶部消息入口存在可见未读数字时进入消息页，处理附件简历请求、明确拒绝、状态通知和需人工处理的会话。
- 支持暂停、修改部分设置、继续、停止、断点记录，以及 SQLite/MySQL/JSON 多层结果保存。

## 环境要求

- Windows 10/11
- Microsoft Edge
- 正式 EXE 运行不需要安装 Python；只有源码开发需要 Python 3.11 及以上（当前构建环境为 Python 3.13）
- 可连接的 MySQL 8.x；GUI 启动正式任务时会创建或更新所需数据库表
- 至少一份文本型 PDF 简历
- 使用“大模型API”时，需要 OpenAI 兼容接口配置
- 使用“Codex主导”时，需要本机 `codex` 命令可用

## 安装

### 全新 Windows 电脑一键离线部署

项目提供独立的 `requests-packages/` 环境包，其中包含 PowerShell 7.6.4 LTS、Edge 151.0.4129.59、MySQL 8.0.36、VC++ x64 运行库、Navicat 17.3.11 和可选 Codex CLI 0.133.0。正式 Nuitka EXE 已内置 Python 运行时和项目依赖，因此环境包不再安装 Python，也不含 wheelhouse 或源码。一键入口会复用任意现有 PowerShell 7；没有时先离线安装，再由 `pwsh.exe` 继续部署。新装的 PowerShell 7、MySQL 和 Navicat 跟随一键部署文件所在盘符，Edge 和 VC++ 等系统组件保留 Windows 默认安装位置；现有软件不迁移。

在新电脑上先把 `requests-packages` 复制到目标目录并运行部署，再把两个 EXE 复制到它的父目录。最终依次双击：

```text
requests-packages\验证安装包.cmd
requests-packages\一键部署.cmd
requests-packages\验证环境.cmd
```

确认环境、两个 EXE 和外置配置均可用后，可双击 `requests-packages\delete.cmd` 永久清理整个离线部署包。该入口无需二次确认，会逐文件覆盖并刷新后绕过回收站删除 `requests-packages`（包括自身），但不会删除已安装的软件、父目录 `config`、`resume_inbox` 或两个 EXE。SSD 磨损均衡、卷影副本、云同步及外部备份不属于本地脚本能够保证清除的范围。

部署脚本会在父目录创建 `config` 和 `resume_inbox`；API Key、MySQL 用户名和密码故意留空。配置位置分别是：

- API：目标根目录 `config/model_api.local.json`，保留模型 `deepseek-v4-flash`
- Codex：双击 `requests-packages/安装Codex（可选）.cmd`；已安装且已登录时自动检测并复用
- GUI/MySQL：目标根目录 `config/gui_defaults.txt`

Codex 主导模式固定使用 `gpt-5.5`；API 模式使用 JSON 中实际填写的模型。开始模型审核前必须至少完成一种。完整说明见 [requests-packages/README.md](requests-packages/README.md) 与 [首次使用向导](requests-packages/首次使用向导.md)。

该一键部署流程已于 2026-08-08 在初始未安装 PowerShell 7 的干净 Windows Sandbox 完成验收；Navicat 可正常使用并成功连接新部署的 MySQL 数据库。验收未使用任何真实账号、密钥、简历或投递功能。

部署包不含或复制源码、API Key、Codex/Boss 登录状态、MySQL 历史数据、Navicat 许可证、真实简历和 `data/edge_profile_boss/`。分发给新用户前请运行 `requests-packages/scripts/Build-Distribution.ps1` 创建“两个 EXE + 环境包”的脱敏副本，不要直接压缩开发目录。`delete.cmd` 检测到 `.git` 或源码目录时会拒绝执行，避免误删开发项目。

### 已有 Python 环境手动安装

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
```

需要运行测试或静态检查时：

```powershell
python -m pip install -r requirements-dev.txt
```

## 首次配置

### 1. 准备简历

把本次使用的唯一一份文本型 PDF 放入 `resume_inbox/`。目录中存在多份 PDF 时程序会拒绝继续，避免选错简历。

PDF 含有个人信息，已通过 `.gitignore` 排除，不应提交或分享。程序只解析本地 PDF 用于岗位审核和事实约束，不会把该文件直接上传到 Boss；消息页发送的是 Boss 账号中已有的附件简历。

### 2. 配置 GUI 默认值

复制脱敏模板：

```powershell
Copy-Item config\gui_defaults.example.txt config\gui_defaults.txt
```

编辑 `config/gui_defaults.txt`。该文件只在 GUI 实例启动时读取一次，运行开始后和暂停恢复时均以界面当前值为准。

文件可能包含 MySQL 明文密码，已被 `.gitignore` 排除，请勿外发。

### 3. 配置大模型 API

```powershell
Copy-Item config\model_api.example.json config\model_api.local.json
```

在 `config/model_api.local.json` 中填写实际 `base_url`、`model` 和密钥。程序使用配置中填写的模型，不会固定替换模型名。密钥可以直接写入 `api_key`，也可以通过 `api_key_env` 指向环境变量；推荐使用环境变量。

本地 API 配置已被 `.gitignore` 排除。

### 4. 准备 MySQL

确保 GUI 中填写的账号可以连接 MySQL，并具有创建数据库、数据表及读写记录的权限。默认数据库名为 `boss_job_assistant`。

程序会维护：

- `automation_runs`：每次运行的状态和汇总计数
- `application_results`：岗位处理结果
- `successful_applications`：成功投递镜像，用于30天去重
- 每日投递统计表

## 登录专用 Edge

先启动项目专用 Edge（两种方式任选其一）：

```powershell
python tools\open_login_edge.py
```

或直接双击项目根目录的 `Boss登录浏览器.exe`（打包产物，无终端窗口；启动成功不弹窗，仅启动失败时弹窗提示）。

在打开的 Edge 中手动登录 Boss直聘，并保持该窗口开启。登录资料保存在 `data/edge_profile_boss/`，不会放进部署包或版本控制。

默认只绑定 `127.0.0.1` 本地调试端口。可通过环境变量 `BOSS_EDGE_DEBUG_PORT` 指定 1024–65535 的固定端口。

## 启动控制台

```powershell
python run_control_panel.py
```

也可以执行：

```powershell
python -m boss_assistant.gui
```

或直接双击项目根目录的 `Boss求职助手.exe`（无控制台窗口）。exe 双击运行时会自动把工作目录固定到项目根目录，`data/`、`config/`、`resume_inbox/` 等相对路径与命令行启动行为一致。

GUI 启动后会通过独立的只读 CDP 会话预读网页求职意向，不会因此初始化简历、MySQL、模型或投递循环。点击“开始”后才正式运行。

## GUI 关键设置

| 设置 | 说明 |
| --- | --- |
| 求职意向 | “平均次数”按页面顺序分配目标数；选择具体“城市 / 岗位”则整轮锁定该意向 |
| 薪资范围 | 单位为 K/月；岗位完整上下限必须落在设置的闭区间内 |
| 公司规模 | 岗位公司规模档位下限必须达到设置值；读取不到时安全判为不符合 |
| 目标公司数 | 1–150；只统计实际完成的目标公司，失败岗位不会伪装成完成 |
| 最低匹配分 | 0–100 |
| 运行模式 | “仅填充不发送”或“实际发送”；默认实际发送 |
| 审核方式 | “大模型API”或“Codex主导” |
| 周末休息 | 不限、双休、大小周、单休 |
| 经验要求 | 经验不限、1-3年、3-5年、5-10年 |

运行中可暂停并修改部分筛选、运行模式、审核方式和 MySQL 设置；求职意向在本轮启动后保持锁定，避免配额和断点错位。

## 当日沟通弹窗

程序区分两类真实 Boss 弹窗：

1. “今天已与 N 位 BOSS 沟通，还剩 N 次沟通机会”：点击确认后优先等待 Boss 自动进入聊天；只有确认仍停留详情页时才重放一次原“立即沟通”。
2. “您已达到沟通上限 / 今天已与150位BOSS沟通 / 明天再来”：点击“确定”并立即正常结束，GUI显示“运行结束”和“结束理由：已达到150沟通上限”。不会再次点击“立即沟通”，不会继续处理其它岗位。

一旦精确确认150位硬上限，程序会先锁定终止状态。即使“确定”按钮点击或弹窗关闭校验失败，也只会附带告警结束，不会恢复投递。

结束原因和可能的关闭告警会写入运行 JSON 与断点文件；MySQL 运行状态按正常完成收尾。

## 数据与日志

所有运行产物默认位于 `data/`，该目录已被版本控制忽略：

| 路径 | 内容 |
| --- | --- |
| `data/jobs.sqlite3` | 本地岗位快照索引与去重数据 |
| `data/job_artifacts/` | 岗位详情结构化快照 |
| `data/automation_runs/` | 每次运行 JSON 和 `boss_checkpoint.json` |
| `data/resume/` | PDF 副本、解析文本和结构化简历 |
| `data/model_api_reviews/` | API 审核脱敏记录 |
| `data/manual_reviews/` | Codex 主导审核请求和响应 |
| `data/probe/` | DOM 探针保存的页面 HTML |
| `data/edge_profile_boss/` | Boss 登录专用 Edge 资料 |

## 诊断工具

读取真实页面的求职意向、岗位卡片并保存 HTML：

```powershell
python tools\probe_dom.py --snapshot-only
```

指定求职意向：

```powershell
python tools\probe_dom.py --target-city 广州 --target-role Python
```

仅填充烟雾验证工具会使用现有断点和 Codex 审核，不连接 MySQL、不发送招呼语，但会真实进入岗位流程并可能向输入框填入内容：

```powershell
python tools\run_fill_only_smoke.py --target-companies 1 --max-jobs 10
```

## 使用 Nuitka 打包为 EXE

开发机安装固定构建依赖，然后执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File tools\build_exe_nuitka.ps1
```

产物为项目根目录下的两个 onefile EXE：`Boss登录浏览器.exe` 和 `Boss求职助手.exe`。构建使用 MSVC、LTO、Windows GUI 子系统和正式 ICO；Python 3.13 不使用 MinGW。两个 EXE 从自身所在目录读取外置 `config/`、`data/` 和 `resume_inbox/`。

正式 A 方案图标完整保存在 `assets/icons/official/`，包括两张透明 PNG、两份多尺寸 ICO 和 A 方案预览图，可供后续版本继续使用。求职助手采用四向 AI 罗盘，登录浏览器采用蓝青连接环与红橙节点；均为白色圆角底，不再使用公文包图案。`tools/make_icons.py` 会生成覆盖 Windows 100%–300% 常用 DPI 阶梯的 16/20/24/28/32/36/40/48/56/64/72/80/96/128/256 像素 ICO；其中 16–96 像素由代码按目标尺寸单独绘制无阴影、加粗、像素对齐的任务栏图层，不再把复杂 256 像素源图直接缩小。GUI 在创建 Tk 窗口前启用 Windows System DPI Awareness，因此 125% 缩放会直接取得 20/40 像素层，不再由 DWM 放大 96 DPI 的 16/32 像素图层。

打包结束后，脚本会通过 Windows Shell 定向通知刷新两个 EXE 的图标，并调用系统图标显示刷新。这样即使保持项目文件夹窗口打开，同路径重建产物也不应继续显示旧图标；不需要手动删除全局 `iconcache_*.db`。

构建前，`tools/obfuscate_strings.py` 只在 `build/nuitka/staging/` 创建临时源码副本，并对核心审核、策略和数据库字符串做每次构建随机的压缩/XOR/编码转换；正式源码及目录结构不修改。该措施与 Nuitka 原生编译共同提高批量解包和 AI 一键还原的门槛，但不能承诺抵御有经验者的动态调试、内存抓取或长期逆向。

**EXE 是打包时刻代码与依赖的冻结快照**：每次 BUG 修复和每次修改运行源码后，都必须同步更新版本号并重新构建两个正式 EXE；未完成 EXE 版本、哈希和安全启动验收，不得把该次 BUG 修复标记为完成。`tools/build_exe.ps1` 仅保留为旧 PyInstaller 开发回退，不是正式发布方式。

## 验证

```powershell
python -m pytest tests -q
python -m compileall -q boss_assistant tests run_control_panel.py tools
.\.venv\Scripts\python.exe -m pip check
python -m ruff check boss_assistant tests run_control_panel.py tools
```

当前 `0.1.9` 基线、自动化流程修复与既有 Nuitka 构建/环境包校验结果见 [VALIDATION_REPORT.md](VALIDATION_REPORT.md)。

## 安全边界与已知限制

- 实际发送模式会产生真实 Boss 沟通记录，启动前必须核对模式、筛选条件和目标数量。
- 自动化运行时可以切换到求职助手控制台或其它窗口，也可以让登录专用 Edge 留在后台；程序会保持 Boss 标签页的可见/活动语义，无需手动把浏览器一直置顶，但不能关闭登录专用 Edge 或 Boss 标签页。
- 进入聊天、点击发送或输入框清空都不是成功证据；只有聊天记录中出现完整我方消息才计为发送成功。
- 发送动作后的确认失败会停止该岗位，不会自动再次发送。
- 页面 DOM 或 Boss 规则可能更新；选择器失效时先运行 DOM 探针并根据真实页面修订。
- 数据库沟通数不能代表 Boss 当日真实沟通数；额度判断始终以实时可见弹窗为准。
- 扫描件 PDF OCR 不在当前范围内。
- 登录态、API密钥、MySQL密码、真实简历和 Edge 用户资料均属于本机私密数据，不应进入版本控制或部署包。
- 当前开发目录可能实际存在上述本机文件；对外分发前必须使用脱敏分发脚本或按同等规则排除，不能仅依赖 `.gitignore`。

## 目录概览

```text
boss_assistant/
  automation/   主循环、策略、审核、消息处理、MySQL
  browser/      最小原生 Edge CDP 封装
  gui/          Tkinter 控制台
  page/         Boss 岗位详情数据模型与读取
  resume/       PDF 收件、解析和结构化
  storage/      SQLite 岗位快照存储
  web/          DOM 选择器与页面解析
config/         本地配置及脱敏模板
resume_inbox/   本次使用的唯一 PDF 简历
tests/          自动化回归测试
tools/          Edge 启动、DOM 探针、仅填充烟雾验证
```

版本变化见 [VERSION_HISTORY.md](VERSION_HISTORY.md)。
