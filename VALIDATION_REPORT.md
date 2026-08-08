# Windows Web 验证报告

## 验证基线

- 项目版本：`0.1.5`
- 验证日期：2026-08-08
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

- 测试：`123 passed`
- Python 编译检查：通过
- 项目 `.venv` 依赖一致性：`No broken requirements found`
- `config/model_api.example.json` 与本地 API 配置：均通过 JSON 解析

`ruff` 尚未安装在当前环境，因此没有把静态检查伪装成已通过；项目已新增 `requirements-dev.txt`，安装后可执行 README 中的静态检查命令。

打包使用的系统 Python 中另有一个不属于本项目依赖清单的全局 FastAPI/Starlette 版本冲突，因此没有把系统环境的 `pip check` 写成通过；该冲突未影响本次 PyInstaller 构建，项目 `.venv` 检查通过。

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

## Win-Web 离线部署包验证

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

本次没有在当前开发机执行 Edge MSI、MySQL 服务、VC++ 或 Navicat 的覆盖安装，因为当前机器已有对应环境；也没有把这次受控跳过路径写成“全新电脑完整安装已验证”。完整一键安装仍需在干净 Windows 10/11 x64 电脑验收。

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
- 当前目录不是 Git 工作树，无法用 `git diff` 或 `git status` 证明改动范围；本次通过逐文件读取、针对性测试和全量测试完成核验。
