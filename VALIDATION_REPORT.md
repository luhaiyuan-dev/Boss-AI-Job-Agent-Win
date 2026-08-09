# Windows Web 验证报告

## 验证基线

- 项目版本：`0.1.6`
- 验证日期：2026-08-09
- 工作目录：项目根目录
- 当前验证环境：Windows、Python 3.13、登录专用 Microsoft Edge

本报告只记录已经执行并有直接证据支持的验证，不把代码存在、按钮点击或页面跳转本身表述为发送成功。

## 自动化验证

执行命令：

```powershell
python -m pytest tests -q
python -m compileall -q boss_assistant tests run_control_panel.py tools
.\.venv\Scripts\python.exe -m pip check
```

结果：

- 测试：`130 passed`
- Python 编译检查：通过
- 项目 `.venv` 依赖一致性：`No broken requirements found`
- `config/model_api.example.json` 与本地 API 配置：均通过 JSON 解析

`ruff` 已安装；本次新增的运行时路径、图标与字符串加固工具单独检查通过。全仓审计仍检出 179 条既有风格问题，因此没有把全仓 Ruff 伪装成通过，也没有借本次打包升级扩大修改范围。

构建和测试统一使用项目 `.venv`，`pip check` 返回 `No broken requirements found`。

## 0.1.6 Nuitka、外置配置与环境包升级

正式构建：

- 使用 Python 3.13.14 x64、Nuitka 4.1.3 与 MSVC 14.5，两个入口均采用 onefile、LTO、无控制台窗口和 0.1.6.0 PE 版本信息；未使用 Python 3.13 不兼容的 MinGW 路径。
- `tools/obfuscate_strings.py` 只在 `build/nuitka/staging` 复制并转换构建副本，正式源码保持可读。最终一轮共转换 202 处引用、183 条唯一字符串；Nuitka 报告包含 `boss_assistant._protected_strings`，核心模块均引用该生成模块。
- 最终 C 构建目录和两个 onefile EXE 对 `严格依据`、`CREATE TABLE`、`岗位方向是否匹配`、`不得编造`、PyInstaller 标记等代表性明文扫描均无命中。该保护用于提高批量解包与 AI 一键还原门槛，不宣称能抵御动态调试或内存抓取。
- 两个 EXE 的 PE Subsystem 均为 `2`（Windows GUI），Authenticode 状态为 `NotSigned`，符合本次已确认的无签名边界。
- `Boss求职助手.exe`：27,759,104 字节，SHA-256 `F84D272EFA4F1013A1378E3254A0CE637ADF37C3EBB14058A0386DD04A8F777B`。
- `Boss登录浏览器.exe`：14,175,744 字节，SHA-256 `9A16562721C11C1B081C39FDFD9FE16E18E672BE03DE676DBEBE7BE2A25F7426`。

运行验证：

- 主 EXE 启动后生成一个响应正常的窗口，标题为 `Boss 求职助手控制台-Win v0.1.6`；正式子进程经 `GetProcessDpiAwareness` 验证为 1（System DPI Aware）。未点击“开始”，随后关闭本次父/子进程。
- 登录 EXE 使用独立临时 Profile 和 9339 调试端口运行，退出码为 0，CDP 观察到 Boss 页面；未登录、未填写、未发送。测试 Edge 通过 `Browser.close` 关闭，隔离 Profile 已移入回收站。
- 本机只读环境验证自动识别 Edge、VC++、运行中的 MySQL、Navicat，以及 PATH 中已安装且登录的 Codex CLI 0.133.0；主程序仍固定要求 Codex 模式使用 `gpt-5.5`。

环境包与图标：

- `requests-packages` 已移除 Python 安装器、`requirements-offline.txt` 和完整 wheelhouse；当前清单为 PowerShell 7.6.4 LTS、Edge、MySQL、VC++、Navicat、Codex 共 6 项，826,296,129 字节（788.02 MiB）。PowerShell ZIP 的 SHA-256 与官方发布清单一致；引导脚本解压后验证 `pwsh.exe`，再写入系统 PATH。
- 一键部署仅安装或复用电脑环境，在父目录创建 `config`、`resume_inbox` 和两个外置配置模板；API Key、MySQL 用户名与密码为空，API 模型保留 `deepseek-v4-flash`。新装 MySQL 密码必须现场无回显输入并二次确认，至少 8 位。
- 一键入口新增仅含 ASCII 的 PowerShell 引导脚本：先复用任意现有 PowerShell 7；没有时离线静默安装随包的 7.6.4 LTS，再强制由 `pwsh.exe` 执行正式部署。Windows PowerShell 5.1 只负责引导，不再解析中文主部署脚本。
- 新增 `requests-packages\delete.cmd` 与 PowerShell 7 删除器：双击后无二次确认，先拒绝磁盘根目录、源码开发目录和链接/重解析点，再对普通文件随机覆盖、WriteThrough 写入、强制刷新、随机改名并绕过回收站删除；入口和脚本自身最后删除，整个 `requests-packages` 消失。删除范围明确不包含已安装的软件、父目录 `config`、`resume_inbox` 和两个 EXE，也不宣称能清除 SSD 磨损均衡、卷影副本、云同步或外部备份。
- 正式 A 方案图标保存在 `assets/icons/official`。两张 PNG 为 1254×1254、四角 alpha 均为 0；两份 ICO 均含 16/20/24/28/32/36/40/48/56/64/72/80/96/128/256 共 15 个尺寸，Nuitka 日志确认分别嵌入两个 EXE。
- 2026-08-09 根据 Windows 任务栏实拍继续定位小图层模糊：本机为 120 DPI（125%），Windows 实际请求 20×20 小图标和 40×40 大图标；上一轮 ICO 恰好缺少这两个尺寸，只能由 Shell 缩放相邻图层。继续检查正式 EXE 子进程后，`GetProcessDpiAwareness` 又确认其级别为 0（DPI unaware），Tk 创建的 96 DPI 窗口还会被 DWM 整体放大。新生成器补齐 100%–300% 常用 DPI 尺寸阶梯，将任务栏专用绘制范围扩展到 16–96 像素；进一步放大四色主体、把 28 像素以下的细星芒简化为实心菱形，并使用面积平均缩小避免 Lanczos 在高对比边缘产生振铃。GUI 在第一个 Tk HWND 创建前启用 System DPI Awareness，使 125% 屏幕直接加载原生 20/40 像素层。15 个尺寸和透明四角由回归测试逐层读取确认。
- 两个 Nuitka onefile EXE 已重新构建，构建日志分别确认 `Adding 15 icon(s)`。主 EXE 再次启动后窗口标题为 `Boss 求职助手控制台-Win v0.1.6` 且保持响应；通过任务栏窗口 `Shell_TrayWnd` 的实拍确认，40 像素原生层能够清晰显示圆角底板、放大的四向罗盘和中心星芒。完整 DPI-aware 窗口截图也确认右上角只剩“默认实际发送 · 自动查看未读消息 · 达到目标公司数立即停止”。测试只启动 GUI，未点击“开始”，随后关闭测试进程。
- 原有 5 个包含中文的部署 PowerShell 脚本继续使用 UTF-8 BOM，引导脚本严格保持纯 ASCII、无需 BOM，并同时通过 Windows PowerShell 5.1 与 PowerShell 7 语法解析。新增删除脚本使用 UTF-8 BOM、明确要求 PowerShell 7，并通过 PowerShell 7 语法解析。

Windows Sandbox 功能已启用并完成干净机验收。首次连接中断由 Hyper-V Worker 事件 33101 定位为来宾虚拟 PCI 协议 `0x10006` 与宿主修订级别不兼容；在 `.wsb` 和宿主 Windows Sandbox 策略中禁用 vGPU，并经用户授权停止 Docker/WSL、重启 `vmcompute` 后，沙盒连接保持稳定。干净沙盒初始没有 PowerShell 7，一键入口成功校验并解压随包的 7.6.4 ZIP，再由 `pwsh.exe` 完成后续环境部署；6 项安装资源哈希全部通过。2026-08-08 用户在该沙盒中手动完成一键部署，确认 Navicat 可以正常使用，并通过 Navicat 成功连接新部署的 MySQL 数据库。验证全程未录入真实 API Key、Codex/Boss 登录信息或简历，也未执行投递。

2026-08-09 进一步将可指定目录的软件改为跟随一键部署文件所在盘符：新装 PowerShell 7 使用 `盘符:\BossJobAssistant\PowerShell\7`，MySQL 程序、数据和 `my.ini` 使用 `盘符:\BossJobAssistant\MySQL`，Navicat 使用 `盘符:\BossJobAssistant\Navicat\17.3.11`。Edge 与 VC++ 运行库继续使用官方安装器的系统默认位置；检测到的既有软件不迁移。两个后续 CMD 入口增加部署盘 PowerShell 回退查找，避免环境变量尚未刷新时误报。该增量通过 PowerShell 5.1/7 语法解析、CMD 编码/换行回归和项目测试；此前干净沙盒验收使用的是修改前的系统盘路径，因此不将其表述为新盘符布局的实机安装证明。

2026-08-09 在 `build/delete-tests` 的隔离副本执行永久删除验证：直接调用删除脚本时，包含 2 MiB 随机载荷、嵌套目录、入口和脚本自身的模拟 `requests-packages` 完整消失，退出码为 0，父目录外部标记文件保持存在；通过 `delete.cmd` 异步启动的第二个模拟包同样连同入口自身删除。第三个副本在父目录放置 `.git` 后返回退出码 1，部署包和载荷均未改变，证明源码保护先于覆盖删除。测试只作用于固定的 `build/delete-tests` 隔离目录，没有在真实 `requests-packages` 上运行删除器。

## 0.1.5 Explorer 图标缓存修复

- 从两个 EXE 直接提取关联图标，均为 0.1.4 起使用的新浅蓝图标；再通过 Windows Shell `SHGetFileInfo` 读取 Explorer 实际解析结果，仍得到相同的新图标。由此排除 ICO 生成或 EXE 资源嵌入失败。
- 根因是已打开的 `explorer.exe` 进程仍持有同一路径旧 EXE 的内存图像列表；仅删除旧文件再重建，不能保证当前文件夹窗口主动失效该条目。
- `tools/build_exe.ps1` 构建结束后现在会对两个绝对 EXE 路径调用 `SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, ...)`，并执行 `ie4uinit.exe -show`。刷新只针对文件显示，不删除全局图标或缩略图缓存数据库。
- 新增回归测试，锁定两个 EXE 都必须发送定向刷新通知，并保留系统图标显示刷新命令。
- 使用修改后的脚本完成 0.1.5 全量构建，末尾 Shell 刷新步骤执行成功且未产生警告；随后重启一次 Explorer 并重新打开项目目录，清除了修复前已经存在的旧内存图标。
- Explorer 重启后再次从两个 EXE 提取关联图标，并通过 Shell `SHGetFileInfo` 获取文件夹所用图标；求职助手和登录浏览器两组 RGBA 像素均完全一致。两个 EXE 的 PE Subsystem 仍为 `2`。
- 当前 `Boss登录浏览器.exe` 为 67,161,602 字节，SHA-256 `4DFA1178FF2C5C7703BB8D9A7BA2D288E55DD5C10C50368AD9FCCB3FB89DA7D9`；`Boss求职助手.exe` 为 68,350,221 字节，SHA-256 `04E20DDB8EA43070608CBF288C293466E560D1FDA14CC8E99355FB4C054A3565`。

## 0.1.4 EXE 闪窗与图标验证

根因与修复：

- 两个 EXE 本身已经是 PE GUI 子系统，但 `discover_edge_debug_targets()` 会通过 `powershell.exe` 读取 Edge 命令行。求职助手 GUI 在登录浏览器不可用时每 5 秒重试一次求职意向预读，因此控制台子进程会按固定间隔短暂显示空窗口；关闭 Edge 后的下一轮探测也会触发同一路径。
- Edge 进程枚举现在显式使用 Windows `CREATE_NO_WINDOW`；登录器复用/启动 Edge 时同时关闭标准输入输出错误句柄，并组合无窗口、脱离父进程和新进程组标志。
- 新增回归测试，直接断言两个外部进程调用路径携带无窗口标志，且登录器三个标准句柄均为 `DEVNULL`。

图标结果：

- 两张最终透明 PNG 源图保存于 `assets/icons/`，四角 alpha 均为 0；配色为浅天蓝/湖蓝、白色，并分别使用珊瑚橙和暖黄色强调色，不含紫黑深蓝主题。
- `tools/make_icons.py` 每次从 PNG 源图生成 ICO；两个 ICO 均实际包含 16、24、32、48、64、128、256 共 7 个尺寸，且四角保持透明。
- PyInstaller 构建脚本对两个入口均保留 `--windowed`，并将新 ICO 分别嵌入对应 EXE；GUI 窗口图标也从同一求职助手 ICO 加载。

实际产物验证：

- `Boss登录浏览器.exe`：67,160,515 字节，SHA-256 `68EC783C2C2CEF6BE859A00C3FD94BDE3ABCD87E3CB7A200E2A55D41F475402A`。
- `Boss求职助手.exe`：68,352,219 字节，SHA-256 `D79EE4EFEEA911D46F299B7B9CBFFFD7EE64CA43C0FEA339B98D98FFCF766A29`。
- 解析两个 EXE 的 PE Optional Header，Subsystem 均为 `2`（Windows GUI）。从两个 EXE 实际提取的关联图标分别为新的公文包星芒和浏览器钥匙孔图标。
- 首轮同时启动两个 EXE 并以 40ms 间隔监测 24 秒，覆盖多轮 5 秒 GUI 求职意向重试，新增 `ConsoleWindowClass` 数量为 0。
- 第二轮使用隔离临时 Profile 强制执行“登录器启动 Edge → CDP `Browser.close` 关闭 → 继续监测”，以 40ms 间隔监测 28 秒，新增 `ConsoleWindowClass` 数量仍为 0；测试未读取或修改现有登录资料，临时 Profile 已移入回收站。

## 0.1.3 Win-Web 离线部署包历史验证

本次新增 `requests-packages/`，以 Android 版已经使用的清单、哈希、部署、环境验证结构为模板，但依赖按当前 Win-Web 项目重新盘点，不包含 ADB、Platform-Tools 或 ADBKeyBoard。

本机版本基线：

- Python 3.13.14 x64
- Microsoft Edge Stable 151.0.4129.59
- MySQL Community Server 8.0.36
- Microsoft Visual C++ v14 Redistributable x64 14.50.35719.0
- Navicat Premium 实际程序文件 17.3.11.0
- Codex CLI 0.133.0
- Selenium 4.45.0、websocket-client 1.9.0、webdriver-manager 4.1.2、mysql-connector-python 9.7.0、pypdf 6.14.2、OpenCC 1.3.2

随包结果：

- `manifest.json` 与 `SHA256SUMS.txt` 共 34 项，总大小 773,466,055 字节（737.63 MiB）。
- Windows PowerShell 5.1 实际执行 `Verify-Packages.ps1`，34 项文件路径、大小和 SHA-256 全部通过。
- 5 个 PowerShell 脚本均使用 UTF-8 BOM，并同时通过 PowerShell 7 与 Windows PowerShell 5.1 语法解析。
- Python、Edge、VC++、Navicat 安装器 Authenticode 状态为 `Valid`；Edge 安装包 SHA-256 与 Microsoft 企业更新接口公布值一致。
- MySQL ZIP 能读取到 `mysql-8.0.36-winx64/bin/mysqld.exe` 等正确目录结构。
- Codex 官方 ZIP 含主程序及两个 Windows sandbox helper；临时解压执行返回 `codex-cli 0.133.0`。
- 运行 `Setup.ps1 -SkipEdge -SkipMySql -SkipNavicat -SkipAiSetup -NoElevation` 成功：从零创建项目 `.venv`，只使用 `wheelhouse/` 安装锁定依赖，导入检查和 `pip check` 通过，现有本机配置未被覆盖。
- `Verify-Environment.ps1` 在当前开发机确认 Python/Edge/MySQL/Codex/API/简历就绪，检查过程中未输出密钥、密码或简历文件名。
- `Build-Distribution.ps1` 已实际创建一次临时脱敏副本：复制 97 个分发文件，本机配置、`data/`、`.venv/` 和 PDF 检出数均为 0；核验后删除了该临时副本。

API Key 与 Codex 安装/登录分别提供跳过选项。部署脚本会明确显示 `config/model_api.local.json`、`config/model_api.example.json` 和 `config/gui_defaults.txt` 的绝对位置。分发辅助脚本会排除本机配置、PDF、`data/`、`.venv/`、缓存和日志。

0.1.3 当时没有在开发机覆盖安装 Edge MSI、MySQL 服务、VC++ 或 Navicat，因此该版本只记录受控跳过路径，未宣称完成全新电脑安装。当前 0.1.6 环境包已经在干净 Windows Sandbox 完成一键部署、Navicat 使用和 MySQL 连接验收，见本文前述 0.1.6 验证记录。

## 150位当日沟通上限真实验证

真实页面观察到的 DOM：

- URL：`https://www.zhipin.com/web/geek/jobs?ka=header-jobs`
- 可见容器：`.chat-block-dialog`
- 标题：`您已达到沟通上限`
- 正文：`您今天已与150位BOSS沟通，休息一下，明天再来吧～`
- 按钮：`.chat-block-dialog .sure-btn`
- 按钮文案：`确定`

真实闭环结果：

- 新选择器识别为 `limit_reached=True`
- `completion_reason` 为 `已达到150沟通上限`
- 点击“确定”后弹窗消失
- 页面保持在 Boss 职位页
- 没有出现聊天输入框
- 没有填入或发送任何招呼语
- 终止信号成功向主循环传播

## 0.1.2 加固验证

本次增加并验证以下边界：

1. DOM 同时存在隐藏旧弹窗和可见新弹窗时，只选择可见节点及可见确认按钮。
2. 首次确认150位硬上限后立即锁定结束原因。
3. 模拟“确定”按钮点击失败时，仍抛出硬上限终止信号，并保存关闭告警；不会按普通岗位失败继续。
4. GUI 与 runner 共用同一个 `AutomationStats` 对象；模拟运行进度为 `inspected=7` 后抛出普通异常，共享统计和中断 JSON 均保留 `7`，MySQL 收尾不再使用全零对象。
5. 运行 JSON 与断点均保存 `completion_reason` 和 `completion_warning`。

## 安全口径

- 未执行真实招呼语发送来验证本次修复。
- 没有把进入聊天页、点击按钮、编辑器清空或数据库计数当作发送成功。
- 120次剩余机会提醒仍保持原有“关闭后恢复当前岗位”的行为；150位硬上限是独立终止分支。
- 如果150位弹窗关闭失败，程序可能无法替用户消除页面遮罩，但必须停止自动化并显示告警，不能继续点击其它岗位。

## 已知限制

- Boss Web DOM、文案和额度规则属于外部状态，未来变化后需要重新进行真实页面核验。
- 扫描件 PDF 不支持 OCR。
- 登录态、API密钥、MySQL凭据和真实简历只在本机维护，不属于可分发项目资产。
- 本次通过 `git diff --check`、逐文件读取、针对性测试和全量测试完成核验。
