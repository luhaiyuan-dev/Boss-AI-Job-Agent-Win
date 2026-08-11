from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from boss_assistant import __version__
from boss_assistant.automation.api_provider import (
    ApiProviderConfig,
    OpenAICompatibleReviewProvider,
)
from boss_assistant.automation.models import (
    AutomationConfig,
    AutomationPolicy,
    AutomationStats,
    ChatConversation,
    ChatJobInfo,
    ChatMessage,
    JobCard,
    JobExpectation,
    JobIntentData,
)
from boss_assistant.automation.mysql_store import (
    AutomationMySqlStore,
    MySqlConfig,
    MySqlStoreError,
)
from boss_assistant.automation.policy import (
    card_rejection_reason,
    card_review_rejection_reason,
)
from boss_assistant.automation.requirements import (
    company_size_meets,
    company_size_range,
    salary_meets,
    salary_range_k,
)
from boss_assistant.automation.review import (
    CardReviewResult,
    ChatReviewResult,
    CodexCliReviewProvider,
    DetailReviewResult,
    ReviewError,
)
from boss_assistant.automation.runner import (
    DAILY_COMMUNICATION_LIMIT_REASON,
    BossAutomationError,
    BossAutomationRunner,
    DailyCommunicationLimitReachedError,
    allocate_expectation_quotas,
    find_recent_successful_application,
    load_recent_successful_applications,
    recommendation_cards_ready,
    select_policy_job_intents,
)
from boss_assistant.browser.driver import (
    BrowserError,
    EdgeDebugTarget,
    EdgeBrowser,
    ElementNotFoundError,
    LoginRequiredError,
    _running_edge_command_lines,
)
from boss_assistant.gui.app import (
    BossControlPanel,
    format_run_completion,
    result_view_is_at_bottom,
)
from boss_assistant.page import PageReadError, build_job_page_data
from boss_assistant.web.page_reader import align_detail_identity, read_job_detail
from boss_assistant.web.selectors import (
    SELECTORS,
    CommunicationQuotaNotice,
    _clean,
    _same_job_card,
    build_card_fingerprint,
    expectation_is_active,
    extract_chat_conversations,
    extract_chat_system_notes,
    extract_job_cards,
    find_chat_entry,
    find_expectation_element,
    find_greeting_editor,
    find_send_button,
    parse_job_intents,
    read_communication_quota_notice,
    read_current_chat_identity,
    read_current_chat_job_info,
    read_message_unread_count,
    resume_request_accept_button,
    select_job_card_inline,
)


def _policy() -> AutomationPolicy:
    return AutomationPolicy(
        excluded_companies=(),
        allowed_job_keywords=("Python", "AI应用开发"),
        allowed_locations=("广州",),
        target_companies=1,
    )


def test_package_version_matches_latest_version_history() -> None:
    history = Path("VERSION_HISTORY.md").read_text(encoding="utf-8")
    released_versions = re.findall(
        r"^- `(\d+\.\d+\.\d+)`（",
        history,
        flags=re.MULTILINE,
    )

    assert released_versions
    assert __version__ == released_versions[-1]


def test_project_documents_match_package_version_and_protect_resume_pdf() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    validation = Path("VALIDATION_REPORT.md").read_text(encoding="utf-8")
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert f"当前版本：`{__version__}`" in readme
    assert f"项目版本：`{__version__}`" in validation
    assert "resume_inbox/*" in gitignore
    assert "!resume_inbox/README.md" in gitignore


def test_edge_process_probe_never_creates_a_console_window() -> None:
    completed = SimpleNamespace(returncode=0, stdout="")
    with patch(
        "boss_assistant.browser.driver.subprocess.run",
        return_value=completed,
    ) as run_process:
        _running_edge_command_lines()

    assert run_process.call_args.kwargs["creationflags"] == getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )


def test_login_launcher_reuses_edge_without_a_console_window(tmp_path: Path) -> None:
    from tools import open_login_edge

    completed = SimpleNamespace(returncode=0)
    with (
        patch.object(open_login_edge, "locate_edge", return_value=Path("msedge.exe")),
        patch.object(
            open_login_edge.subprocess,
            "run",
            return_value=completed,
        ) as run_process,
    ):
        assert open_login_edge._open_boss_in_existing_edge(tmp_path)

    kwargs = run_process.call_args.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_login_launcher_disables_background_page_throttling() -> None:
    source = Path("tools/open_login_edge.py").read_text(encoding="utf-8")

    assert '"--disable-background-timer-throttling"' in source
    assert '"--disable-backgrounding-occluded-windows"' in source
    assert '"--disable-renderer-backgrounding"' in source


def test_cdp_target_emulates_visible_focus_without_bringing_window_to_front() -> None:
    browser = EdgeBrowser()
    target = EdgeDebugTarget(
        debugger_address="127.0.0.1:63289",
        url="https://www.zhipin.com/web/geek/jobs",
        title="Boss直聘",
        target_id="boss-page",
        source="test",
    )
    calls: list[tuple[str, dict[str, object] | None]] = []
    client = SimpleNamespace(
        call=lambda method, params=None: calls.append((method, params)) or {},
        close=lambda: None,
    )
    payload = {
        "id": "boss-page",
        "type": "page",
        "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/boss-page",
    }

    with patch.object(browser, "_target_payloads", return_value=[payload]), patch(
        "boss_assistant.browser.driver._CdpClient", return_value=client
    ):
        browser._connect_target(target)  # noqa: SLF001

    assert calls == [
        ("Emulation.setFocusEmulationEnabled", {"enabled": True})
    ]
    assert not any(method == "Page.bringToFront" for method, _params in calls)


def test_exe_build_notifies_explorer_to_refresh_both_icons() -> None:
    script = Path("tools/build_exe_nuitka.ps1").read_text(encoding="utf-8-sig")

    assert "-m nuitka" in script
    assert '"--msvc=latest"' in script
    assert "--mingw64" not in script
    assert '"--mode=onefile"' in script
    assert '"--windows-console-mode=disable"' in script
    assert "tools\\obfuscate_strings.py" in script
    assert "assets\\icons\\official" in script
    assert "SHCNE_UPDATEITEM" in script
    assert "SHCNF_PATHW" in script
    assert "Update-ExplorerIcon $target" in script
    assert 'System32\\ie4uinit.exe' in script


def test_formal_concept_a_icons_are_preserved_for_reuse() -> None:
    icon_root = Path("assets/icons/official")
    taskbar_sizes = (16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 96)
    expected_sizes = {
        *((size, size) for size in taskbar_sizes),
        (128, 128),
        (256, 256),
    }

    for stem in ("boss_assistant", "boss_login"):
        assert (icon_root / f"{stem}.png").stat().st_size > 10_000
        icon_path = icon_root / f"{stem}.ico"
        assert icon_path.stat().st_size > 10_000
        with Image.open(icon_path) as icon:
            assert icon.ico.sizes() == expected_sizes
            for size in taskbar_sizes:
                layer = icon.ico.getimage((size, size)).convert("RGBA")
                alpha = layer.getchannel("A")
                corners = (
                    alpha.getpixel((0, 0)),
                    alpha.getpixel((size - 1, 0)),
                    alpha.getpixel((0, size - 1)),
                    alpha.getpixel((size - 1, size - 1)),
                )
                assert corners == (0, 0, 0, 0)
    assert (icon_root / "concept-a-preview.png").is_file()
    readme = (icon_root / "README.md").read_text(encoding="utf-8")
    assert "正式" in readme
    assert "A" in readme
    generator = Path("tools/make_icons.py").read_text(encoding="utf-8")
    assert "SMALL_ICON_SIZES" in generator
    assert "20, 24, 28, 32, 36, 40" in generator
    assert "Image.Resampling.BOX" in generator
    assert "append_images=frames[:-1]" in generator


def test_header_hint_omits_random_wait_copy() -> None:
    source = Path("boss_assistant/gui/app.py").read_text(encoding="utf-8")
    assert "默认实际发送 · 自动查看未读消息 · 达到目标公司数立即停止" in source
    assert "默认实际发送 · 每步随机等待" not in source


def test_gui_enables_dpi_awareness_before_tk_window_creation() -> None:
    source = Path("boss_assistant/gui/app.py").read_text(encoding="utf-8")
    assert "SetProcessDpiAwareness(1)" in source
    constructor = source[source.index("class BossControlPanel"):]
    assert constructor.index("_enable_windows_dpi_awareness()") < constructor.index(
        "super().__init__()"
    )


def test_gui_defaults_example_is_parseable_and_has_no_real_password() -> None:
    from boss_assistant.gui.app import parse_gui_defaults

    text = Path("config/gui_defaults.example.txt").read_text(encoding="utf-8")
    defaults = parse_gui_defaults(text)

    assert defaults["目标岗位方向"]
    assert defaults["目标城市"]
    assert defaults["MySQL数据库"] == "boss_job_assistant"
    assert defaults["MySQL密码"] == ""


def test_win_offline_bundle_manifest_matches_files_and_has_no_python_runtime() -> None:
    bundle = Path("requests-packages")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    packages = manifest["packages"]
    paths = {item["path"] for item in packages}
    lowered = "\n".join(paths).casefold()

    assert manifest["project_version"] == __version__
    assert manifest["target"] == "Windows 10/11 x64"
    assert len(packages) == len(paths) == 6
    assert "adb" not in lowered
    assert "platform-tools" not in lowered
    assert "python" not in lowered
    assert not (bundle / "wheelhouse").exists()
    assert not (bundle / "requirements-offline.txt").exists()
    assert "installers/PowerShell-7.6.4-win-x64.zip" in paths
    assert "installers/MicrosoftEdgeEnterpriseX64-151.0.4129.59.msi" in paths
    assert "installers/mysql-8.0.36-winx64.zip" in paths
    assert "installers/navicat-premium-17.3.11-en-x64.exe" in paths
    assert "installers/codex-x86_64-pc-windows-msvc-0.133.0.exe.zip" in paths

    for package in packages:
        file_path = bundle / package["path"]
        assert file_path.is_file(), package["path"]
        assert file_path.stat().st_size == package["bytes"]
        assert re.fullmatch(r"[0-9A-F]{64}", package["sha256"])


class _RecordingMySqlConnector:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def connect(self, **arguments: object) -> object:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return object()


def test_mysql_store_forces_pure_python_connector_for_nuitka_onefile() -> None:
    connector = _RecordingMySqlConnector()
    store = AutomationMySqlStore(
        MySqlConfig("127.0.0.1", 3306, "test_user", "test_password", "test_db"),
        connector=connector,
    )

    store._connect(with_database=False)
    store._connect(with_database=True)

    assert connector.calls[0]["use_pure"] is True
    assert "database" not in connector.calls[0]
    assert connector.calls[1]["use_pure"] is True
    assert connector.calls[1]["database"] == "test_db"


def test_mysql_store_preserves_connector_error_context() -> None:
    connector = _RecordingMySqlConnector(error=RuntimeError("authentication failed"))
    store = AutomationMySqlStore(
        MySqlConfig("127.0.0.1", 3306, "test_user", "test_password", "test_db"),
        connector=connector,
    )

    with pytest.raises(MySqlStoreError, match="MySQL 连接失败：authentication failed"):
        store._connect(with_database=True)


def test_win_offline_setup_installs_only_host_environment_and_writes_templates() -> None:
    bundle = Path("requests-packages")
    setup = (bundle / "scripts/Setup.ps1").read_text(encoding="utf-8")
    readme = (bundle / "README.md").read_text(encoding="utf-8")
    distribution = (bundle / "scripts/Build-Distribution.ps1").read_text(
        encoding="utf-8"
    )
    bootstrap = (bundle / "scripts/Bootstrap-PowerShell.ps1").read_text(
        encoding="ascii"
    )
    remover = (bundle / "scripts/Remove-DeploymentPackage.ps1").read_text(
        encoding="utf-8-sig"
    )
    delete_launcher = (bundle / "delete.cmd").read_text(encoding="utf-8")

    assert "wheelhouse" not in setup
    assert "python-3" not in setup.casefold()
    assert "Install-Codex-Optional" not in setup
    assert '@("model_api.local.json", "model_api.local.json")' in setup
    assert '@("gui_defaults.txt", "gui_defaults.txt")' in setup
    assert "Read-ConfirmedMySqlPassword" in setup
    assert "至少8个字符" in setup
    assert "已存在且未覆盖" in setup
    assert "不含" in readme and "ADB" in readme
    assert '"config"' in distribution
    assert '".pdf"' in distribution
    assert "PowerShell-7.6.4-win-x64.zip" in bootstrap
    assert "Test-PowerShell7" in bootstrap
    assert "Setup.ps1" in bootstrap
    assert "Expand-Archive" in bootstrap
    assert 'Join-Path $deploymentDriveRoot "BossJobAssistant"' in bootstrap
    assert "DriveType]::Fixed" in bootstrap
    assert "DriveType]::Fixed" in setup
    assert 'Join-Path $environmentRoot "PowerShell\\7"' in bootstrap
    assert 'Join-Path $environmentRoot "MySQL"' in setup
    assert 'Join-Path $environmentRoot "Navicat\\17.3.11"' in setup
    assert "ProgramData" not in setup
    assert '/DIR="{0}"' in setup
    assert 'SetEnvironmentVariable(\n                "Path"' in bootstrap
    assert 'start "" /b "%PWSH_EXE%"' in delete_launcher
    assert "Remove-DeploymentPackage.ps1" in delete_launcher
    assert "%~d0\\BossJobAssistant\\PowerShell\\7\\pwsh.exe" in delete_launcher
    assert "%ProgramFiles%\\PowerShell\\7\\pwsh.exe" in delete_launcher
    assert 'GetFileName($root) -ne "requests-packages"' in remover
    assert 'Join-Path $parent ".git"' in remover
    assert 'Join-Path $parent "boss_assistant"' in remover
    assert "FileOptions]::WriteThrough" in remover
    assert "RandomNumberGenerator" in remover
    assert "$stream.Flush($true)" in remover
    assert 'temporaryName = ".wipe-' in remover
    assert "[IO.Directory]::Delete($root, $false)" in remover
    assert "Clear-RecycleBin" not in remover
    assert "cipher.exe" not in remover.casefold()
    assert "Read-Host" not in remover
    remover_bytes = (bundle / "scripts/Remove-DeploymentPackage.ps1").read_bytes()
    assert remover_bytes.startswith(b"\xef\xbb\xbf")
    for launcher_name in ("验证环境.cmd", "安装Codex（可选）.cmd"):
        launcher = (bundle / launcher_name).read_text(encoding="utf-8")
        assert "%~d0\\BossJobAssistant\\PowerShell\\7\\pwsh.exe" in launcher
    for command_file in bundle.glob("*.cmd"):
        command_bytes = command_file.read_bytes()
        assert not command_bytes.startswith(b"\xef\xbb\xbf"), command_file.name
        assert b"\r\n" in command_bytes, command_file.name
        assert b"\n" not in command_bytes.replace(b"\r\n", b""), command_file.name

    api_template = json.loads(
        (bundle / "templates/model_api.local.json").read_text(encoding="utf-8")
    )
    gui_template = (bundle / "templates/gui_defaults.txt").read_text(encoding="utf-8")
    assert api_template["model"] == "deepseek-v4-flash"
    assert api_template["api_key"] == ""
    assert "MySQL用户名：。" in gui_template
    assert "MySQL密码：。" in gui_template


def test_deployment_package_remover_self_deletes_only_isolated_bundle(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required for the deployment-package remover")

    parent = tmp_path / "offline-distribution"
    bundle = parent / "requests-packages"
    scripts = bundle / "scripts"
    installers = bundle / "installers" / "nested"
    scripts.mkdir(parents=True)
    installers.mkdir(parents=True)
    shutil.copy2(
        "requests-packages/scripts/Remove-DeploymentPackage.ps1",
        scripts / "Remove-DeploymentPackage.ps1",
    )
    shutil.copy2("requests-packages/delete.cmd", bundle / "delete.cmd")
    (bundle / "一键部署.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (bundle / "manifest.json").write_text("{}\n", encoding="utf-8")
    (scripts / "Setup.ps1").write_text("# marker\n", encoding="utf-8")
    (installers / "payload.bin").write_bytes(b"deployment-payload" * 131_072)
    outside = parent / "must-remain.txt"
    outside.write_text("keep", encoding="utf-8")

    result = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "Remove-DeploymentPackage.ps1"),
            "-PackageRoot",
            str(bundle),
            "-NoCompletionPopup",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert not bundle.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_codex_provider_reuses_installed_logged_in_cli(tmp_path: Path) -> None:
    codex = tmp_path / "codex.exe"
    codex.write_bytes(b"placeholder")
    commands: list[list[str]] = []

    def runner(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.133.0", stderr="")

    with patch.object(CodexCliReviewProvider, "_configured_model", return_value="gpt-5.5"):
        provider = CodexCliReviewProvider(
            directory=tmp_path / "reviews",
            workspace=tmp_path,
            codex_path=codex,
            preflight_runner=runner,
        )

    assert provider.codex_path == str(codex.resolve())
    assert commands == [
        [str(codex.resolve()), "--version"],
        [str(codex.resolve()), "login", "status"],
    ]


def test_codex_provider_rejects_installed_but_logged_out_cli(tmp_path: Path) -> None:
    codex = tmp_path / "codex.exe"
    codex.write_bytes(b"placeholder")

    def runner(command: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1 if command[-2:] == ["login", "status"] else 0,
            stdout="",
            stderr="",
        )

    with pytest.raises(ReviewError, match="尚未登录"):
        CodexCliReviewProvider(
            directory=tmp_path / "reviews",
            codex_path=codex,
            preflight_runner=runner,
        )


def test_build_only_string_transform_hides_sensitive_literals() -> None:
    from tools.obfuscate_strings import (
        _docstring_node_ids,
        _insert_decoder_import,
        _StringTransformer,
    )

    sensitive = "这是只应存在于构建前源码中的一段核心模型审核提示"
    tree = ast.parse(
        "class Example:\n"
        "    def value(self):\n"
        f"        return {sensitive!r}\n"
    )
    transformer = _StringTransformer(_docstring_node_ids(tree))
    transformed = transformer.visit(tree)
    _insert_decoder_import(transformed)
    ast.fix_missing_locations(transformed)
    rendered = ast.unparse(transformed)

    assert sensitive not in rendered
    assert "from boss_assistant._protected_strings import decode as _ps_decode" in rendered
    assert "_ps_decode(" in rendered
    assert "__ps" not in rendered
    assert sensitive in transformer.values.values()


def _card(**overrides: object) -> JobCard:
    values: dict[str, object] = {
        "job_name": "Python后端开发工程师",
        "company_name": "示例科技",
        "salary": "15-25K",
        "location": "广州·天河区",
        "recruiter_activity": "今日活跃",
        "tags": ("1-3年", "本科"),
        "fingerprint": "card-1",
        "experience": "1-3年",
        "degree": "本科",
        "job_id": "job-1",
        "company_scale": "100-499人",
    }
    values.update(overrides)
    return JobCard(**values)


class _Element:
    def __init__(self, name: str = "", *, displayed: bool = True) -> None:
        self.name = name
        self.displayed = displayed
        self.typed: list[str] = []

    def send_keys(self, value: str) -> None:
        self.typed.append(value)

    def is_displayed(self) -> bool:
        return self.displayed


class _SelectorBrowser:
    def __init__(
        self,
        *,
        cards: list[dict] | None = None,
        expectations: list[dict] | None = None,
    ) -> None:
        self.cards = cards or []
        self.expectations = expectations or []
        self.elements: dict[str, _Element] = {}

    def js(self, script: str, *_args: object) -> object:
        if "data-bossidx" in script:
            return self.cards
        if "data-bossexp" in script:
            return self.expectations
        raise AssertionError("unexpected script")

    def find_css(self, css: str) -> _Element:
        return self.elements.setdefault(css, _Element(css))


def test_card_fingerprint_is_stable_and_company_job_based() -> None:
    left = build_card_fingerprint("Python开发", "示例科技", "10K", "广州")
    right = build_card_fingerprint("Python开发", "示例科技", "20K", "深圳")
    assert left == right


def test_extract_job_cards_reads_fields_and_deduplicates() -> None:
    entry = {
        "idx": 0,
        "job_name": "Python开发",
        "company_name": "示例科技",
        "salary": "\ue032\ue031-\ue033\ue036K",
        "location": "广州·天河区",
        "recruiter_activity": "在线",
        "tags": ["3-5年", "本科", "Python"],
        "job_id": "abc",
        "href": "/job_detail/abc.html",
    }
    browser = _SelectorBrowser(cards=[entry, {**entry, "idx": 1}])
    cards = extract_job_cards(browser)  # type: ignore[arg-type]
    assert len(cards) == 1
    assert cards[0].experience == "3-5年"
    assert cards[0].degree == "本科"
    assert cards[0].job_id == "abc"
    assert cards[0].detail_url == "/job_detail/abc.html"
    assert cards[0].salary == "10-25K"
    assert cards[0].recruiter_activity == "在线"


def test_extract_job_cards_reads_company_scale_from_page_component_data() -> None:
    entry = {
        "idx": 0,
        "job_name": "Python开发",
        "company_name": "示例科技",
        "salary": "8-13K",
        "location": "广州",
        "tags": ["1-3年", "本科"],
        "job_id": "job-abc",
        "href": "/job_detail/job-abc.html",
        "company_scale": "100-499人",
    }

    class _ScaleBrowser(_SelectorBrowser):
        def js(self, script: str, *args: object) -> object:
            if "data-bossidx" in script:
                assert "brandScaleName" in script
                assert args[-1] is True
                return [dict(entry)]
            raise AssertionError("unexpected script")

    cards = extract_job_cards(  # type: ignore[arg-type]
        _ScaleBrowser(),
        include_company_scale=True,
    )

    assert cards[0].company_scale == "100-499人"


def test_kanzhun_private_salary_digits_are_decoded() -> None:
    assert _clean("\ue03a-\ue032\ue033K·\ue032\ue034薪") == "9-12K·13薪"


@pytest.mark.parametrize(
    ("salary", "expected"),
    (
        ("6-8K", True),
        ("8-13K", True),
        ("12-13K", True),
        ("5-8K", True),
        ("5-13K", True),
        ("13-18K", False),
        ("9-15K", False),
        ("4-8K", False),
    ),
)
def test_salary_range_requires_the_whole_job_range_inside_settings(
    salary: str,
    expected: bool,
) -> None:
    assert salary_meets(salary, 5, 13) is expected


def test_salary_range_parses_extra_months_but_rejects_non_k_pay() -> None:
    assert salary_range_k("10-15K·13薪") == (10.0, 15.0)
    assert salary_range_k("200-300元/天") is None
    assert not salary_meets(None, 5, 13)


@pytest.mark.parametrize(
    ("scale", "expected"),
    (
        ("0-20人", False),
        ("20-99人", True),
        ("100-499人", True),
        ("10000人以上", True),
        (None, False),
    ),
)
def test_company_scale_uses_the_boss_category_lower_bound(
    scale: str | None,
    expected: bool,
) -> None:
    assert company_size_meets(scale, 20) is expected


def test_company_scale_parser_supports_open_ended_category() -> None:
    assert company_size_range("20-99人") == (20, 99)
    assert company_size_range("10000人以上") == (10000, None)


def test_card_gate_applies_salary_and_company_scale_before_model_review() -> None:
    policy = AutomationPolicy(
        excluded_companies=(),
        allowed_job_keywords=("Python",),
        allowed_locations=("广州",),
        target_companies=1,
        salary_min_k=5,
        salary_max_k=13,
        minimum_company_size=20,
    )

    assert card_rejection_reason(_card(salary="8-13K"), policy) is None
    assert "薪资范围不符" in str(
        card_rejection_reason(_card(salary="13-18K"), policy)
    )
    assert "公司规模不符" in str(
        card_rejection_reason(
            _card(salary="8-13K", company_scale="0-20人"),
            policy,
        )
    )


def test_parse_and_find_city_expectation() -> None:
    browser = _SelectorBrowser(
        expectations=[
            {"idx": 0, "text": "广州·AI应用开发"},
            {"idx": 1, "text": "深圳·Python后端"},
        ]
    )
    intents = parse_job_intents(browser)  # type: ignore[arg-type]
    assert [(item.city, item.role) for item in intents.expectations] == [
        ("广州", "AI应用开发"),
        ("深圳", "Python后端"),
    ]
    element = find_expectation_element(  # type: ignore[arg-type]
        browser, "深圳", role="Python后端"
    )
    assert element is browser.elements["[data-bossexp='1']"]


def test_expectation_active_check_uses_city_and_role() -> None:
    calls: list[tuple[object, ...]] = []

    class _ActiveBrowser:
        @staticmethod
        def js(*args: object) -> bool:
            calls.append(args)
            return True

    assert expectation_is_active(  # type: ignore[arg-type]
        _ActiveBrowser(), "广州", role="Python"
    )
    assert calls[0][-2:] == ("广州", "Python")


def test_parse_role_city_parentheses_expectation() -> None:
    browser = _SelectorBrowser(
        expectations=[
            {"idx": 0, "text": "IT技术支持(广州)"},
            {"idx": 1, "text": "Python（深圳）"},
        ]
    )
    intents = parse_job_intents(browser)  # type: ignore[arg-type]
    assert [(item.city, item.role) for item in intents.expectations] == [
        ("广州", "IT技术支持"),
        ("深圳", "Python"),
    ]


def test_missing_location_is_blocked_before_model_review() -> None:
    reason = card_rejection_reason(
        _card(location=None),
        _policy(),
        resume_degree_level=6,
    )
    assert reason == "工作地点未识别，不能确认符合目标城市"


def test_ungrounded_hr_match_is_blocked() -> None:
    review = CardReviewResult(
        eligible=True,
        job_direction_match=True,
        location_match=True,
        reason="模型误判",
        combined_directions=("Python", "AI", "后端开发"),
        matched_direction_keywords=("HR", "文员"),
    )
    reason = card_review_rejection_reason(
        _card(job_name="HR文员6k+提成/可实习"),
        review,
    )
    assert reason is not None
    assert "卡片方向硬校验不通过" in reason


def test_grounded_direction_and_location_pass() -> None:
    review = CardReviewResult(
        eligible=True,
        job_direction_match=True,
        location_match=True,
        reason="匹配",
        combined_directions=("Python", "AI应用开发"),
        matched_direction_keywords=("Python",),
    )
    assert card_review_rejection_reason(_card(), review) is None


def test_api_parser_applies_local_card_gate() -> None:
    provider = OpenAICompatibleReviewProvider(
        ApiProviderConfig(enabled=True, model="test-model", api_key="test-key"),
        transport=lambda *_args: {},
    )
    response = {
        "job_direction_match": True,
        "location_match": True,
        "excluded_direction_match": False,
        "resume_inferred_directions": ["后端开发"],
        "combined_directions": ["Python", "AI", "后端开发"],
        "matched_direction_keywords": ["HR", "文员"],
        "matched_excluded_direction_keywords": [],
        "reason": "模型误判",
    }
    result = provider._parse_card_response(  # noqa: SLF001
        response,
        _card(job_name="HR文员6k+提成/可实习"),
        _policy(),
    )
    assert not result.eligible
    assert "卡片方向硬校验不通过" in result.reason


def test_api_incomplete_response_is_audited_and_retried(tmp_path: Path) -> None:
    response_body = json.dumps(
        {
            "choices": [
                {
                    "message": {"content": json.dumps({"result": "ok"})},
                    "finish_reason": "stop",
                }
            ]
        }
    ).encode("utf-8")
    calls = 0

    class _Response:
        def __init__(self, *, incomplete: bool) -> None:
            self.incomplete = incomplete

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            if self.incomplete:
                raise IncompleteRead(b"")
            return response_body

    def fake_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(incomplete=calls == 1)

    sleeps: list[float] = []
    provider = OpenAICompatibleReviewProvider(
        ApiProviderConfig(
            enabled=True,
            model="test-model",
            api_key="test-key",
            max_retries=2,
            retry_base_seconds=0.25,
        ),
        sleep=sleeps.append,
        audit_directory=tmp_path,
    )

    with patch(
        "boss_assistant.automation.api_provider.urlopen",
        side_effect=fake_urlopen,
    ):
        result = provider._exchange(  # noqa: SLF001
            "card_review",
            ("测试",),
            {"job": "Python开发"},
            {"result": "string"},
        )

    assert result == {"result": "ok"}
    assert calls == 2
    assert sleeps == [0.25]
    audits = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(tmp_path.glob("*.json"))
    ]
    failed = next(item for item in audits if item["error"])
    assert failed["attempt"] == 1
    assert failed["raw_response"] is None
    assert "响应传输不完整" in failed["error"]
    assert any(item["attempt"] == 2 and item["error"] is None for item in audits)


class _DetailBrowser:
    current_url = "https://www.zhipin.com/web/geek/job-detail/abc"

    def js(self, _script: str, *_args: object) -> dict[str, object]:
        return {
            "job_name": "Python后端工程师",
            "company_name": "示例科技",
            "salary": "15-25K",
            "location": "广州·天河区",
            "experience": "",
            "description": "负责Python服务开发\n双休",
            "tags": ["3-5年", "本科"],
            "node_count": 321,
        }

    def page_text(self) -> str:
        return "fallback"


def test_detail_reader_builds_complete_page_without_expand_action() -> None:
    snapshot = read_job_detail(_DetailBrowser())  # type: ignore[arg-type]
    assert snapshot.job_data.is_boss_job_detail_page
    assert snapshot.job_data.experience.value == "3-5年"
    assert "负责Python服务开发" in (snapshot.job_description or "")
    assert "3-5年" in (snapshot.job_description or "")


def test_detail_reader_normalizes_fixed_panel_company_text() -> None:
    class _FixedPanelBrowser(_DetailBrowser):
        def js(self, _script: str, *_args: object) -> dict[str, object]:
            data = super().js(_script, *_args)
            data["company_name"] = "示例科技 · 人事"
            data["location"] = "广州天河区软件园"
            return data

    snapshot = read_job_detail(_FixedPanelBrowser())  # type: ignore[arg-type]
    assert snapshot.company_name == "示例科技"
    assert snapshot.location == "广州天河区软件园"


def test_stream_typing_sends_one_character_at_a_time() -> None:
    browser = EdgeBrowser()
    element = _Element()
    with patch("boss_assistant.browser.driver.time.sleep"), patch(
        "boss_assistant.browser.driver.random.uniform", return_value=0.0
    ):
        browser.type_stream(element, "您好，Boss")  # type: ignore[arg-type]
    assert element.typed == list("您好，Boss")


def test_context_click_uses_live_element_rect_and_right_mouse_button() -> None:
    browser = EdgeBrowser()
    browser.driver = SimpleNamespace()
    calls: list[tuple[str, dict[str, object] | None]] = []
    with patch.object(
        browser,
        "_element_rect",
        return_value={"x": 123.0, "y": 456.0},
    ), patch.object(
        browser,
        "_cdp_call",
        side_effect=lambda method, params=None: calls.append((method, params)) or {},
    ):
        browser.context_click(_Element(), description="会话")  # type: ignore[arg-type]

    assert [params["type"] for _method, params in calls if params] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert calls[1][1]["button"] == "right"  # type: ignore[index]
    assert calls[2][1]["button"] == "right"  # type: ignore[index]


def test_hover_uses_live_element_rect_and_only_moves_mouse() -> None:
    browser = EdgeBrowser()
    browser.driver = SimpleNamespace()
    calls: list[tuple[str, dict[str, object] | None]] = []
    with patch.object(
        browser,
        "_element_rect",
        return_value={"x": 123.0, "y": 456.0},
    ), patch.object(
        browser,
        "_cdp_call",
        side_effect=lambda method, params=None: calls.append((method, params)) or {},
    ):
        browser.hover(_Element(), description="会话")  # type: ignore[arg-type]

    assert calls == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": 123.0, "y": 456.0},
        )
    ]


def test_browser_wait_for_retries_transient_cdp_browser_error() -> None:
    browser = EdgeBrowser()
    attempts = iter(
        (
            BrowserError("页面短时不可用"),
            BrowserError("元素尚未挂载"),
            True,
        )
    )

    def predicate(_browser: EdgeBrowser) -> bool:
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("boss_assistant.browser.driver.time.sleep"):
        assert browser.wait_for(
            predicate,
            timeout=1.0,
            description="网络延迟后的页面元素",
            poll=0.01,
        ) is True


def test_runner_wait_retries_transient_page_read_error() -> None:
    browser = EdgeBrowser()
    runner = BossAutomationRunner(
        browser,
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    attempts = iter(
        (
            PageReadError("详情字段尚未渲染"),
            True,
        )
    )

    def predicate(_browser: EdgeBrowser) -> bool:
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("boss_assistant.browser.driver.time.sleep"):
        assert runner._wait(predicate, "岗位详情") is True  # noqa: SLF001


def test_greeting_send_waits_before_send_action() -> None:
    class _ChatBrowser:
        driver = None

        def __init__(self) -> None:
            self.clicked: list[tuple[object, str]] = []
            self.typed = ""

        def click(self, element, *, description=""):
            self.clicked.append((element, description))

        @staticmethod
        def clear_editor(_element) -> None:
            return None

        def type_stream(self, _element, text, *, control_point=None):
            self.typed = text

        def editor_value(self, _element) -> str:
            return self.typed

    browser = _ChatBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    editor = _Element("editor")
    send_button = _Element("send")
    greeting = "您好，我有相关项目经验，希望与您进一步沟通该岗位"

    with patch.object(
        runner,
        "_wait",
        side_effect=(editor, send_button, True),
    ), patch.object(
        runner,
        "_pause_before",
        return_value=1.2,
    ) as pause_before, patch.object(
        runner,
        "_greeting_in_messages",
        return_value=False,
    ), patch(
        "boss_assistant.automation.runner.find_send_button",
        return_value=send_button,
    ):
        runner._fill_or_send_greeting(greeting, send=True, open_chat=False)  # noqa: SLF001

    assert browser.typed == greeting
    assert browser.clicked[-1] == (send_button, "发送招呼语")
    assert "发送招呼语" in [
        call.args[0] for call in pause_before.call_args_list
    ]


def test_greeting_editor_is_repaired_before_any_send_when_stream_reorders_text() -> None:
    class _ChatBrowser:
        driver = None

        def __init__(self) -> None:
            self.typed = ""
            self.repaired: list[str] = []
            self.clicked: list[tuple[object, str]] = []

        @staticmethod
        def clear_editor(_element) -> None:
            return None

        def type_stream(self, _element, text, *, control_point=None):
            self.typed = text.replace("TypeScript", "Typecript") + "S"

        def editor_value(self, _element) -> str:
            return self.typed

        def set_value(self, _element, text: str) -> None:
            self.typed = text
            self.repaired.append(text)

        def click(self, element, *, description="") -> None:
            self.clicked.append((element, description))

    browser = _ChatBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    editor = _Element("editor")
    send_button = _Element("send")
    greeting = "您好，我有全栈开发经验，熟悉TypeScript，希望与您进一步沟通该岗位"

    def click_ready(_finder, description, **_kwargs):
        element = editor if "输入框" in description else send_button
        if element is send_button:
            browser.click(element, description=description)
        return element

    with patch.object(
        runner, "_click_when_ready", side_effect=click_ready
    ), patch.object(
        runner, "_wait_for_displayed", return_value=editor
    ), patch.object(
        runner, "_wait", side_effect=lambda predicate, *_args, **_kwargs: predicate(None)
    ), patch.object(
        runner, "_pause_before", return_value=1.2
    ), patch.object(
        runner, "_greeting_in_messages", side_effect=(False, True)
    ):
        runner._fill_or_send_greeting(greeting, send=True, open_chat=False)  # noqa: SLF001

    assert browser.repaired == [greeting]
    assert browser.typed == greeting
    assert browser.clicked == [(send_button, "发送招呼语")]


def test_communication_quota_notice_requires_exact_text_and_visible_button() -> None:
    dialog = _Element("dialog")
    confirm = _Element("confirm")

    class _QuotaBrowser:
        def __init__(self, text: str) -> None:
            self.text = text

        def find_all_css(self, selector):
            return [confirm] if "sure-btn" in selector else [dialog]

        def text_of(self, _element) -> str:
            return self.text

    notice = read_communication_quota_notice(
        _QuotaBrowser(
            "温馨提示 您今天已与120位BOSS沟通，还剩30次沟通机会哦 好"
        )  # type: ignore[arg-type]
    )

    assert notice is not None
    assert notice.contacted_count == 120
    assert notice.remaining_count == 30
    assert notice.confirm_button is confirm
    assert (
        read_communication_quota_notice(
            _QuotaBrowser("温馨提示 是否确认发送简历 好")  # type: ignore[arg-type]
        )
        is None
    )


def test_daily_150_communication_limit_requires_terminal_semantics() -> None:
    dialog = _Element("dialog")
    confirm = _Element("confirm")

    class _LimitBrowser:
        def __init__(self, text: str, button_text: str = "确定") -> None:
            self.text = text
            self.button_text = button_text

        def find_all_css(self, selector):
            return [confirm] if "sure-btn" in selector else [dialog]

        def text_of(self, element) -> str:
            return self.button_text if element is confirm else self.text

    notice = read_communication_quota_notice(
        _LimitBrowser(
            "您已达到沟通上限\n您今天已与150位BOSS沟通，休息一下，明天再来吧～\n确定"
        )  # type: ignore[arg-type]
    )

    assert notice is not None
    assert notice.contacted_count == 150
    assert notice.remaining_count is None
    assert notice.limit_reached is True
    assert (
        read_communication_quota_notice(
            _LimitBrowser(
                "您已达到沟通上限 您今天已与149位BOSS沟通，休息一下，明天再来吧～"
            )  # type: ignore[arg-type]
        )
        is None
    )
    assert (
        read_communication_quota_notice(
            _LimitBrowser(
                "您已达到沟通上限 您今天已与150位BOSS沟通，休息一下，明天再来吧～",
                button_text="取消",
            )  # type: ignore[arg-type]
        )
        is None
    )


def test_communication_limit_skips_hidden_stale_dialog_and_button() -> None:
    hidden_dialog = _Element("hidden-dialog", displayed=False)
    visible_dialog = _Element("visible-dialog")
    hidden_confirm = _Element("hidden-confirm", displayed=False)
    visible_confirm = _Element("visible-confirm")

    class _StaleDialogBrowser:
        @staticmethod
        def find_all_css(selector):
            if "sure-btn" in selector:
                return [hidden_confirm, visible_confirm]
            return [hidden_dialog, visible_dialog]

        @staticmethod
        def text_of(element) -> str:
            if element is visible_dialog:
                return (
                    "您已达到沟通上限 您今天已与150位BOSS沟通，"
                    "休息一下，明天再来吧～ 确定"
                )
            if element is visible_confirm:
                return "确定"
            return "旧弹窗"

    notice = read_communication_quota_notice(
        _StaleDialogBrowser()  # type: ignore[arg-type]
    )

    assert notice is not None
    assert notice.limit_reached is True
    assert notice.confirm_button is visible_confirm


def test_daily_150_limit_is_confirmed_and_stops_without_retry() -> None:
    class _LimitBrowser:
        driver = None

        def __init__(self) -> None:
            self.clicked: list[tuple[object, str]] = []

        def click(self, element, *, description=""):
            self.clicked.append((element, description))

    browser = _LimitBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    confirm = _Element("limit-confirm")
    notice = CommunicationQuotaNotice(
        contacted_count=150,
        remaining_count=None,
        text="您已达到沟通上限 您今天已与150位BOSS沟通，明天再来吧～",
        confirm_button=confirm,  # type: ignore[arg-type]
        limit_reached=True,
    )

    with patch.object(
        runner,
        "_pause_before",
        return_value=1.0,
    ), patch.object(
        runner,
        "_wait",
        return_value=True,
    ), patch(
        "boss_assistant.automation.runner.read_communication_quota_notice",
        return_value=notice,
    ):
        with pytest.raises(DailyCommunicationLimitReachedError):
            runner._dismiss_communication_quota_notice_if_present()  # noqa: SLF001

    assert browser.clicked == [(confirm, "关闭150位沟通上限弹窗")]
    assert runner.completion_reason == DAILY_COMMUNICATION_LIMIT_REASON


def test_daily_150_limit_still_stops_when_confirm_click_fails() -> None:
    class _FailingBrowser:
        driver = None

        @staticmethod
        def click(_element, *, description="") -> None:
            raise BrowserError("确认按钮点击失败")

    runner = BossAutomationRunner(
        _FailingBrowser(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    notice = CommunicationQuotaNotice(
        contacted_count=150,
        remaining_count=None,
        text="您已达到沟通上限 您今天已与150位BOSS沟通，明天再来吧～",
        confirm_button=_Element("limit-confirm"),  # type: ignore[arg-type]
        limit_reached=True,
    )

    with patch.object(
        runner,
        "_pause_before",
        return_value=1.0,
    ), patch(
        "boss_assistant.automation.runner.read_communication_quota_notice",
        return_value=notice,
    ):
        with pytest.raises(DailyCommunicationLimitReachedError):
            runner._dismiss_communication_quota_notice_if_present()  # noqa: SLF001

    assert runner.completion_reason == DAILY_COMMUNICATION_LIMIT_REASON
    assert runner.completion_warning is not None
    assert "确认按钮点击失败" in runner.completion_warning


def test_daily_150_limit_completion_text_is_running_ended() -> None:
    assert format_run_completion(
        DAILY_COMMUNICATION_LIMIT_REASON,
        Path("run.json"),
    ) == (
        "运行结束",
        "结束理由：已达到150沟通上限；运行记录：run.json",
    )
    assert format_run_completion(
        DAILY_COMMUNICATION_LIMIT_REASON,
        Path("run.json"),
        "弹窗关闭失败",
    ) == (
        "运行结束",
        "结束理由：已达到150沟通上限；提示：弹窗关闭失败；运行记录：run.json",
    )


def test_daily_150_limit_finishes_run_and_persists_reason(tmp_path: Path) -> None:
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        run_directory=tmp_path,
        policy=_policy(),
        review_provider=SimpleNamespace(),  # type: ignore[arg-type]
    )
    runner.ensure_ready = lambda: None  # type: ignore[method-assign]
    runner.read_job_intents = lambda: JobIntentData(  # type: ignore[method-assign]
        (JobExpectation("广州", "Python", None, ()),)
    )

    def reach_limit(*_args, **_kwargs) -> None:
        runner.completion_reason = DAILY_COMMUNICATION_LIMIT_REASON
        runner.completion_warning = "弹窗关闭校验失败"
        raise DailyCommunicationLimitReachedError

    runner.open_recommendations = reach_limit  # type: ignore[method-assign]

    stats, log_path = runner.run()

    assert stats.sent == 0
    assert runner.completion_reason == DAILY_COMMUNICATION_LIMIT_REASON
    run_payload = json.loads(log_path.read_text(encoding="utf-8"))
    checkpoint_payload = json.loads(
        (tmp_path / "boss_checkpoint.json").read_text(encoding="utf-8")
    )
    assert run_payload["completion_reason"] == DAILY_COMMUNICATION_LIMIT_REASON
    assert run_payload["completion_warning"] == "弹窗关闭校验失败"
    assert checkpoint_payload["status"] == "completed"
    assert (
        checkpoint_payload["completion_reason"]
        == DAILY_COMMUNICATION_LIMIT_REASON
    )
    assert checkpoint_payload["completion_warning"] == "弹窗关闭校验失败"


def test_runner_uses_shared_stats_when_generic_failure_escapes(
    tmp_path: Path,
) -> None:
    shared_stats = AutomationStats()
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        run_directory=tmp_path,
        config=AutomationConfig(inspect_unread_messages=True),
        policy=_policy(),
        review_provider=SimpleNamespace(),  # type: ignore[arg-type]
        stats=shared_stats,
    )
    runner.ensure_ready = lambda: None  # type: ignore[method-assign]
    runner.read_job_intents = lambda: JobIntentData(  # type: ignore[method-assign]
        (JobExpectation("广州", "Python", None, ()),)
    )
    runner.open_recommendations = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    def fail_after_progress(stats: AutomationStats, **_kwargs) -> None:
        stats.inspected = 7
        raise BossAutomationError("模拟运行中断")

    runner.process_unread_messages = fail_after_progress  # type: ignore[method-assign]

    with pytest.raises(BossAutomationError, match="模拟运行中断"):
        runner.run()

    assert shared_stats.inspected == 7
    run_path = next(tmp_path.glob("boss_run_*.json"))
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    assert run_payload["stats"]["inspected"] == 7


def test_quota_notice_is_closed_then_original_chat_click_is_retried() -> None:
    class _ChatBrowser:
        driver = None

        def __init__(self) -> None:
            self.clicked: list[tuple[object, str]] = []
            self.typed = ""

        def click(self, element, *, description=""):
            self.clicked.append((element, description))

        @staticmethod
        def clear_editor(_element) -> None:
            return None

        def type_stream(self, _element, text, *, control_point=None):
            self.typed = text

        def editor_value(self, _element) -> str:
            return self.typed

    browser = _ChatBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    entry = _Element("entry")
    editor = _Element("editor")
    confirm = _Element("quota-confirm")
    notice = CommunicationQuotaNotice(
        contacted_count=120,
        remaining_count=30,
        text="您今天已与120位BOSS沟通，还剩30次沟通机会哦",
        confirm_button=confirm,  # type: ignore[arg-type]
    )
    greeting = "您好，我有相关项目经验，希望与您进一步沟通该岗位"

    with patch.object(
        runner,
        "_click_when_ready",
        side_effect=(entry, entry, editor),
    ) as click_when_ready, patch.object(
        runner,
        "_wait",
        side_effect=(
            "quota_notice",
            True,
            ElementNotFoundError("未自动进入聊天"),
            "editor",
        ),
    ), patch.object(
        runner,
        "_pause_before",
        return_value=1.2,
    ), patch.object(
        runner,
        "_sleep",
    ), patch(
        "boss_assistant.automation.runner.read_communication_quota_notice",
        return_value=notice,
    ):
        runner._fill_or_send_greeting(greeting, send=False)  # noqa: SLF001

    descriptions = [
        call.args[1] for call in click_when_ready.call_args_list
    ]
    assert descriptions[:2] == [
        "点击“立即沟通”",
        "关闭沟通提醒后重新点击“立即沟通”",
    ]
    assert browser.clicked == [(confirm, "关闭当日沟通次数提醒")]
    assert browser.typed == greeting


def test_quota_notice_uses_automatic_chat_transition_without_second_click() -> None:
    class _ChatBrowser:
        driver = None

        def __init__(self) -> None:
            self.clicked: list[tuple[object, str]] = []

        def click(self, element, *, description=""):
            self.clicked.append((element, description))

    browser = _ChatBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    entry = _Element("entry")
    editor = _Element("editor")
    confirm = _Element("quota-confirm")
    notice = CommunicationQuotaNotice(
        contacted_count=120,
        remaining_count=30,
        text="您今天已与120位BOSS沟通，还剩30次沟通机会哦",
        confirm_button=confirm,  # type: ignore[arg-type]
    )

    with patch.object(
        runner,
        "_click_when_ready",
        return_value=entry,
    ) as click_when_ready, patch.object(
        runner,
        "_wait",
        side_effect=("quota_notice", True, editor),
    ), patch.object(
        runner,
        "_pause_before",
        return_value=1.2,
    ), patch.object(
        runner,
        "_sleep",
    ), patch(
        "boss_assistant.automation.runner.read_communication_quota_notice",
        return_value=notice,
    ):
        runner._open_chat_with_quota_notice_retry()  # noqa: SLF001

    assert click_when_ready.call_count == 1
    assert browser.clicked == [(confirm, "关闭当日沟通次数提醒")]


def test_open_chat_reselects_expected_detail_once_when_review_redraws_panel() -> None:
    browser = SimpleNamespace(
        driver=None,
        current_url="https://www.zhipin.com/web/geek/jobs",
    )
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    card = _card()
    entry = _Element("entry")

    with patch(
        "boss_assistant.automation.runner.find_greeting_editor", return_value=None
    ), patch.object(
        runner,
        "_click_when_ready",
        side_effect=(ElementNotFoundError("旧详情无入口"), entry),
    ) as click_when_ready, patch.object(
        runner, "_wait", side_effect=(True, "editor")
    ), patch.object(
        runner, "_sleep"
    ), patch(
        "boss_assistant.automation.runner.select_job_card_inline", return_value=True
    ):
        runner._open_chat_with_quota_notice_retry(expected_card=card)  # noqa: SLF001

    descriptions = [call.args[1] for call in click_when_ready.call_args_list]
    assert descriptions == ["点击“立即沟通”", "重新定位后点击“立即沟通”"]


def test_open_chat_retries_once_when_click_is_swallowed_on_same_detail() -> None:
    browser = SimpleNamespace(
        driver=None,
        current_url="https://www.zhipin.com/web/geek/jobs",
    )
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    card = _card()
    entry = _Element("entry")

    with patch(
        "boss_assistant.automation.runner.find_greeting_editor", return_value=None
    ), patch.object(
        runner, "_click_when_ready", return_value=entry
    ) as click_when_ready, patch.object(
        runner,
        "_wait",
        side_effect=(ElementNotFoundError("点击后未切换"), "editor"),
    ), patch.object(
        runner, "_detail_chat_entry", return_value=entry
    ), patch.object(
        runner, "_sleep"
    ):
        runner._open_chat_with_quota_notice_retry(expected_card=card)  # noqa: SLF001

    descriptions = [call.args[1] for call in click_when_ready.call_args_list]
    assert descriptions == [
        "点击“立即沟通”",
        "首次点击未切换后重新点击沟通入口",
    ]


@pytest.mark.parametrize("prefix", ("已读 ", "送达 ", "[已读]", "[送达]"))
def test_greeting_confirmation_ignores_delivery_status_prefix(prefix: str) -> None:
    greeting = "您好，我有相关项目经验，希望与您进一步沟通该岗位"
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )

    with patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=(ChatMessage(prefix + greeting, True, 0),),
    ):
        assert runner._greeting_in_messages(greeting) is True  # noqa: SLF001


def test_click_when_ready_relocates_after_delay_and_ignores_hidden_element() -> None:
    hidden = _Element("hidden", displayed=False)
    visible = _Element("visible", displayed=True)

    class _Browser:
        def __init__(self) -> None:
            self.clicked: list[object] = []

        def click(self, element: object, *, description: str = "") -> None:
            self.clicked.append(element)

    browser = _Browser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    elements = iter((hidden, visible))
    runner._pause_before = lambda *_args, **_kwargs: 1.0  # type: ignore[method-assign]  # noqa: SLF001

    def wait_until_visible(predicate, _description, timeout=None):
        assert predicate(browser) is None
        assert predicate(browser) is visible
        return visible

    runner._wait = wait_until_visible  # type: ignore[method-assign]  # noqa: SLF001

    result = runner._click_when_ready(  # noqa: SLF001
        lambda: next(elements),
        "打开会话",
    )

    assert result is visible
    assert browser.clicked == [visible]


def test_pause_before_sleeps_for_generated_delay() -> None:
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        config=AutomationConfig(
            action_delay_min_seconds=1.25,
            action_delay_max_seconds=1.25,
        ),
    )

    with patch.object(runner, "_sleep") as sleep, patch(
        "boss_assistant.automation.runner.random.uniform",
        return_value=1.25,
    ):
        delay = runner._pause_before("执行测试动作")  # noqa: SLF001

    assert delay == 1.25
    sleep.assert_called_once_with(1.25)


def test_current_chat_conversation_shape_reads_notice_badge_and_identity() -> None:
    class _ConversationBrowser:
        def __init__(self) -> None:
            self.args: tuple[object, ...] = ()

        def js(self, _script: str, *args: object) -> object:
            self.args = args
            return [
                {
                    "idx": 8,
                    "name": "郑晓雁",
                    "company": "天讯瑞达",
                    "position": "HR",
                    "last": "可以发一份简历吗",
                    "unread": 1,
                    "pinned": False,
                }
            ]

    browser = _ConversationBrowser()
    conversations = extract_chat_conversations(browser)  # type: ignore[arg-type]

    assert len(conversations) == 1
    assert conversations[0].recruiter_name == "郑晓雁"
    assert conversations[0].company_name == "天讯瑞达"
    assert conversations[0].position_name == "HR"
    assert conversations[0].unread_count == 1
    assert ".notice-badge" in SELECTORS["chat_conv_badge"]
    assert ".notice-badge" in browser.args[-1]


def test_resume_request_accept_never_falls_back_to_generic_agree_button() -> None:
    class _ContactCardBrowser:
        @staticmethod
        def js(_script: str, *_args: object) -> bool:
            # 联系方式卡片存在“同意”，但没有被严格的附件简历脚本标记。
            return False

        @staticmethod
        def find_css(_selector: str):
            raise AssertionError("没有附件简历标记时不应查找或点击任何同意按钮")

    assert (
        resume_request_accept_button(_ContactCardBrowser()) is None  # type: ignore[arg-type]
    )


def test_live_resume_request_card_class_is_collected_as_system_context() -> None:
    class _Card:
        text = "我想要一份您的附件简历，您是否同意 拒绝 同意"

    class _ResumeCardBrowser:
        @staticmethod
        def find_all_first_css(selectors: list[str]):
            assert ".message-card-wrap" in selectors
            return [_Card()]

        @staticmethod
        def text_of(element: _Card) -> str:
            return element.text

    notes = extract_chat_system_notes(_ResumeCardBrowser())  # type: ignore[arg-type]

    assert notes == ("我想要一份您的附件简历，您是否同意 拒绝 同意",)


def test_open_conversation_waits_for_target_identity_and_all_unread_messages() -> None:
    conversation = _conversation(
        recruiter_name="侯女士",
        company_name="润和软件",
        last_message="您正在与Boss侯女士沟通",
        unread_count=2,
    )
    runner = BossAutomationRunner(
        SimpleNamespace(click=lambda *_args, **_kwargs: None),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    ready: list[object] = []
    runner._wait = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda predicate, *_args, **_kwargs: ready.append(predicate) or True
    )
    runner._click_when_ready = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda *_args, **_kwargs: object()
    )

    with patch(
        "boss_assistant.automation.runner.find_conversation_open_target",
        return_value=object(),
    ):
        assert runner._open_conversation(conversation) is True  # noqa: SLF001

    predicate = ready[0]
    one_unread = (
        ChatMessage("您好，我有Python开发经验。", True, 0),
        ChatMessage("你好啊，可以聊一聊~", False, 1),
    )
    two_unread = (
        *one_unread,
        ChatMessage("我想要一份您的附件简历，您是否同意", False, 2),
    )

    with patch(
        "boss_assistant.automation.runner.read_current_chat_identity",
        return_value=("其他HR", "其他公司"),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=two_unread,
    ):
        assert predicate(None) is False  # type: ignore[operator]

    with patch(
        "boss_assistant.automation.runner.read_current_chat_identity",
        return_value=("侯女士", "润和软件"),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=one_unread,
    ), patch(
        "boss_assistant.automation.runner.extract_chat_system_notes",
        return_value=(),
    ), patch(
        "boss_assistant.automation.runner.resume_request_accept_button",
        return_value=None,
    ):
        assert predicate(None) is False  # type: ignore[operator]

    with patch(
        "boss_assistant.automation.runner.read_current_chat_identity",
        return_value=("侯女士", "润和软件"),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=two_unread,
    ):
        assert predicate(None) is True  # type: ignore[operator]


def test_current_chat_identity_reads_live_header_shape() -> None:
    class _IdentityBrowser:
        @staticmethod
        def js(_script: str, *_args: object) -> object:
            return {"recruiter": "侯女士", "company": "润和软件"}

    assert read_current_chat_identity(  # type: ignore[arg-type]
        _IdentityBrowser()
    ) == ("侯女士", "润和软件")


def test_detail_identity_is_locked_to_reviewed_job_card() -> None:
    snapshot = read_job_detail(_DetailBrowser())  # type: ignore[arg-type]
    card = _card(
        job_name="AI数据工程实习生-五险一金",
        company_name="安点科技",
    )

    aligned = align_detail_identity(snapshot, card)

    assert aligned.job_name == "AI数据工程实习生-五险一金"
    assert aligned.company_name == "安点科技"


def _conversation(**overrides: object) -> ChatConversation:
    values: dict[str, object] = {
        "recruiter_name": "黄先生",
        "company_name": "财达配送",
        "position_name": "黄站长",
        "last_message": "方便加一下微信吗",
        "unread_count": 1,
        "last_message_from_me": False,
        "fingerprint": "huang-caidapeisong",
    }
    values.update(overrides)
    return ChatConversation(**values)  # type: ignore[arg-type]


def test_unsolicited_contact_request_with_mismatched_job_is_ignored() -> None:
    conversation = _conversation()

    class _ReviewProvider:
        @staticmethod
        def review_chat_message(*_args, **_kwargs):
            return ChatReviewResult(
                resume_requested=False,
                contact_requested=True,
                reply="好的，这是我的简历，您看看",
                reason="HR明确索要微信",
            )

        @staticmethod
        def review_card(*_args, **_kwargs):
            return CardReviewResult(
                eligible=False,
                job_direction_match=False,
                location_match=True,
                reason="配送站长不属于目标方向",
            )

    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        policy=_policy(),
        review_provider=_ReviewProvider(),  # type: ignore[arg-type]
    )
    runner._return_to_message_list = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    runner._find_conversation = lambda _fingerprint: conversation  # type: ignore[method-assign]  # noqa: SLF001
    runner._open_conversation = lambda _conversation: True  # type: ignore[method-assign]  # noqa: SLF001
    runner._wait = lambda predicate, *_args, **_kwargs: predicate(None)  # type: ignore[method-assign]  # noqa: SLF001

    with patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=(ChatMessage("方便加一下微信吗", False, 0),),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_system_notes",
        return_value=(),
    ), patch(
        "boss_assistant.automation.runner.resume_request_accept_button",
        return_value=None,
    ), patch(
        "boss_assistant.automation.runner.read_current_chat_job_info",
        return_value=ChatJobInfo("配送站长", "8-12K", "广州"),
    ), patch.object(runner, "_pin_conversation") as pin:
        stats = AutomationStats()
        runner._handle_unread_conversation(conversation, stats)  # noqa: SLF001

    pin.assert_not_called()
    assert stats.chat_actions[-1]["action"] == "无需处理"
    assert "与当前条件不匹配" in str(stats.chat_actions[-1]["reason"])


@pytest.mark.parametrize(
    "last_message",
    (
        "很抱歉，您的简历和我们当前的职位需求不是很匹配。",
        "经过认真考虑，我们认为您的经验与我们的职位需求并不完全一致。"
        "祝您在BOSS直聘找到您理想的工作！",
        "您的经验并不完全符合我们当前项目的需求。",
        "[祈祷] 不好意思，不太合适哦",
        "感谢您的关注，暂时不能和您合作，希望您找到合适的工作机会！",
        "非常抱歉，您不太适合我这个职位，祝您求职顺利。",
        "您的经验与我们当前项目的需求并不完全吻合。",
    ),
)
def test_hr_rejection_is_ignored_without_pin_or_model_review(
    last_message: str,
) -> None:
    conversation = _conversation(
        last_message=last_message
    )
    provider = SimpleNamespace(
        review_chat_message=lambda *_args, **_kwargs: pytest.fail(
            "明确拒绝不应再调用模型"
        )
    )
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        review_provider=provider,  # type: ignore[arg-type]
    )
    runner._return_to_message_list = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    runner._find_conversation = lambda _fingerprint: conversation  # type: ignore[method-assign]  # noqa: SLF001
    runner._open_conversation = lambda _conversation: pytest.fail(  # type: ignore[method-assign]  # noqa: SLF001
        "列表预览已明确拒绝时不应打开会话"
    )
    runner._wait = lambda predicate, *_args, **_kwargs: predicate(None)  # type: ignore[method-assign]  # noqa: SLF001

    with patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=(
            ChatMessage("很抱歉，您的简历和职位需求不是很匹配。", False, 0),
        ),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_system_notes",
        return_value=(),
    ), patch(
        "boss_assistant.automation.runner.resume_request_accept_button",
        return_value=None,
    ), patch.object(runner, "_pin_conversation") as pin:
        stats = AutomationStats()
        runner._handle_unread_conversation(conversation, stats)  # noqa: SLF001

    pin.assert_not_called()
    assert stats.chat_actions[-1]["action"] == "无需处理"
    assert "不回复且不置顶" in str(stats.chat_actions[-1]["reason"])


def test_pin_conversation_hovers_row_before_waiting_for_operation_button() -> None:
    hovered: list[object] = []
    pauses: list[str] = []
    clicks: list[str] = []
    row = object()
    operation = object()
    menu_item = object()
    menu_clicks: list[tuple[object, str]] = []
    browser = SimpleNamespace(
        hover=lambda element, **_kwargs: hovered.append(element),
        find_clickable_by_text=lambda *_args, **_kwargs: menu_item,
        click=lambda element, **kwargs: menu_clicks.append(
            (element, str(kwargs.get("description") or ""))
        ),
    )
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    runner._wait_for_displayed = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda finder, *_args, **_kwargs: finder()
    )
    runner._pause_before = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda description: pauses.append(description) or 1.0
    )
    runner._wait = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda predicate, *_args, **_kwargs: predicate(None)
    )

    def click_when_ready(finder, description, **_kwargs):
        clicks.append(description)
        return finder()

    runner._click_when_ready = click_when_ready  # type: ignore[method-assign]  # noqa: SLF001

    with patch(
        "boss_assistant.automation.runner.find_conversation_open_target",
        return_value=row,
    ), patch(
        "boss_assistant.automation.runner.find_conversation_operation_element",
        return_value=operation,
    ):
        runner._pin_conversation(_conversation())  # noqa: SLF001

    assert hovered == [row]
    assert any("显示会话" in description for description in pauses)
    assert clicks == ["打开会话“黄先生”的操作菜单"]
    assert menu_clicks == [(menu_item, "置顶会话“黄先生”")]


@pytest.mark.parametrize(
    "last_message",
    (
        "对方已同意，您的附件简历已发送给对方",
        "对方已查看了您的附件简历",
        "对方查看了你的简历",
    ),
)
def test_resume_sent_or_viewed_notification_returns_without_open_or_pin(
    last_message: str,
) -> None:
    conversation = _conversation(last_message=last_message)
    provider = SimpleNamespace(
        review_chat_message=lambda *_args, **_kwargs: pytest.fail(
            "简历已发送或已查看通知不应调用模型"
        )
    )
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        review_provider=provider,  # type: ignore[arg-type]
    )
    runner._return_to_message_list = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    runner._find_conversation = lambda _fingerprint: conversation  # type: ignore[method-assign]  # noqa: SLF001
    runner._wait = lambda predicate, *_args, **_kwargs: predicate(None)  # type: ignore[method-assign]  # noqa: SLF001
    runner._open_conversation = lambda _conversation: pytest.fail(  # type: ignore[method-assign]  # noqa: SLF001
        "简历终态通知不应打开会话"
    )

    with patch.object(runner, "_pin_conversation") as pin:
        stats = AutomationStats()
        runner._handle_unread_conversation(conversation, stats)  # noqa: SLF001

    pin.assert_not_called()
    assert stats.chat_actions[-1]["action"] == "无需处理"
    assert "不置顶" in str(stats.chat_actions[-1]["reason"])


def test_find_chat_entry_skips_hidden_stale_duplicate() -> None:
    hidden = _Element("立即沟通", displayed=False)
    visible = _Element("继续沟通", displayed=True)

    class _Browser:
        @staticmethod
        def find_all_css(selector: str):
            return [hidden, visible] if selector == ".op-btn-chat" else []

        @staticmethod
        def text_of(element: _Element) -> str:
            return element.name

        @staticmethod
        def find_clickable_by_text(*_args, **_kwargs):
            return None

    assert find_chat_entry(_Browser()) is visible  # type: ignore[arg-type]


def test_chat_editor_and_send_button_skip_hidden_or_disabled_stale_nodes() -> None:
    class _Control(_Element):
        def __init__(self, name: str, *, displayed: bool = True, enabled: bool = True):
            super().__init__(name, displayed=displayed)
            self.enabled = enabled

        def is_enabled(self) -> bool:
            return self.enabled

    hidden_editor = _Control("hidden-editor", displayed=False)
    visible_editor = _Control("visible-editor")
    disabled_send = _Control("disabled-send", enabled=False)
    enabled_send = _Control("enabled-send")

    class _Browser:
        @staticmethod
        def find_all_css(selector: str):
            if selector == "#chat-input":
                return [hidden_editor, visible_editor]
            if selector == ".btn-send":
                return [disabled_send, enabled_send]
            return []

        @staticmethod
        def find_clickable_by_text(*_args, **_kwargs):
            return None

    browser = _Browser()
    assert find_greeting_editor(browser) is visible_editor  # type: ignore[arg-type]
    assert find_send_button(browser) is enabled_send  # type: ignore[arg-type]


def test_current_chat_job_info_reads_name_salary_and_location() -> None:
    elements = {
        ".chat-conversation [class*='position-name']": _Element("Python开发工程师"),
        ".chat-conversation .left-content .salary": _Element("8-12K"),
        ".chat-conversation .left-content .city": _Element("广州"),
    }

    class _Browser:
        @staticmethod
        def find_all_css(selector: str):
            element = elements.get(selector)
            return [element] if element else []

        @staticmethod
        def text_of(element: _Element) -> str:
            return element.name

    info = read_current_chat_job_info(_Browser())  # type: ignore[arg-type]

    assert info == ChatJobInfo("Python开发工程师", "8-12K", "广州")


def test_phone_pinned_friend_top_is_collected_as_pinned() -> None:
    class _Browser:
        @staticmethod
        def js(script: str, *_args):
            assert ".friend-top" in script
            return [
                {
                    "name": "陈女士",
                    "company": "示例科技",
                    "position": "HR",
                    "last": "待处理",
                    "unread": 1,
                    "pinned": True,
                }
            ]

    conversations = extract_chat_conversations(_Browser())  # type: ignore[arg-type]

    assert conversations[0].pinned is True


def test_pin_conversation_accepts_cancel_pin_as_already_pinned() -> None:
    row = object()
    operation = object()
    cancel_pin = object()
    menu_clicks: list[object] = []

    def find_menu(texts, **_kwargs):
        return cancel_pin if "取消置顶" in texts else None

    browser = SimpleNamespace(
        hover=lambda *_args, **_kwargs: None,
        find_clickable_by_text=find_menu,
        click=lambda element, **_kwargs: menu_clicks.append(element),
    )
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
    )
    runner._wait_for_displayed = lambda finder, *_args, **_kwargs: finder()  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._pause_before = lambda *_args, **_kwargs: 1.0  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._wait = lambda predicate, *_args, **_kwargs: predicate(None)  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._click_when_ready = lambda finder, *_args, **_kwargs: finder()  # type: ignore[method-assign]  # noqa: SLF001,E501

    with patch(
        "boss_assistant.automation.runner.find_conversation_open_target",
        return_value=row,
    ), patch(
        "boss_assistant.automation.runner.find_conversation_operation_element",
        return_value=operation,
    ):
        newly_pinned = runner._pin_conversation(_conversation())  # noqa: SLF001

    assert newly_pinned is False
    assert menu_clicks == []


def test_semantic_rejection_is_ignored_without_pin() -> None:
    conversation = _conversation(
        last_message="感谢投递，本次招聘流程到此结束，祝您求职顺利。"
    )
    provider = SimpleNamespace(
        review_chat_message=lambda *_args, **_kwargs: ChatReviewResult(
            resume_requested=False,
            contact_requested=False,
            no_action_needed=True,
            reply="",
            reason="HR明确结束本次招聘流程",
        )
    )
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        review_provider=provider,  # type: ignore[arg-type]
    )
    runner._return_to_message_list = lambda: None  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._find_conversation = lambda _fingerprint: conversation  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._open_conversation = lambda _conversation: True  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._wait = lambda predicate, *_args, **_kwargs: predicate(None)  # type: ignore[method-assign]  # noqa: SLF001,E501

    with patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=(ChatMessage(conversation.last_message, False, 0),),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_system_notes",
        return_value=(),
    ), patch(
        "boss_assistant.automation.runner.resume_request_accept_button",
        return_value=None,
    ), patch.object(runner, "_pin_conversation") as pin:
        stats = AutomationStats()
        runner._handle_unread_conversation(conversation, stats)  # noqa: SLF001

    pin.assert_not_called()
    assert stats.chat_actions[-1]["action"] == "无需处理"
    assert "结束" in str(stats.chat_actions[-1]["reason"])


def test_excluded_company_unread_request_is_ignored_without_model_or_pin() -> None:
    conversation = _conversation(company_name="财达配送", last_message="发份简历看看")
    provider = SimpleNamespace(
        review_chat_message=lambda *_args, **_kwargs: pytest.fail(
            "命中不打招呼公司后不应调用消息模型"
        )
    )
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        policy=AutomationPolicy(
            excluded_companies=("财达配送",),
            allowed_job_keywords=("Python",),
            allowed_locations=("广州",),
            target_companies=1,
        ),
        review_provider=provider,  # type: ignore[arg-type]
    )
    runner._return_to_message_list = lambda: None  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._find_conversation = lambda _fingerprint: conversation  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._open_conversation = lambda _conversation: True  # type: ignore[method-assign]  # noqa: SLF001,E501
    runner._wait = lambda predicate, *_args, **_kwargs: predicate(None)  # type: ignore[method-assign]  # noqa: SLF001,E501

    with patch(
        "boss_assistant.automation.runner.extract_chat_messages",
        return_value=(ChatMessage("您好，我想了解岗位", True, 0), ChatMessage("发份简历看看", False, 1)),
    ), patch(
        "boss_assistant.automation.runner.extract_chat_system_notes",
        return_value=(),
    ), patch(
        "boss_assistant.automation.runner.resume_request_accept_button",
        return_value=None,
    ), patch.object(runner, "_pin_conversation") as pin:
        stats = AutomationStats()
        runner._handle_unread_conversation(conversation, stats)  # noqa: SLF001

    pin.assert_not_called()
    assert stats.chat_actions[-1]["action"] == "无需处理"
    assert "不打招呼名单" in str(stats.chat_actions[-1]["reason"])


def test_unsolicited_chat_salary_mismatch_stops_before_model_review() -> None:
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        policy=AutomationPolicy(
            excluded_companies=(),
            allowed_job_keywords=("Python",),
            allowed_locations=("广州",),
            target_companies=1,
            salary_min_k=8,
            salary_max_k=15,
        ),
        review_provider=SimpleNamespace(
            review_card=lambda *_args, **_kwargs: pytest.fail(
                "薪资本地硬门槛不符时不应调用模型"
            )
        ),  # type: ignore[arg-type]
    )

    matched, reason = runner._review_unsolicited_chat_job(  # noqa: SLF001
        _conversation(),
        ChatJobInfo("Python开发工程师", "7-12K", "广州"),
    )

    assert matched is False
    assert "薪资" in reason


def test_result_view_only_follows_latest_when_at_bottom() -> None:
    assert result_view_is_at_bottom((0.0, 1.0)) is True
    assert result_view_is_at_bottom((0.25, 0.75)) is False
    assert result_view_is_at_bottom((0.9999995, 0.9999995)) is True


def test_gui_new_result_does_not_interrupt_manual_history_scroll() -> None:
    class _Tree:
        def __init__(self) -> None:
            self.view = (0.2, 0.7)
            self.rows: list[str] = []
            self.seen: list[str] = []

        def yview(self):
            return self.view

        def insert(self, *_args, **_kwargs):
            row = f"row-{len(self.rows) + 1}"
            self.rows.append(row)
            return row

    tree = _Tree()
    panel = SimpleNamespace(
        tree=tree,
        _row_records={},
        _follow_latest_results=False,
        _row_counts=lambda: (0, 0),
        _refresh_progress_from_table=lambda: None,
    )
    tree.see = lambda row: tree.seen.append(row)  # type: ignore[attr-defined]
    result = {
        "created_at": "2026-08-04T12:00:00+08:00",
        "company_name": "示例科技",
        "job_name": "Python开发",
        "delivery_status": "未投递",
    }

    BossControlPanel._add_result(panel, result)  # type: ignore[arg-type]
    assert tree.seen == []

    tree.view = (0.5, 1.0)
    BossControlPanel._sync_result_follow_state(panel)  # type: ignore[arg-type]
    BossControlPanel._add_result(panel, result)  # type: ignore[arg-type]
    assert tree.seen == ["row-2"]


def test_gui_follow_mode_survives_stale_yview_during_rapid_results() -> None:
    class _Tree:
        def __init__(self) -> None:
            self.rows: list[str] = []
            self.seen: list[str] = []

        def yview(self):
            # 模拟 Tk 连续 insert/see 后尚未完成滚动范围重算时的短暂旧值。
            return (0.4, 0.9)

        def insert(self, *_args, **_kwargs):
            row = f"row-{len(self.rows) + 1}"
            self.rows.append(row)
            return row

        def see(self, row: str) -> None:
            self.seen.append(row)

    tree = _Tree()
    panel = SimpleNamespace(
        tree=tree,
        _row_records={},
        _follow_latest_results=True,
        _row_counts=lambda: (len(tree.rows), 0),
        _refresh_progress_from_table=lambda: None,
    )
    result = {
        "created_at": "2026-08-11T12:00:00+08:00",
        "company_name": "示例科技",
        "job_name": "Python开发",
        "delivery_status": "未投递",
    }

    for _ in range(6):
        BossControlPanel._add_result(panel, result)  # type: ignore[arg-type]

    assert tree.seen == [f"row-{index}" for index in range(1, 7)]
    assert panel._follow_latest_results is True


def test_gui_scrollbar_restores_follow_mode_at_bottom() -> None:
    class _Tree:
        def __init__(self) -> None:
            self.view = (0.2, 0.7)
            self.commands: list[tuple[str, ...]] = []

        def yview(self, *args: str):
            if args:
                self.commands.append(args)
                if args == ("moveto", "1.0"):
                    self.view = (0.5, 1.0)
                return None
            return self.view

    tree = _Tree()
    panel = SimpleNamespace(
        tree=tree,
        _follow_latest_results=False,
        _hide_cell_viewer=lambda: None,
    )
    panel._schedule_result_follow_sync = lambda: (  # type: ignore[attr-defined]
        BossControlPanel._sync_result_follow_state(panel)  # type: ignore[arg-type]
    )

    BossControlPanel._on_result_scroll(  # type: ignore[arg-type]
        panel,
        "moveto",
        "1.0",
    )

    assert tree.commands == [("moveto", "1.0")]
    assert panel._follow_latest_results is True


def test_recent_success_requires_exact_company_and_job(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "created_at": now.isoformat(),
        "decisions": [
            {
                "company_name": "示例科技",
                "job_name": "Python开发",
                "delivery_status": "发送成功",
                "created_at": (now - timedelta(days=2)).isoformat(),
            },
            {
                "company_name": "另一公司",
                "job_name": "AI开发",
                "delivery_status": "已填充未发送",
                "created_at": now.isoformat(),
            },
        ],
    }
    (tmp_path / "boss_run_test.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    recent = load_recent_successful_applications(tmp_path, now=now)
    assert find_recent_successful_application(
        "示例科技", "Python开发", recent
    ) == now - timedelta(days=2)
    assert find_recent_successful_application("示例科技", "AI开发", recent) is None


class _LoginBrowser:
    current_url = ""

    def __init__(self) -> None:
        self.logged_in = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def open(self, url: str) -> None:
        self.current_url = url

    def is_logged_in(self) -> bool:
        return self.logged_in

    def switch_to_boss_page(self) -> bool:
        return True

    def consolidate_windows(self) -> None:
        return None


def test_login_callback_is_emitted_and_runner_continues() -> None:
    browser = _LoginBrowser()
    messages: list[str] = []

    def on_login(message: str) -> None:
        messages.append(message)
        browser.logged_in = True

    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        login_required_callback=on_login,
        login_wait_seconds=1,
    )
    runner._sleep = lambda _seconds: None  # type: ignore[method-assign]
    runner.ensure_ready()
    assert browser.started
    assert len(messages) == 1
    assert "Edge" in messages[0]


def test_current_browser_mode_requires_existing_logged_in_page() -> None:
    browser = _LoginBrowser()
    messages: list[str] = []

    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        login_required_callback=messages.append,
        require_logged_in_before_start=True,
    )
    with pytest.raises(LoginRequiredError):
        runner.ensure_ready()
    assert browser.started
    assert len(messages) == 1
    assert "重新启动脚本" in messages[0]


def test_message_check_without_red_number_does_not_open_messages() -> None:
    class _MessageBrowser:
        current_url = "https://www.zhipin.com/web/geek/jobs"

        @staticmethod
        def js(_script: str, *_args: object) -> int:
            return 0

        def open(self, url: str) -> None:
            self.current_url = url

    runner = BossAutomationRunner(
        _MessageBrowser(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
        review_provider=SimpleNamespace(review_chat_message=lambda *_args: None),
    )

    with patch.object(runner, "_sleep"), patch.object(
        runner, "_pause_before", return_value=1.0
    ) as pause_before, patch(
        "boss_assistant.automation.runner.extract_chat_conversations",
        return_value=(),
    ), patch.object(runner, "_return_to_recommendations") as restore:
        page_changed = runner.process_unread_messages(AutomationStats(), force=False)

    assert page_changed is False
    pause_before.assert_not_called()
    restore.assert_not_called()


def test_message_red_number_is_read_before_opening_message_page() -> None:
    class _MessageBrowser:
        current_url = "https://www.zhipin.com/web/geek/jobs"

        @staticmethod
        def js(_script: str, *_args: object) -> int:
            return 4

        def open(self, url: str) -> None:
            self.current_url = url

    browser = _MessageBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
        review_provider=SimpleNamespace(review_chat_message=lambda *_args: None),
    )

    with patch.object(runner, "_sleep"), patch.object(
        runner, "_pause_before", return_value=1.0
    ) as pause_before, patch.object(
        runner, "_wait"
    ) as wait_for_unread, patch(
        "boss_assistant.automation.runner.extract_chat_conversations",
        return_value=(),
    ), patch.object(runner, "_return_to_recommendations") as restore:
        page_changed = runner.process_unread_messages(AutomationStats())

    assert read_message_unread_count(browser) == 4  # type: ignore[arg-type]
    assert page_changed is True
    assert browser.current_url == "https://www.zhipin.com/web/geek/chat"
    pause_before.assert_called_once_with("打开消息会话列表")
    assert any(
        call.args[1] == "含未读角标的消息会话列表"
        for call in wait_for_unread.call_args_list
    )
    restore.assert_called_once_with()


def test_message_check_is_due_after_configured_inspected_jobs() -> None:
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
        config=AutomationConfig(force_message_check_every_n_jobs=2),
        review_provider=SimpleNamespace(review_chat_message=lambda *_args: None),
    )
    stats = AutomationStats(inspected=1)

    with patch.object(
        runner, "process_unread_messages", return_value=False
    ) as process:
        assert runner._inspect_messages_if_due(stats) is False  # noqa: SLF001
        process.assert_not_called()
        stats.inspected = 2
        assert runner._inspect_messages_if_due(stats) is False  # noqa: SLF001

    process.assert_called_once_with(stats, force=True)
    assert runner._jobs_since_message_check == 0  # noqa: SLF001


def test_jobs_page_falls_back_to_direct_navigation_when_chat_has_no_tab() -> None:
    class _ChatBrowser:
        current_url = "https://www.zhipin.com/web/geek/chat"

        @staticmethod
        def is_logged_in() -> bool:
            return True

        def open(self, url: str) -> None:
            self.current_url = url

        @staticmethod
        def page_text() -> str:
            return ""

    browser = _ChatBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
        require_logged_in_before_start=True,
    )

    with patch(
        "boss_assistant.automation.runner.click_positions_tab",
        return_value=False,
    ), patch.object(runner, "_pause_before", return_value=1.0), patch.object(
        runner, "_sleep"
    ), patch.object(runner, "_wait"):
        runner._go_to_jobs_page()

    assert browser.current_url == "https://www.zhipin.com/web/geek/jobs"


def test_jobs_page_falls_back_when_positions_click_does_not_navigate() -> None:
    class _ChatBrowser:
        current_url = "https://www.zhipin.com/web/geek/chat"

        @staticmethod
        def is_logged_in() -> bool:
            return True

        def open(self, url: str) -> None:
            self.current_url = url

        @staticmethod
        def page_text() -> str:
            return ""

    browser = _ChatBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
        require_logged_in_before_start=True,
    )

    def wait_for_navigation(predicate, _description, timeout=None):
        if timeout is not None:
            raise ElementNotFoundError("点击未切换")
        assert predicate(browser) is True
        return True

    with patch(
        "boss_assistant.automation.runner.click_positions_tab",
        return_value=True,
    ), patch.object(runner, "_pause_before", return_value=1.0), patch.object(
        runner, "_sleep"
    ), patch.object(
        runner, "_wait", side_effect=wait_for_navigation
    ):
        runner._go_to_jobs_page()

    assert browser.current_url == "https://www.zhipin.com/web/geek/jobs"


def test_open_card_never_falls_back_to_full_detail_url_after_list_refresh() -> None:
    class _DetailFallbackBrowser:
        driver = None
        current_url = "https://www.zhipin.com/web/geek/jobs"

        def open(self, url: str) -> None:
            self.current_url = url

    browser = _DetailFallbackBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
    )
    card = _card(detail_url="/job_detail/abc.html")

    with patch(
        "boss_assistant.automation.runner.select_job_card_inline",
        return_value=False,
    ), patch.object(runner, "_pause_before", return_value=1.0), patch.object(
        runner,
        "_wait",
        side_effect=ElementNotFoundError("等待超时"),
    ):
        with pytest.raises(BossAutomationError, match="仍无法按岗位ID、详情链接或指纹"):
            runner._open_card(card)

    assert browser.current_url == "https://www.zhipin.com/web/geek/jobs"


def test_job_card_identity_survives_company_text_change_via_detail_url() -> None:
    before = _card(
        company_name="钛马",
        fingerprint="before",
        job_id=None,
        detail_url="/job_detail/abc123.html?ka=search_list_1",
    )
    after = _card(
        company_name="上海钛马信息网络技术有限公司",
        fingerprint="after",
        job_id=None,
        detail_url="https://www.zhipin.com/job_detail/abc123.html?ka=search_list_2",
    )

    assert _same_job_card(before, after) is True


def test_open_card_waits_for_transient_list_redraw_then_selects() -> None:
    class _DetailBrowser:
        driver = None
        current_url = "https://www.zhipin.com/web/geek/jobs"

    browser = _DetailBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
    )
    card = _card()
    selection_results = iter((False, False, True))

    def wait_with_redraw(predicate, description, timeout=None):
        if "推荐列表中的岗位" in description:
            assert predicate(browser) is False
            assert predicate(browser) is False
            assert predicate(browser) is True
            return True
        return True

    with patch(
        "boss_assistant.automation.runner.select_job_card_inline",
        side_effect=lambda *_args: next(selection_results),
    ), patch.object(
        runner, "_pause_before", return_value=1.0
    ), patch.object(
        runner, "_sleep"
    ), patch.object(
        runner, "_wait", side_effect=wait_with_redraw
    ):
        runner._open_card(card)


def test_open_card_waits_for_matching_description_not_early_chat_button() -> None:
    class _DetailBrowser:
        driver = None
        current_url = "https://www.zhipin.com/web/geek/jobs"

        def open(self, url: str) -> None:
            self.current_url = url

    def snapshot(name: str, description: str):
        return SimpleNamespace(
            job_data=SimpleNamespace(
                is_boss_job_detail_page=bool(name and description),
                job_name=SimpleNamespace(value=name),
                job_description=SimpleNamespace(value=description),
            )
        )

    browser = _DetailBrowser()
    runner = BossAutomationRunner(
        browser,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
    )
    card = _card(detail_url="/job_detail/abc.html")
    details = iter(
        (
            snapshot("上一岗位", ""),
            snapshot("上一岗位", "上一岗位正文"),
            snapshot(card.job_name, "当前岗位完整正文"),
        )
    )

    def wait_until_ready(predicate, description, timeout=None):
        if "推荐列表中的岗位" in description:
            assert predicate(browser) is True
            return True
        assert predicate(browser) is False
        assert predicate(browser) is False
        assert predicate(browser) is True
        return True

    with patch(
        "boss_assistant.automation.runner.select_job_card_inline",
        return_value=True,
    ), patch(
        "boss_assistant.automation.runner.read_job_detail",
        side_effect=lambda _browser: next(details),
    ), patch(
        "boss_assistant.automation.runner.align_detail_identity",
        side_effect=lambda snapshot, _card: snapshot,
    ) as align_identity, patch.object(
        runner, "_pause_before", return_value=1.0
    ), patch.object(
        runner, "_sleep"
    ), patch.object(
        runner, "_wait", side_effect=wait_until_ready
    ):
        runner._open_card(card)

    align_identity.assert_not_called()


def test_run_reloads_cards_before_processing_next_after_return(
    tmp_path: Path,
) -> None:
    intent = JobExpectation("广州", "Python", None, ())
    intents = JobIntentData((intent,))
    first = _card(
        job_name="Python开发甲",
        company_name="甲公司",
        fingerprint="initial-first",
        job_id="initial-first",
    )
    stale_second = _card(
        job_name="Python开发旧卡",
        company_name="旧卡公司",
        fingerprint="stale-second",
        job_id="stale-second",
    )
    refreshed_second = _card(
        job_name="Python开发乙",
        company_name="乙公司",
        fingerprint="refreshed-second",
        job_id="refreshed-second",
    )

    class _ReviewProvider:
        @staticmethod
        def review_card(_card_value, *_args):
            return CardReviewResult(
                eligible=True,
                job_direction_match=True,
                location_match=True,
                reason="匹配",
                combined_directions=("Python",),
                matched_direction_keywords=("Python",),
            )

        @staticmethod
        def review_detail(*_args):
            return DetailReviewResult(
                should_apply=True,
                score=90,
                reasons=("匹配",),
                matched_skills=("Python",),
                greeting="您好，我有相关项目经验，希望与您进一步沟通该岗位",
                qualifications_summary="匹配",
            )

    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(
            save_snapshot=lambda *_args: SimpleNamespace(
                action=SimpleNamespace(value="inserted")
            )
        ),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        run_directory=tmp_path,
        config=AutomationConfig(
            max_jobs=5,
            dry_run=True,
            inspect_unread_messages=False,
        ),
        policy=AutomationPolicy(
            excluded_companies=(),
            allowed_job_keywords=("Python",),
            allowed_locations=("广州",),
            target_companies=2,
        ),
        review_provider=_ReviewProvider(),  # type: ignore[arg-type]
    )
    runner.ensure_ready = lambda: None  # type: ignore[method-assign]
    runner.read_job_intents = lambda: intents  # type: ignore[method-assign]
    runner.open_recommendations = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runner._current_page = lambda: "recommendations"  # type: ignore[method-assign]  # noqa: SLF001
    runner._save_checkpoint = lambda *_args: tmp_path / "checkpoint.json"  # type: ignore[method-assign]  # noqa: SLF001
    runner._save_run_log = lambda *_args: tmp_path / "run.json"  # type: ignore[method-assign]  # noqa: SLF001

    page = {"refreshed": False}
    opened: list[JobCard] = []
    current: list[JobCard] = []

    def open_card(card: JobCard) -> None:
        opened.append(card)
        current[:] = [card]

    def return_to_recommendations(**_kwargs) -> None:
        page["refreshed"] = True

    def extract_cards(_browser):
        if page["refreshed"]:
            return (refreshed_second,)
        return (first, stale_second)

    def read_current_detail(_browser):
        card = current[0]
        job = build_job_page_data(
            current_url="https://www.zhipin.com/web/geek/jobs",
            job_name=card.job_name,
            company_name=card.company_name,
            salary=card.salary,
            location=card.location,
            job_description="岗位职责完整文本",
            experience=card.experience,
            is_detail_page=True,
            node_count=10,
            accessible_text_count=5,
        )
        return SimpleNamespace(
            job_data=job,
            job_name=card.job_name,
            company_name=card.company_name,
            salary=card.salary,
            location=card.location,
        )

    runner._open_card = open_card  # type: ignore[method-assign]  # noqa: SLF001
    runner._return_to_recommendations = return_to_recommendations  # type: ignore[method-assign]  # noqa: SLF001

    with patch(
        "boss_assistant.automation.runner.extract_job_cards",
        side_effect=extract_cards,
    ), patch(
        "boss_assistant.automation.runner.read_job_detail",
        side_effect=read_current_detail,
    ):
        stats, _log_path = runner.run()

    assert opened == [first, refreshed_second]
    assert stale_second not in opened
    assert stats.matched == 2


def test_gui_module_imports_without_runner_cycle() -> None:
    import boss_assistant.gui.app as app

    assert app.BossControlPanel is not None
    assert app.DEFAULT_RUN_MODE == "实际发送"
    assert app.DEFAULT_REVIEW_MODE == "大模型API"
    assert f"v{__version__}" in app.APP_TITLE
    assert "v" not in app.HEADER_TITLE


def test_gui_formats_web_expectation_option_with_city_and_role() -> None:
    from boss_assistant.gui.app import format_expectation_option

    assert (
        format_expectation_option(JobExpectation("广州", "Python", None, ()))
        == "广州 / Python"
    )
    assert (
        format_expectation_option(JobExpectation(None, "产品经理", None, ()))
        == "不限城市 / 产品经理"
    )


def test_gui_fetches_job_intents_without_starting_automation() -> None:
    from boss_assistant.gui.app import fetch_job_intents_for_gui

    intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Python", None, ()),
        )
    )

    class _Browser:
        current_url = "https://www.zhipin.com/web/geek/chat"

        def __init__(self) -> None:
            self.started = False
            self.closed = False
            self.opened: list[str] = []

        def start(self) -> None:
            self.started = True

        @staticmethod
        def is_logged_in() -> bool:
            return True

        def open(self, url: str) -> None:
            self.opened.append(url)
            self.current_url = url

        def wait_for(self, predicate, **_kwargs):
            return predicate(self)

        def quit(self) -> None:
            self.closed = True

    browser = _Browser()

    with patch("boss_assistant.gui.app.parse_job_intents", return_value=intents):
        result = fetch_job_intents_for_gui(
            browser_factory=lambda **_kwargs: browser,
            timeout=0.1,
        )

    assert result == intents
    assert browser.started
    assert browser.opened == ["https://www.zhipin.com/web/geek/jobs"]
    assert browser.closed


def test_gui_txt_defaults_only_accept_manual_input_fields() -> None:
    from boss_assistant.gui.app import parse_gui_defaults

    defaults = parse_gui_defaults(
        "\n".join(
            (
                "\ufeff不打招呼公司：外包公司，亚信。",
                "目标岗位方向:Python，C++，后端。",
                "排除岗位方向：Java。",
                "目标城市：广州，深圳。",
                "薪资下限：5。",
                "薪资上限：13。",
                "公司规模：20。",
                "目标公司数：80。",
                "最低匹配分：65。",
                "MySQL主机：localhost。",
                "MySQL端口：3307。",
                "MySQL用户名：boss_user。",
                "MySQL密码:secret:with：colons。",
                "MySQL数据库：boss_jobs。",
                "运行模式：实际发送。",
                "审核方式：大模型API。",
                "未知字段：不会进入结果。",
            )
        )
    )

    assert defaults["不打招呼公司"] == "外包公司，亚信"
    assert defaults["目标岗位方向"] == "Python，C++，后端"
    assert defaults["薪资下限"] == "5"
    assert defaults["薪资上限"] == "13"
    assert defaults["公司规模"] == "20"
    assert defaults["MySQL密码"] == "secret:with：colons"
    assert defaults["MySQL数据库"] == "boss_jobs"
    assert "运行模式" not in defaults
    assert "审核方式" not in defaults
    assert "未知字段" not in defaults


def test_gui_txt_explicit_empty_values_become_blank_or_none() -> None:
    from boss_assistant.gui.app import parse_gui_defaults

    defaults = parse_gui_defaults(
        "\n".join(
            (
                "不打招呼公司：。",
                "目标岗位方向：。",
                "排除岗位方向：。",
                "目标城市：。",
                "薪资下限：。",
                "薪资上限：。",
                "公司规模：。",
                "目标公司数：。",
                "最低匹配分：。",
                "MySQL主机：。",
                "MySQL端口：。",
                "MySQL用户名：。",
                "MySQL密码：。",
                "MySQL数据库：。",
            )
        )
    )

    assert defaults == {
        "不打招呼公司": "无",
        "目标岗位方向": "",
        "排除岗位方向": "无",
        "目标城市": "",
        "薪资下限": "",
        "薪资上限": "",
        "公司规模": "",
        "目标公司数": "",
        "最低匹配分": "",
        "MySQL主机": "",
        "MySQL端口": "",
        "MySQL用户名": "",
        "MySQL密码": "",
        "MySQL数据库": "",
    }


def test_gui_txt_defaults_load_utf8_bom_file(tmp_path: Path) -> None:
    from boss_assistant.gui.app import load_gui_defaults

    path = tmp_path / "gui_defaults.txt"
    path.write_text(
        "目标岗位方向：Python。\nMySQL用户名：root。\n",
        encoding="utf-8-sig",
    )

    defaults = load_gui_defaults(path)

    assert defaults["目标岗位方向"] == "Python"
    assert defaults["MySQL用户名"] == "root"
    assert defaults["目标公司数"] == "50"


def test_collect_settings_uses_current_gui_values_without_rereading_txt() -> None:
    from boss_assistant.gui.app import BossControlPanel

    class _Value:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

    panel = SimpleNamespace(
        excluded_var=_Value("GUI排除公司"),
        jobs_var=_Value("Python，后端"),
        excluded_jobs_var=_Value("Java"),
        locations_var=_Value("深圳"),
        salary_min_var=_Value("5"),
        salary_max_var=_Value("13"),
        company_size_var=_Value("20"),
        target_var=_Value("3"),
        score_var=_Value("70"),
        mysql_host_var=_Value("gui-host"),
        mysql_port_var=_Value("3307"),
        mysql_user_var=_Value("gui-user"),
        mysql_password_var=_Value("gui-password"),
        mysql_database_var=_Value("gui_database"),
        mode_var=_Value("实际发送"),
        weekend_var=_Value("双休"),
        experience_var=_Value("3-5年"),
        expectation_var=_Value("深圳 / Python"),
        _expectation_by_option={
            "深圳 / Python": JobExpectation("深圳", "Python", None, ())
        },
        review_mode_var=_Value("大模型API"),
        _required=lambda _label, value: value.strip(),
    )

    with patch(
        "boss_assistant.gui.app.load_gui_defaults",
        side_effect=AssertionError("运行阶段不得重新读取TXT"),
    ):
        settings = BossControlPanel._collect_settings(panel)  # type: ignore[arg-type]

    policy = settings["policy"]
    mysql = settings["mysql"]
    assert policy.excluded_companies == ("GUI排除公司",)
    assert policy.allowed_job_keywords == ("Python", "后端")
    assert policy.allowed_locations == ("深圳",)
    assert policy.target_companies == 3
    assert policy.minimum_score == 70
    assert policy.salary_min_k == 5
    assert policy.salary_max_k == 13
    assert policy.minimum_company_size == 20
    assert policy.selected_expectation == JobExpectation(
        "深圳", "Python", None, ()
    )
    assert mysql.host == "gui-host"
    assert mysql.port == 3307
    assert mysql.user == "gui-user"
    assert mysql.password == "gui-password"
    assert settings["mode"] == "实际发送"
    assert settings["review_mode"] == "大模型API"


def test_invalid_delay_range_is_rejected() -> None:
    from boss_assistant.automation.models import AutomationConfig

    with pytest.raises(ValueError):
        AutomationConfig(action_delay_min_seconds=0.5)


def test_public_jobs_page_with_login_entry_is_not_logged_in() -> None:
    browser = EdgeBrowser()
    browser.driver = SimpleNamespace(
        current_url="https://www.zhipin.com/web/geek/jobs",
        get_cookies=lambda: [],
    )
    with patch.object(
        browser,
        "find_all_css",
        side_effect=lambda selector: (
            [_Element("登录/注册")]
            if selector == "a[ka='header-login']"
            else []
        ),
    ), patch.object(
        browser, "text_of", return_value="登录/注册"
    ), patch.object(
        browser, "find_clickable_by_text", return_value=_Element("登录/注册")
    ):
        assert not browser.is_logged_in()


def test_logged_in_home_ignores_hidden_login_form() -> None:
    browser = EdgeBrowser()
    browser.driver = SimpleNamespace(
        current_url="https://www.zhipin.com/",
        get_cookies=lambda: [{"name": "zp_at"}, {"name": "wt2"}],
    )
    with patch.object(
        browser,
        "find_all_css",
        side_effect=lambda selector: (
            [_Element(displayed=False)]
            if selector == "input[type='tel'][placeholder*='手机']"
            else []
        ),
    ), patch.object(
        browser, "find_clickable_by_text", return_value=None
    ):
        assert browser.is_logged_in()


def test_login_page_with_stale_cookies_is_not_logged_in() -> None:
    browser = EdgeBrowser()
    browser.driver = SimpleNamespace(
        current_url="https://www.zhipin.com/web/user/?ka=header-login",
        get_cookies=lambda: [{"name": "zp_at"}, {"name": "wt2"}],
    )
    with patch.object(browser, "find_css", return_value=_Element()), patch.object(
        browser, "find_first_css", return_value=None
    ), patch.object(browser, "page_text", return_value="登录/注册"):
        assert not browser.is_logged_in()


@pytest.mark.parametrize(
    ("target", "expected"),
    (
        (10, (4, 3, 3)),
        (2, (1, 1, 0)),
        (1, (1, 0, 0)),
    ),
)
def test_expectation_quota_is_even_and_page_order_gets_remainder(
    target: int,
    expected: tuple[int, ...],
) -> None:
    intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Golang", None, ()),
            JobExpectation("广州", "Python", None, ()),
        )
    )
    assert allocate_expectation_quotas(intents, target) == expected


def test_average_times_keeps_all_job_intents_for_distribution() -> None:
    intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Python", None, ()),
        )
    )

    assert select_policy_job_intents(intents, None) is intents


def test_single_expectation_receives_full_target_without_distribution() -> None:
    intents = JobIntentData((JobExpectation("广州", "Python", None, ()),))
    assert allocate_expectation_quotas(intents, 50) == (50,)


def test_selected_expectation_replaces_even_distribution_with_single_intent() -> None:
    intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Golang", None, ()),
            JobExpectation("广州", "Python", None, ()),
        )
    )

    selected = select_policy_job_intents(
        intents,
        JobExpectation("广州", "Python", None, ()),
    )

    assert selected.expectations == (JobExpectation("广州", "Python", None, ()),)
    assert allocate_expectation_quotas(selected, 10) == (10,)


def test_run_keeps_gui_filtered_intent_as_default_for_message_page_return(
    tmp_path: Path,
) -> None:
    full_intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Python", None, ()),
        )
    )
    selected = JobExpectation("广州", "Python", None, ())
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        run_directory=tmp_path,
        config=AutomationConfig(
            max_jobs=1,
            dry_run=True,
            stagnant_scroll_limit=1,
            inspect_unread_messages=True,
        ),
        policy=AutomationPolicy(
            excluded_companies=(),
            allowed_job_keywords=("Python",),
            allowed_locations=("广州",),
            target_companies=1,
            selected_expectation=selected,
        ),
        review_provider=SimpleNamespace(
            review_chat_message=lambda *_args, **_kwargs: None
        ),  # type: ignore[arg-type]
    )
    runner.ensure_ready = lambda: None  # type: ignore[method-assign]
    runner.read_job_intents = lambda: full_intents  # type: ignore[method-assign]
    runner.open_recommendations = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runner.process_unread_messages = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    runner._current_page = lambda: "recommendations"  # type: ignore[method-assign]  # noqa: SLF001
    runner._save_checkpoint = lambda *_args: tmp_path / "checkpoint.json"  # type: ignore[method-assign]  # noqa: SLF001
    runner._save_run_log = lambda *_args: tmp_path / "run.json"  # type: ignore[method-assign]  # noqa: SLF001

    with patch(
        "boss_assistant.automation.runner.extract_job_cards",
        return_value=(),
    ):
        runner.run()

    assert runner._job_intents.expectations == (selected,)  # noqa: SLF001


def test_missing_selected_expectation_stops_instead_of_falling_back() -> None:
    intents = JobIntentData((JobExpectation("广州", "Python", None, ()),))

    with pytest.raises(BossAutomationError, match="已不在当前 Boss 页面"):
        select_policy_job_intents(
            intents,
            JobExpectation("深圳", "C/C++", None, ()),
        )


def test_fill_only_smoke_restores_selected_expectation_from_checkpoint(
    tmp_path: Path,
) -> None:
    from tools.run_fill_only_smoke import _load_policy

    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "policy": {
                    "excluded_companies": [],
                    "allowed_job_keywords": ["Python"],
                    "allowed_locations": ["广州"],
                    "target_companies": 50,
                    "salary_min_k": 5,
                    "salary_max_k": 13,
                    "minimum_company_size": 20,
                    "selected_expectation": {
                        "city": "广州",
                        "role": "Python",
                        "salary": None,
                        "keywords": [],
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    policy = _load_policy(checkpoint, target_companies=2)

    assert policy.target_companies == 2
    assert policy.selected_expectation == JobExpectation(
        "广州", "Python", None, ()
    )
    assert (policy.salary_min_k, policy.salary_max_k) == (5, 13)
    assert policy.minimum_company_size == 20


def test_open_recommendations_defaults_to_first_added_expectation() -> None:
    class _JobsBrowser:
        current_url = "https://www.zhipin.com/web/geek/jobs"

        @staticmethod
        def is_logged_in() -> bool:
            return True

        @staticmethod
        def page_text() -> str:
            return ""

        @staticmethod
        def close_extra_windows() -> None:
            return None

    intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Python", None, ()),
        )
    )
    runner = BossAutomationRunner(
        _JobsBrowser(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(education="本科"),  # type: ignore[arg-type]
    )

    with patch.object(runner, "_pause_before", return_value=1.0), patch.object(
        runner,
        "_wait",
        side_effect=lambda predicate, description, timeout=None: (
            predicate(runner.browser)
            if "求职意向（" in description
            else True
        ),
    ), patch(
        "boss_assistant.automation.runner.click_expectation",
        return_value=True,
    ) as click, patch(
        "boss_assistant.automation.runner.expectation_is_active",
        return_value=True,
    ):
        runner.open_recommendations(intents)

    click.assert_called_once_with(
        runner.browser,
        "深圳",
        role="C/C++",
    )
    assert runner._selected_city == "深圳"  # noqa: SLF001
    assert runner._selected_role == "C/C++"  # noqa: SLF001


def test_run_switches_to_next_expectation_only_after_current_quota(
    tmp_path: Path,
) -> None:
    intents = JobIntentData(
        (
            JobExpectation("深圳", "C/C++", None, ()),
            JobExpectation("广州", "Golang", None, ()),
        )
    )
    cards = (
        _card(
            job_name="C/C++工程师",
            company_name="甲公司",
            location="深圳·南山区",
            fingerprint="expect-0-card",
        ),
        _card(
            job_name="Golang工程师",
            company_name="乙公司",
            location="广州·天河区",
            fingerprint="expect-1-card",
        ),
    )

    class _ReviewProvider:
        @staticmethod
        def review_card(card, *_args):
            keyword = "C/C++" if "C/C++" in card.job_name else "Golang"
            return CardReviewResult(
                eligible=True,
                job_direction_match=True,
                location_match=True,
                reason="匹配",
                combined_directions=(keyword,),
                matched_direction_keywords=(keyword,),
            )

        @staticmethod
        def review_detail(*_args):
            return DetailReviewResult(
                should_apply=True,
                score=90,
                reasons=("匹配",),
                matched_skills=("Python",),
                greeting="您好，我有相关项目经验，希望与您进一步沟通该岗位",
                qualifications_summary="匹配",
            )

    activated: list[JobExpectation] = []
    runner = BossAutomationRunner(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(
            save_snapshot=lambda *_args: SimpleNamespace(
                action=SimpleNamespace(value="inserted")
            )
        ),  # type: ignore[arg-type]
        SimpleNamespace(education=()),  # type: ignore[arg-type]
        run_directory=tmp_path,
        config=AutomationConfig(max_jobs=10, dry_run=True),
        policy=AutomationPolicy(
            excluded_companies=(),
            allowed_job_keywords=("C/C++", "Golang"),
            allowed_locations=("深圳", "广州"),
            target_companies=2,
        ),
        review_provider=_ReviewProvider(),  # type: ignore[arg-type]
    )
    runner.ensure_ready = lambda: None  # type: ignore[method-assign]
    runner.read_job_intents = lambda: intents  # type: ignore[method-assign]

    def open_recommendations(
        _intents=None,
        *,
        expectation=None,
        immediate_return=False,
    ):
        if expectation is not None:
            activated.append(expectation)

    runner.open_recommendations = open_recommendations  # type: ignore[method-assign]
    runner._return_to_recommendations = lambda **_kwargs: None  # type: ignore[method-assign]  # noqa: SLF001
    runner._open_card = lambda _card: None  # type: ignore[method-assign]  # noqa: SLF001
    runner._current_page = lambda: "recommendations"  # type: ignore[method-assign]  # noqa: SLF001
    runner._save_checkpoint = lambda *_args: tmp_path / "checkpoint.json"  # type: ignore[method-assign]  # noqa: SLF001
    runner._save_run_log = lambda *_args: tmp_path / "run.json"  # type: ignore[method-assign]  # noqa: SLF001

    def snapshot_for_selected(_browser):
        card = cards[runner._selected_expectation_index or 0]  # noqa: SLF001
        job = build_job_page_data(
            current_url="https://www.zhipin.com/web/geek/jobs",
            job_name=card.job_name,
            company_name=card.company_name,
            salary=card.salary,
            location=card.location,
            job_description="岗位职责完整文本",
            experience=card.experience,
            is_detail_page=True,
            node_count=10,
            accessible_text_count=5,
        )
        return SimpleNamespace(
            job_data=job,
            job_name=card.job_name,
            company_name=card.company_name,
            salary=card.salary,
            location=card.location,
        )

    with patch.object(
        runner,
        "process_unread_messages",
        return_value=False,
    ) as inspect_messages, patch(
        "boss_assistant.automation.runner.extract_job_cards",
        side_effect=lambda _browser: (
            cards[runner._selected_expectation_index or 0],
        ),
    ), patch(
        "boss_assistant.automation.runner.read_job_detail",
        side_effect=snapshot_for_selected,
    ):
        stats, _log_path = runner.run()

    assert activated == list(intents.expectations)
    inspect_messages.assert_called_once()
    assert inspect_messages.call_args.kwargs == {"force": True}
    assert stats.matched == 2
    assert [record["expectation_index"] for record in stats.decisions] == [0, 1]
    assert [record["expectation_quota"] for record in stats.decisions] == [1, 1]


def test_recommendation_wait_rejects_stale_cards_from_previous_city() -> None:
    shenzhen = _card(location="深圳·南山区·科技园")
    guangzhou = _card(location="广州·天河区·棠下")

    assert recommendation_cards_ready((shenzhen,), "广州市") is False
    assert recommendation_cards_ready((guangzhou,), "广州市") is True
