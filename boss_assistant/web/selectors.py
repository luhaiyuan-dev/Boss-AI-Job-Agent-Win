"""从 Boss直聘 Web 端 DOM 提取求职意向、岗位卡片、详情与聊天数据。

对应 Android 端的 ``automation/selectors.py``：Android 靠 resource-id 后缀在 uiautomator
XML 里定位，Web 端靠 CSS/文本在实时 DOM 里定位。由于 Boss Web 前端类名会随版本变化，
所有选择器集中在 ``SELECTORS`` 并支持外部覆盖（``config/web_selectors.local.json``），
配合 ``tools/probe_dom.py`` 可在登录后快速校准，无需改动业务逻辑。

提取采用注入 JS 的方式：一次性给每张卡片打上 ``data-bossidx``，既能读取字段，又能
在返回列表后用同一个下标稳定地重新定位并点击，规避 Selenium 的元素失效问题。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from selenium.webdriver.remote.webelement import WebElement

from boss_assistant.browser import EdgeBrowser
from boss_assistant.automation.models import (
    ChatConversation,
    ChatJobInfo,
    ChatMessage,
    JobCard,
    JobExpectation,
    JobIntentData,
)
from boss_assistant.automation.requirements import degree_level
from boss_assistant.paths import runtime_root


class WebSelectionError(RuntimeError):
    """页面结构中找不到期望的元素。"""


@dataclass(frozen=True)
class CommunicationQuotaNotice:
    """“今日已与 N 位 BOSS 沟通”提醒及其可确认按钮。"""

    contacted_count: int
    remaining_count: int | None
    text: str
    confirm_button: WebElement
    limit_reached: bool = False


# ---------------------------------------------------------------- 选择器配置 ----
# 每个键是一组按优先级排列的候选 CSS 选择器；命中第一组非空结果即采用。
# 可被 config/web_selectors.local.json 中的同名键整组覆盖（便于登录后按真实 DOM 校准）。
SELECTORS: dict[str, list[str]] = {
    # 顶部导航“职位”入口。
    "positions_nav": [
        "a[ka='header-jobs']",
        "a[ka='header-job']",
        ".nav-word a[href*='geek/jobs']",
        "a[href='/web/geek/jobs']",
    ],
    # 求职意向 / 期望岗位的可点击项（点击“职位”后出现的一排）。
    "expectation_items": [
        ".expect-list .expect-item",
        ".job-expect-list li",
        ".expect-select .item",
        ".condition-filter-select .expect-item",
    ],
    # 岗位卡片容器。
    "cards": [
        "li.job-card-wrapper",
        "div.job-card-wrapper",
        "li.job-card-box",
        "div.job-card-box",
        ".rec-job-list li.card-area",
        ".job-list-container li",
    ],
    # 卡片内各字段（在卡片元素内部再次查询）。
    "card_job_name": ["[class*='job-name']", ".job-title .name", ".name"],
    "card_salary": ["[class*='salary']", ".job-limit .red", ".red"],
    "card_location": [
        "[class*='job-area']",
        "[class*='company-location']",
        "[class*='job-location']",
        ".job-title .company-location",
    ],
    "card_company": [
        ".job-card-footer .boss-name",
        ".boss-info .boss-name",
        ".boss-name",
        "[class*='company-name']",
        "[class*='company_name']",
        ".company-info .name",
    ],
    "card_tags": [
        ".tag-list li",
        "[class*='tag-list'] li",
        ".job-tags span",
        "[class*='tag_list'] li",
        ".job-card-footer li",
    ],
    "card_activity": [
        ".boss-online-tag",
        ".boss-online-icon",
        "[class*='active-time']",
        "[class*='online']",
        "[class*='active']",
        ".job-card-footer .info-public",
    ],
    # 职位页右侧固定详情面板字段。必须先限定在 job-detail-container 内，
    # 否则通用的 .job-name / .salary 会误读左侧第一张岗位卡片。
    "detail_job_name": [
        ".job-detail-container .job-detail-header .job-name",
        ".job-banner .name h1",
        ".job-primary .name",
        "[class*='job-detail'] [class*='job-name']",
        ".job-banner h1",
    ],
    "detail_salary": [
        ".job-detail-container .job-detail-header .job-salary",
        ".job-banner .salary",
        ".job-primary .salary",
        "[class*='job-detail'] [class*='salary']",
    ],
    "detail_company": [
        ".job-detail-container .job-boss-info .boss-info-attr",
        ".job-banner .company-info .name",
        ".sider-company .company-info .name",
        "[class*='company-name']",
        ".company-name",
    ],
    "detail_location": [
        ".job-detail-container .job-address .job-address-desc",
        ".job-detail-container .job-detail-header .tag-list li:first-child",
        ".job-banner .location-address",
        ".job-address .location-address",
        "[class*='location-address']",
        ".job-primary .job-area",
    ],
    "detail_experience": [
        ".job-detail-container .job-detail-header .tag-list li:nth-child(2)",
        ".job-banner .text-desc.text-experiece",
        "[class*='experiece']",
        "[class*='experience']",
    ],
    "detail_tags": [
        ".job-detail-container .job-detail-body .job-label-list li",
        ".job-banner .tag-list li",
        ".job-primary .tag-list li",
        ".job-tags li",
        "[class*='job-detail'] .tag-list li",
    ],
    "detail_description": [
        ".job-detail-container .job-detail-body .desc",
        ".job-detail-section .job-sec-text",
        ".job-sec .text",
        ".job-detail .text",
        "[class*='job-detail'] [class*='job-sec']",
        ".detail-content .text",
    ],
    # 详情页底部沟通入口。
    "chat_entry": [
        ".job-banner .btn-startchat",
        ".op-btn-chat",
        "a.btn-startchat",
        "[class*='start-chat']",
        "[class*='btn-startchat']",
    ],
    # 达到当日沟通提醒阈值后，Boss 会在职位详情上方显示全屏遮罩。必须同时
    # 校验正文语义，不能把其它同样使用 .sure-btn 的确认弹窗误当成此提醒。
    "communication_quota_dialog": [
        ".chat-block-dialog",
    ],
    "communication_quota_confirm": [
        ".chat-block-dialog .chat-block-footer .sure-btn",
        ".chat-block-dialog .sure-btn",
    ],
    # 聊天页招呼语输入框。
    "chat_editor": [
        "#chat-input",
        ".chat-input",
        ".input-area textarea",
        ".conversation-editor [contenteditable='true']",
        "div[contenteditable='true']",
        "textarea",
    ],
    "chat_send_button": [
        ".submit-btn",
        ".btn-send",
        "[class*='send-message']",
        "button[type='submit']",
    ],
    # 顶部“消息”文字旁的红色数字。真实页面结构为
    # a[ka='header-message'] > span.nav-chat-num；只读取，不点击。
    "message_unread_badge": [
        "a[ka='header-message'] .nav-chat-num",
        "a[href*='/web/geek/chat'] [class*='chat-num']",
        "a[href*='/web/geek/chat'] [class*='unread']",
        "a[href*='/web/geek/chat'] [class*='badge']",
    ],
    # 聊天会话列表。
    "chat_conversation_items": [
        ".geek-item",
        ".user-list li",
        ".chat-user-list li",
        "[class*='conversation'] li",
        ".chat-list .item",
    ],
    # 当前 Web 消息列表把姓名、公司、职位放在同一个 name-box 中：
    # span.name-text + span + i.vline + span。优先使用精确子节点，避免通用
    # [class*='name'] 把三段文本拼成 recruiter_name，进而破坏会话指纹。
    "chat_conv_name": [
        ".name-box .name-text",
        ".name-text",
        ".geek-name",
        ".name-box .name",
        "[class*='name']",
    ],
    "chat_conv_company": [
        ".name-box .name-text + span",
        "[class*='company-name']",
        "[class*='company_name']",
    ],
    "chat_conv_position": [
        ".name-box .vline + span",
        "[class*='source-job']",
        "[class*='position']",
        ".title",
    ],
    "chat_conv_last": ["[class*='last-msg']", "[class*='push-text']", ".gray"],
    # 2026-07 当前真实页面使用 span.notice-badge；保留旧结构作为回退。
    "chat_conv_badge": [
        ".notice-badge",
        "[class*='notice-badge']",
        "[class*='badge']",
        "[class*='unread']",
        ".red-tip",
    ],
    # 会话行的正文才是打开会话的实际点击区域；li 的右侧还包含操作按钮，
    # 点击整行中心在虚拟列表中可能落到空白/操作区。
    "chat_conv_open_target": [
        ".friend-content",
        ".friend-content-warp",
        ".text",
    ],
    "chat_conv_operation": [
        ".user-operation .icon-operate",
        ".user-operation",
        ".list-operate",
    ],
    # 当前会话顶部展示的招聘岗位名。招聘者在列表中的 position_name 是
    # “HR/经理/站长”等身份，不能拿来判断岗位方向。
    "chat_current_job_name": [
        ".chat-conversation .job-info .job-name",
        ".chat-conversation .job-title",
        ".chat-conversation [class*='job-name']",
        ".chat-conversation [class*='position-name']",
        ".chat-conversation a[href*='job_detail']",
    ],
    "chat_current_job_salary": [
        ".chat-conversation .left-content .salary",
        ".chat-conversation [class*='job-card'] [class*='salary']",
        ".chat-conversation [class*='position'] + [class*='salary']",
    ],
    "chat_current_job_location": [
        ".chat-conversation .left-content .city",
        ".chat-conversation [class*='job-card'] [class*='city']",
        ".chat-conversation [class*='job-card'] [class*='location']",
    ],
    # 当前会话头部身份。打开未读会话后必须先确认这里已经切换到目标 HR，
    # 不能仅因旧会话的岗位名仍在 DOM 中就提前开始读取消息。
    "chat_current_recruiter_name": [
        ".chat-conversation .base-info .name-text",
        ".chat-conversation .name-content .name-text",
        ".chat-conversation .name-text",
    ],
    # 聊天消息气泡。
    "chat_message_items": [
        ".chat-message-list .message-item",
        ".im-list .message-item",
        "[class*='message-item']",
        ".chat-content .item",
    ],
    "chat_message_mine": ["item-myself", "message-self", "self", "myself"],
    # 精确的 text-content 必须先于宽泛的 [class*='text']；后者在当前页面会把
    # “已读/送达”状态和气泡正文拼在一起，造成发送成功后的精确文本确认假阴性。
    "chat_message_text": [
        ".text-content",
        ".message-text",
        ".bubble",
        "[class*='text']",
    ],
    # 发送附件简历入口与系统提示。
    "resume_send_entry": [
        "[class*='send-resume']",
        ".resume-btn",
        ".tools-item[title*='简历']",
    ],
    "resume_confirm_button": [
        ".boss-popup__button--primary",
        ".dialog-btn .btn-sure",
        ".btn-sure",
        "button.sure",
    ],
    "resume_request_accept": [
        ".card-btn .btn-agree",
        "[class*='agree']",
        ".resume-request .agree",
    ],
    "system_notes": [
        ".chat-message-list .item-system",
        "[class*='item-system']",
        ".message-card .tip",
        # 当前 Boss Web（chat-new/v5519）的简历请求实际使用
        # ``message-card-wrap``，不是独立的 ``message-card`` 类。请求卡片和
        # 发送后的附件卡片都应作为结构化卡片上下文读取。
        ".message-card-wrap",
        ".message-card",
        "[class*='request-card']",
        "[class*='exchange-card']",
        "[class*='system-tip']",
    ],
}


_OVERRIDE_LOADED = False
_KANZHUN_DIGIT_TRANSLATION = str.maketrans(
    {codepoint: str(codepoint - 0xE031) for codepoint in range(0xE031, 0xE03B)}
)


def load_selector_overrides(path: str | Path | None = None) -> None:
    """把本地覆盖文件整组合并进 SELECTORS（每个键的候选列表整体替换）。"""

    global _OVERRIDE_LOADED
    override_path = (
        Path(path)
        if path is not None
        else runtime_root() / "config" / "web_selectors.local.json"
    )
    if not override_path.exists():
        _OVERRIDE_LOADED = True
        return
    try:
        data = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _OVERRIDE_LOADED = True
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                SELECTORS[key] = list(value)
    _OVERRIDE_LOADED = True


def _sel(key: str) -> list[str]:
    if not _OVERRIDE_LOADED:
        load_selector_overrides()
    return SELECTORS.get(key, [])


def _find_first_displayed_css(
    browser: EdgeBrowser,
    selectors: Sequence[str],
) -> WebElement | None:
    """跳过 Vue 遗留的隐藏旧节点，返回第一个真正可见的候选元素。"""

    for selector in selectors:
        for element in browser.find_all_css(selector):
            if element.is_displayed():
                return element
    return None


# ---------------------------------------------------------------- 通用文本处理 ----
def _clean(value: str | None) -> str:
    # Boss 薪资使用 kanzhun-mix 私有字体：DOM 中 U+E031..U+E03A
    # 分别显示为 0..9。浏览器肉眼正常，直接读取 textContent 则会得到乱码。
    decoded = (value or "").translate(_KANZHUN_DIGIT_TRANSLATION)
    return re.sub(r"\s+", " ", decoded).strip()


def _identity(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(normalized.split())


def build_card_fingerprint(
    job_name: str,
    company: str | None,
    salary: str | None,
    location: str | None,
) -> str:
    """优先用公司+职位稳定去重；缺公司时再并入薪资与地点。"""

    identity = [_identity(job_name), _identity(company)]
    if not company:
        identity.extend((_identity(salary), _identity(location)))
    canonical = "|".join(identity)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_conversation_fingerprint(
    recruiter_name: str,
    company_name: str | None,
    position_name: str | None,
) -> str:
    canonical = "|".join(
        (_identity(recruiter_name), _identity(company_name), _identity(position_name))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _looks_like_experience(text: str) -> bool:
    return (
        "年" in text
        or "应届" in text
        or "在校" in text
        or "经验不限" in text
        or ("经验" in text and "不限" in text)
    )


def _looks_like_degree(text: str) -> bool:
    return degree_level(text) > 0 or "学历不限" in text


def _split_experience_degree(tags: Sequence[str]) -> tuple[str | None, str | None]:
    experience: str | None = None
    degree: str | None = None
    for tag in tags:
        text = _clean(tag)
        if not text:
            continue
        if experience is None and _looks_like_experience(text):
            experience = text
        elif degree is None and _looks_like_degree(text):
            degree = text
    return experience, degree


# ---------------------------------------------------------------- 岗位卡片 ----
_COLLECT_CARDS_JS = r"""
const [cardSelectors, nameSels, salarySels, locSels, companySels, tagSels, activitySels, includeCompanyScale] = arguments;
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const pickText = (root, sels) => {
  for (const s of sels) { try { const e = root.querySelector(s); if (e) { const v = clean(e.textContent); if (v) return v; } } catch (err) {} }
  return '';
};
const pickActivity = (root, sels) => {
  for (const s of sels) {
    try {
      const e = root.querySelector(s);
      if (!e) continue;
      const text = clean(
        e.textContent || e.getAttribute('aria-label') || e.getAttribute('title')
      );
      if (text) return text;
      const cls = String(e.className || '').toLowerCase();
      if (cls.includes('online')) return '在线';
      if (cls.includes('active')) return '活跃';
    } catch (err) {}
  }
  return '';
};
const pickCompanyScale = (root) => {
  let current = root;
  for (let i = 0; i < 6 && current; i++, current = current.parentElement) {
    try {
      const vm = current.__vue__;
      const data = vm && vm.$props && vm.$props.data;
      const value = clean(data && data.brandScaleName);
      if (value) return value;
    } catch (error) {}
  }
  return '';
};
let cards = [];
for (const sel of cardSelectors) {
  try { const found = Array.from(document.querySelectorAll(sel)); if (found.length) { cards = found; break; } } catch (err) {}
}
if (!cards.length) {
  const seen = new Set();
  for (const n of document.querySelectorAll("[class*='job-name']")) {
    let el = n;
    for (let i = 0; i < 6 && el; i++) {
      el = el.parentElement;
      if (
        el &&
        el.querySelector(
          ".boss-name, [class*='company-name'], [class*='company_name']"
        )
      ) break;
    }
    if (el && !seen.has(el)) { seen.add(el); cards.push(el); }
  }
}
// 清除上一轮的下标标记。
document.querySelectorAll('[data-bossidx]').forEach(e => e.removeAttribute('data-bossidx'));
const out = [];
cards.forEach((card, i) => {
  const job_name = pickText(card, nameSels);
  if (!job_name) return;
  card.setAttribute('data-bossidx', String(i));
  const tags = [];
  for (const s of tagSels) {
    try { card.querySelectorAll(s).forEach(t => { const v = clean(t.textContent); if (v) tags.push(v); }); } catch (err) {}
    if (tags.length) break;
  }
  let job_id = card.getAttribute('data-jobid') || card.getAttribute('data-securityid') || '';
  let href = '';
  const link = card.querySelector("a[href*='job_detail'], a[href*='job-detail'], a[ka]");
  if (link) { href = link.getAttribute('href') || ''; if (!job_id) { const m = href.match(/job_detail\/([\w~-]+)/); if (m) job_id = m[1]; } }
  out.push({
    idx: i,
    job_name: job_name,
    salary: pickText(card, salarySels),
    location: pickText(card, locSels),
    company_name: pickText(card, companySels),
    tags: Array.from(new Set(tags)),
    recruiter_activity: pickActivity(card, activitySels),
    company_scale: includeCompanyScale ? pickCompanyScale(card) : '',
    job_id: job_id,
    href: href,
  });
});
return out;
"""


def iter_job_card_dicts(
    browser: EdgeBrowser,
    *,
    include_company_scale: bool = False,
) -> list[dict]:
    """注入 JS 采集当前可见的岗位卡片原始字段（并给每张卡片打 data-bossidx）。"""

    result = browser.js(
        _COLLECT_CARDS_JS,
        _sel("cards"),
        _sel("card_job_name"),
        _sel("card_salary"),
        _sel("card_location"),
        _sel("card_company"),
        _sel("card_tags"),
        _sel("card_activity"),
        include_company_scale,
    )
    return list(result) if isinstance(result, list) else []


def _dict_to_card(entry: dict) -> JobCard:
    job_name = _clean(entry.get("job_name"))
    company = _clean(entry.get("company_name")) or None
    salary = _clean(entry.get("salary")) or None
    location = _clean(entry.get("location")) or None
    tags = tuple(dict.fromkeys(_clean(t) for t in entry.get("tags") or () if _clean(t)))
    experience, degree = _split_experience_degree(tags)
    fingerprint = build_card_fingerprint(job_name, company, salary, location)
    return JobCard(
        job_name=job_name,
        company_name=company,
        salary=salary,
        location=location,
        recruiter_activity=_clean(entry.get("recruiter_activity")) or None,
        tags=tags,
        fingerprint=fingerprint,
        experience=experience,
        degree=degree,
        job_id=_clean(entry.get("job_id")) or None,
        detail_url=_clean(entry.get("href")) or None,
        company_scale=_clean(entry.get("company_scale")) or None,
    )


def _detail_url_identity(value: str | None) -> str:
    """忽略域名和跟踪参数，保留详情路径作为列表重绘前后的稳定岗位身份。"""

    raw = _clean(value)
    if not raw:
        return ""
    parsed = urlsplit(raw)
    return parsed.path.rstrip("/").casefold()


def _same_job_card(left: JobCard, right: JobCard) -> bool:
    """判断列表重绘前后的两张卡是否为同一岗位。"""

    if left.job_id and right.job_id and left.job_id == right.job_id:
        return True
    left_url = _detail_url_identity(left.detail_url)
    right_url = _detail_url_identity(right.detail_url)
    if left_url and right_url and left_url == right_url:
        return True
    return left.fingerprint == right.fingerprint


def extract_job_cards(
    browser: EdgeBrowser,
    *,
    include_company_scale: bool = False,
) -> tuple[JobCard, ...]:
    """返回当前页面的岗位卡片（按稳定身份去重）。"""

    cards: list[JobCard] = []
    seen: set[str] = set()
    for entry in iter_job_card_dicts(
        browser,
        include_company_scale=include_company_scale,
    ):
        if not _clean(entry.get("job_name")):
            continue
        card = _dict_to_card(entry)
        if card.fingerprint in seen:
            continue
        seen.add(card.fingerprint)
        cards.append(card)
    return tuple(cards)


def find_card_element(browser: EdgeBrowser, card: JobCard) -> WebElement | None:
    """在最新 DOM 中按 job_id 或指纹重新定位卡片元素，返回可点击的下标标记元素。"""

    entries = iter_job_card_dicts(browser)
    for entry in entries:
        candidate = _dict_to_card(entry)
        if _same_job_card(card, candidate):
            element = browser.find_css(f"[data-bossidx='{entry.get('idx')}']")
            if element is not None:
                return element
    return None


def select_job_card_inline(browser: EdgeBrowser, card: JobCard) -> bool:
    """在列表页把点击事件派发到卡片容器本身，避免命中内部详情链接。

    Boss 的 ``li.job-card-box`` 负责刷新右侧固定详情面板，而其内部岗位名是
    ``/job_detail/...`` 链接。坐标点击无法保证事件目标，容易直接导航到完整详情页。
    """

    entries = iter_job_card_dicts(browser)
    target_idx: object | None = None
    for entry in entries:
        candidate = _dict_to_card(entry)
        if _same_job_card(card, candidate):
            target_idx = entry.get("idx")
            break
    if target_idx is None:
        return False
    return bool(
        browser.js(
            r"""
const idx = String(arguments[0]);
const card = document.querySelector(`[data-bossidx="${CSS.escape(idx)}"]`);
if (!card) return false;
card.dispatchEvent(new MouseEvent('click', {
  bubbles: true,
  cancelable: true,
  view: window
}));
return true;
""",
            target_idx,
        )
    )


# ---------------------------------------------------------------- 求职意向 ----
_COLLECT_EXPECT_JS = r"""
const [selectors] = arguments;
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
let items = [];
for (const sel of selectors) {
  try { const found = Array.from(document.querySelectorAll(sel)); if (found.length) { items = found; break; } } catch (err) {}
}
document.querySelectorAll('[data-bossexp]').forEach(e => e.removeAttribute('data-bossexp'));
const out = [];
items.forEach((el, i) => {
  const text = clean(el.textContent);
  if (!text) return;
  el.setAttribute('data-bossexp', String(i));
  out.push({ idx: i, text: text });
});
return out;
"""


def _collect_expectations(browser: EdgeBrowser) -> list[dict]:
    result = browser.js(_COLLECT_EXPECT_JS, _sel("expectation_items"))
    return list(result) if isinstance(result, list) else []


_EXPECT_TEXT_PATTERN = re.compile(r"^\[?\s*([^\]\|·]+?)\s*[\]\|]\s*(.+)$")
_EXPECT_ROLE_CITY_PATTERN = re.compile(r"^(.+?)\s*[（(]\s*([^（）()]+)\s*[）)]$")


def _parse_expectation_text(text: str) -> JobExpectation:
    """把“[城市]角色”“城市·角色”这类文案解析为城市与角色。"""

    cleaned = _clean(text)
    match = _EXPECT_TEXT_PATTERN.match(cleaned)
    if match:
        city = match.group(1).strip() or None
        role = match.group(2).strip() or cleaned
    else:
        role_city_match = _EXPECT_ROLE_CITY_PATTERN.match(cleaned)
        if role_city_match:
            role = role_city_match.group(1).strip() or cleaned
            city = role_city_match.group(2).strip() or None
        else:
            parts = re.split(r"[·|/]", cleaned, maxsplit=1)
            if len(parts) == 2 and 2 <= len(parts[0]) <= 6:
                city, role = parts[0].strip() or None, parts[1].strip() or cleaned
            else:
                city, role = None, cleaned
    return JobExpectation(city=city, role=role, salary=None, keywords=())


def parse_job_intents(browser: EdgeBrowser) -> JobIntentData:
    """读取页面上的求职意向列表。"""

    entries = _collect_expectations(browser)
    expectations: list[JobExpectation] = []
    seen: set[str] = set()
    for entry in entries:
        text = _clean(entry.get("text"))
        if not text or text in {"全职期望", "兼职期望", "新增期望", "管理期望"}:
            continue
        expectation = _parse_expectation_text(text)
        key = _identity(expectation.role) + "|" + _identity(expectation.city)
        if key in seen:
            continue
        seen.add(key)
        expectations.append(expectation)
    return JobIntentData(tuple(expectations), None)


def find_expectation_element(
    browser: EdgeBrowser, city: str | None, *, role: str | None = None
) -> WebElement | None:
    """定位与目标城市（可选角色）匹配的求职意向可点击元素。"""

    entries = _collect_expectations(browser)
    target_city = _identity(city)
    target_role = _identity(role)

    def score(text: str) -> int:
        ident = _identity(text)
        value = 0
        if target_city and target_city in ident:
            value += 2
        if target_role and target_role in ident:
            value += 1
        return value

    best_idx: int | None = None
    best_score = 0
    for entry in entries:
        current = score(_clean(entry.get("text")))
        if current > best_score:
            best_score = current
            best_idx = entry.get("idx")
    if best_idx is None:
        # 没有城市匹配时退回第一条求职意向，避免空推荐。
        if entries and not target_city:
            best_idx = entries[0].get("idx")
        else:
            return None
    return browser.find_css(f"[data-bossexp='{best_idx}']")


_CLICK_EXPECTATION_JS = r"""
const [selectors, targetCity, targetRole] = arguments;
const clean = (value) => (value || '').replace(/\s+/g, '').toLowerCase();
let items = [];
for (const selector of selectors) {
  try {
    const found = Array.from(document.querySelectorAll(selector));
    if (found.length) { items = found; break; }
  } catch (error) {}
}
const city = clean(targetCity);
const role = clean(targetRole);
let best = null;
let bestScore = -1;
for (const item of items) {
  const text = clean(item.textContent);
  let score = 0;
  if (city && text.includes(city)) score += 4;
  if (role && (text.includes(role) || role.includes(text))) score += 2;
  if (!city && !role && best === null) score = 1;
  if (score > bestScore) {
    best = item;
    bestScore = score;
  }
}
if (!best || (city && bestScore < 4)) return {clicked: false, text: ''};
best.click();
return {
  clicked: true,
  text: (best.textContent || '').replace(/\s+/g, ' ').trim(),
};
"""


def click_expectation(
    browser: EdgeBrowser,
    city: str | None,
    *,
    role: str | None = None,
) -> bool:
    """在同一 DOM 帧内匹配并点击求职意向，避免页面重绘导致元素失效。"""

    result = browser.js(
        _CLICK_EXPECTATION_JS,
        _sel("expectation_items"),
        city or "",
        role or "",
    )
    return bool(isinstance(result, dict) and result.get("clicked"))


_EXPECTATION_ACTIVE_JS = r"""
const [selectors, targetCity, targetRole] = arguments;
const clean = (value) => (value || '').replace(/\s+/g, '').toLowerCase();
let items = [];
for (const selector of selectors) {
  try {
    const found = Array.from(document.querySelectorAll(selector));
    if (found.length) { items = found; break; }
  } catch (error) {}
}
const city = clean(targetCity);
const role = clean(targetRole);
return items.some((item) => {
  const active = item.classList.contains('active')
    || item.classList.contains('selected')
    || item.classList.contains('current')
    || item.getAttribute('aria-selected') === 'true'
    || item.getAttribute('data-selected') === 'true';
  if (!active) return false;
  const text = clean(item.textContent);
  return (!city || text.includes(city))
    && (!role || text.includes(role) || role.includes(text));
});
"""


def expectation_is_active(
    browser: EdgeBrowser,
    city: str | None,
    *,
    role: str | None = None,
) -> bool:
    """确认网页真实激活项与目标城市/角色一致，而不只确认点击事件已发出。"""

    return bool(
        browser.js(
            _EXPECTATION_ACTIVE_JS,
            _sel("expectation_items"),
            city or "",
            role or "",
        )
    )


def click_positions_tab(
    browser: EdgeBrowser,
    *,
    before_click: Callable[[str], None] | None = None,
) -> bool:
    """点击顶部“职位”入口；找不到显式入口时返回 False，由运行器改用直达 URL。"""

    element = browser.find_first_css(_sel("positions_nav"))
    if element is None:
        element = browser.find_clickable_by_text(["职位"], tags=("a", "span", "li"))
    if element is None:
        return False
    if before_click:
        before_click("点击“职位”")
    browser.click(element, description="职位")
    return True


# ---------------------------------------------------------------- 详情沟通入口 ----
CHAT_ENTRY_TEXTS = ("立即沟通", "继续沟通")
_COMMUNICATION_QUOTA_NOTICE_RE = re.compile(
    r"今天已(?:经)?与\s*(\d+)\s*位\s*BOSS沟通.*?还剩\s*(\d+)\s*次沟通机会",
    flags=re.IGNORECASE | re.DOTALL,
)
_DAILY_COMMUNICATION_LIMIT_RE = re.compile(
    r"您已达到沟通上限.*?今天已(?:经)?与\s*(150)\s*位\s*BOSS沟通"
    r".*?明天再来",
    flags=re.IGNORECASE | re.DOTALL,
)


def find_chat_entry(browser: EdgeBrowser) -> WebElement | None:
    # Vue 切换右侧详情时可能短暂保留上一岗位的隐藏按钮。同一 CSS 命中多个
    # 节点时，find_first_css() 永远返回 index=0，导致可见的新按钮一直被遮蔽。
    # 必须遍历全部候选并只返回当前真正可见且文案可靠的入口。
    for selector in _sel("chat_entry"):
        for element in browser.find_all_css(selector):
            if (
                element.is_displayed()
                and browser.text_of(element) in CHAT_ENTRY_TEXTS + ("沟通",)
            ):
                return element
    return browser.find_clickable_by_text(list(CHAT_ENTRY_TEXTS))


def read_communication_quota_notice(
    browser: EdgeBrowser,
) -> CommunicationQuotaNotice | None:
    """识别可继续的次数提醒或150位当日硬上限，排除其它通用确认框。"""

    dialog = _find_first_displayed_css(
        browser,
        _sel("communication_quota_dialog"),
    )
    if dialog is None:
        return None
    text = browser.text_of(dialog)
    limit_match = _DAILY_COMMUNICATION_LIMIT_RE.search(text)
    quota_match = _COMMUNICATION_QUOTA_NOTICE_RE.search(text)
    if limit_match is None and quota_match is None:
        return None
    confirm = _find_first_displayed_css(
        browser,
        _sel("communication_quota_confirm"),
    )
    if confirm is None:
        return None
    if limit_match is not None:
        # 达到硬上限的弹窗必须是当前实测的“确定”，避免把同容器下其它确认操作
        # 误判为终止信号。
        if browser.text_of(confirm).strip() != "确定":
            return None
        return CommunicationQuotaNotice(
            contacted_count=int(limit_match.group(1)),
            remaining_count=None,
            text=text,
            confirm_button=confirm,
            limit_reached=True,
        )
    assert quota_match is not None
    return CommunicationQuotaNotice(
        contacted_count=int(quota_match.group(1)),
        remaining_count=int(quota_match.group(2)),
        text=text,
        confirm_button=confirm,
    )


def find_greeting_editor(browser: EdgeBrowser) -> WebElement | None:
    return browser.find_first_css(_sel("chat_editor"))


def find_send_button(browser: EdgeBrowser) -> WebElement | None:
    element = browser.find_first_css(_sel("chat_send_button"))
    if element is not None:
        return element
    return browser.find_clickable_by_text(["发送"], tags=("button", "a", "span", "div"))


# ---------------------------------------------------------------- 聊天会话列表 ----
_READ_MESSAGE_UNREAD_JS = r"""
const [badgeSelectors] = arguments;
const visible = (el) => {
  if (!el) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== 'none'
    && style.visibility !== 'hidden'
    && rect.width > 0
    && rect.height > 0;
};
for (const selector of badgeSelectors) {
  try {
    for (const badge of document.querySelectorAll(selector)) {
      if (!visible(badge)) continue;
      const text = (badge.textContent || badge.getAttribute('aria-label') || '').trim();
      const match = text.match(/(\d+)/);
      if (match) return parseInt(match[1], 10);
    }
  } catch (error) {}
}
return 0;
"""


def read_message_unread_count(browser: EdgeBrowser) -> int:
    """只读顶部“消息”文字旁的可见红色数字，不进入消息页。"""

    result = browser.js(_READ_MESSAGE_UNREAD_JS, _sel("message_unread_badge"))
    try:
        return max(0, int(result or 0))
    except (TypeError, ValueError):
        return 0


_COLLECT_CONV_JS = r"""
const [itemSels, nameSels, companySels, posSels, lastSels, badgeSels] = arguments;
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const pick = (root, sels) => { for (const s of sels){ try{ const e=root.querySelector(s); if(e){const v=clean(e.textContent); if(v) return v;} }catch(err){} } return ''; };
const visible = (el) => {
  try {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0
      && rect.width > 0 && rect.height > 0;
  } catch (err) { return false; }
};
let items = [];
for (const sel of itemSels){ try{ const f=Array.from(document.querySelectorAll(sel)); if(f.length){items=f;break;} }catch(err){} }
document.querySelectorAll('[data-bossconv]').forEach(e => e.removeAttribute('data-bossconv'));
const out = [];
items.forEach((el, i) => {
  // Boss 虚拟列表会短暂保留宽高为 0 的旧会话节点。若给这种节点打标并返回，
  // 随后的打开/置顶会连续报“元素不可见”。
  if (!visible(el)) return;
  const name = pick(el, nameSels);
  if (!name) return;
  el.setAttribute('data-bossconv', String(i));
  let unread = 0;
  for (const s of badgeSels){ try{ const b=el.querySelector(s); if(b){ const m=clean(b.textContent).match(/(\d+)/); if(m){ unread=parseInt(m[1]); break; } } }catch(err){} }
  // 手机端置顶同步到 Web 后，真实标记位于行内 .friend-content.friend-top，
  // li 自身没有 pinned/top class。保留旧结构兼容，同时识别当前 friend-top。
  const pinned = /pinned|top|sticky/i.test(el.className || '')
    || !!el.querySelector(".friend-top, [class*='top-tip'], [class*='sticky']");
  out.push({
    idx: i,
    name: name,
    company: pick(el, companySels),
    position: pick(el, posSels),
    last: pick(el, lastSels),
    unread: unread,
    pinned: pinned
  });
});
return out;
"""


def _collect_conversations(browser: EdgeBrowser) -> list[dict]:
    result = browser.js(
        _COLLECT_CONV_JS,
        _sel("chat_conversation_items"),
        _sel("chat_conv_name"),
        _sel("chat_conv_company"),
        _sel("chat_conv_position"),
        _sel("chat_conv_last"),
        _sel("chat_conv_badge"),
    )
    return list(result) if isinstance(result, list) else []


def extract_chat_conversations(browser: EdgeBrowser) -> tuple[ChatConversation, ...]:
    conversations: list[ChatConversation] = []
    seen: set[str] = set()
    for entry in _collect_conversations(browser):
        recruiter_name = _clean(entry.get("name"))
        if not recruiter_name:
            continue
        company_name = _clean(entry.get("company")) or None
        position_name = _clean(entry.get("position")) or None
        # 兼容旧页面/本地 selector 覆盖：旧结构可能把“公司|职位”作为一个字段。
        if position_name and not company_name:
            contact = position_name
            parts = [part.strip() for part in re.split(r"[|·]", contact)]
            if len(parts) > 1:
                company_name = parts[0] or None
                position_name = parts[1] if parts[1] else None
        fingerprint = build_conversation_fingerprint(
            recruiter_name, company_name, position_name
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        conversations.append(
            ChatConversation(
                recruiter_name=recruiter_name,
                company_name=company_name,
                position_name=position_name,
                last_message=_clean(entry.get("last")),
                unread_count=int(entry.get("unread") or 0),
                last_message_from_me=False,
                fingerprint=fingerprint,
                pinned=bool(entry.get("pinned")),
            )
        )
    return tuple(conversations)


def find_conversation_element(
    browser: EdgeBrowser, fingerprint: str
) -> WebElement | None:
    for entry in _collect_conversations(browser):
        recruiter_name = _clean(entry.get("name"))
        if not recruiter_name:
            continue
        company_name = _clean(entry.get("company")) or None
        position_name = _clean(entry.get("position")) or None
        if position_name and not company_name:
            contact = position_name
            parts = [part.strip() for part in re.split(r"[|·]", contact)]
            if len(parts) > 1:
                company_name = parts[0] or None
                position_name = parts[1] if parts[1] else None
        current = build_conversation_fingerprint(
            recruiter_name, company_name, position_name
        )
        if current == fingerprint:
            return browser.find_css(f"[data-bossconv='{entry.get('idx')}']")
    return None


def _conversation_index(browser: EdgeBrowser, fingerprint: str) -> object | None:
    for entry in _collect_conversations(browser):
        recruiter_name = _clean(entry.get("name"))
        if not recruiter_name:
            continue
        company_name = _clean(entry.get("company")) or None
        position_name = _clean(entry.get("position")) or None
        if position_name and not company_name:
            parts = [
                part.strip()
                for part in re.split(r"[|·]", position_name)
            ]
            if len(parts) > 1:
                company_name = parts[0] or None
                position_name = parts[1] or None
        if (
            build_conversation_fingerprint(
                recruiter_name, company_name, position_name
            )
            == fingerprint
        ):
            return entry.get("idx")
    return None


def find_conversation_open_target(
    browser: EdgeBrowser, fingerprint: str
) -> WebElement | None:
    idx = _conversation_index(browser, fingerprint)
    if idx is None:
        return None
    for selector in _sel("chat_conv_open_target"):
        element = browser.find_css(
            f"[data-bossconv='{idx}'] {selector}"
        )
        if element is not None:
            return element
    return browser.find_css(f"[data-bossconv='{idx}']")


def find_conversation_operation_element(
    browser: EdgeBrowser, fingerprint: str
) -> WebElement | None:
    idx = _conversation_index(browser, fingerprint)
    if idx is None:
        return None
    for selector in _sel("chat_conv_operation"):
        element = browser.find_css(
            f"[data-bossconv='{idx}'] {selector}"
        )
        if element is not None:
            return element
    return None


def read_current_chat_job_name(browser: EdgeBrowser) -> str | None:
    for selector in _sel("chat_current_job_name"):
        element = browser.find_css(selector)
        if element is None:
            continue
        text = _clean(browser.text_of(element))
        if text:
            return text
    return None


def read_current_chat_job_info(browser: EdgeBrowser) -> ChatJobInfo:
    """读取当前聊天顶部真实岗位名、薪资和城市，不以列表招聘者身份代替岗位。"""

    def read(selectors: list[str]) -> str | None:
        for selector in selectors:
            for element in browser.find_all_css(selector):
                if not element.is_displayed():
                    continue
                text = _clean(browser.text_of(element))
                if text:
                    return text
        return None

    return ChatJobInfo(
        job_name=read(_sel("chat_current_job_name")),
        salary=read(_sel("chat_current_job_salary")),
        location=read(_sel("chat_current_job_location")),
    )


def read_current_chat_identity(
    browser: EdgeBrowser,
) -> tuple[str | None, str | None]:
    """读取当前聊天头部的 HR 姓名和公司，供会话切换完成校验使用。"""

    result = browser.js(
        r"""
const [nameSels] = arguments;
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const root = document.querySelector('.chat-conversation');
if (!root) return { recruiter: '', company: '' };
let recruiter = '';
for (const sel of nameSels) {
  try {
    const el = document.querySelector(sel);
    const value = clean(el && el.textContent);
    if (value) { recruiter = value; break; }
  } catch (err) {}
}
const base = root.querySelector('.base-info');
let company = '';
if (base) {
  for (const child of Array.from(base.children)) {
    if (child.tagName !== 'SPAN') continue;
    if (child.classList.contains('name-text') ||
        child.classList.contains('base-title')) continue;
    const value = clean(child.textContent);
    if (value && value !== recruiter) { company = value; break; }
  }
}
return { recruiter, company };
""",
        _sel("chat_current_recruiter_name"),
    )
    if not isinstance(result, dict):
        return None, None
    return (
        _clean(result.get("recruiter")) or None,
        _clean(result.get("company")) or None,
    )


# ---------------------------------------------------------------- 聊天消息 ----
_COLLECT_MSG_JS = r"""
const [itemSels, mineMarkers, textSels, systemSels] = arguments;
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
let items = [];
for (const sel of itemSels){ try{ const f=Array.from(document.querySelectorAll(sel)); if(f.length){items=f;break;} }catch(err){} }
const out = [];
items.forEach((el, i) => {
  if (systemSels.some(s => { try { return el.matches(s) || !!el.querySelector(s); } catch(err) { return false; } })) return;
  const cls = (el.className || '') + ' ' + (el.getAttribute('data-type') || '');
  let text = '';
  for (const s of textSels){ try{ const e=el.querySelector(s); if(e){ const v=clean(e.textContent); if(v){text=v;break;} } }catch(err){} }
  if (!text) text = clean(el.textContent);
  if (!text) return;
  const mine = mineMarkers.some(m => cls.indexOf(m) >= 0);
  out.push({ idx: i, text: text, mine: mine });
});
return out;
"""


def extract_chat_messages(browser: EdgeBrowser) -> tuple[ChatMessage, ...]:
    result = browser.js(
        _COLLECT_MSG_JS,
        _sel("chat_message_items"),
        _sel("chat_message_mine"),
        _sel("chat_message_text"),
        _sel("system_notes"),
    )
    entries = list(result) if isinstance(result, list) else []
    messages: list[ChatMessage] = []
    for order, entry in enumerate(entries):
        text = _clean(entry.get("text"))
        if not text:
            continue
        messages.append(
            ChatMessage(text=text, from_me=bool(entry.get("mine")), top=order)
        )
    return tuple(messages)


def extract_chat_system_notes(browser: EdgeBrowser) -> tuple[str, ...]:
    notes: list[str] = []
    for element in browser.find_all_first_css(_sel("system_notes")):
        text = _clean(browser.text_of(element))
        if text and text not in notes:
            notes.append(text)
    return tuple(notes)


def resume_request_accept_button(browser: EdgeBrowser) -> WebElement | None:
    # 不能使用全局“.btn-agree/同意”：联系方式交换卡片也有同名按钮，曾造成
    # 程序把微信号发给 HR 后仍等待“简历已发送”，最终表现为确认超时。
    marked = browser.js(
        r"""
document.querySelectorAll('[data-boss-resume-accept]').forEach(
  e => e.removeAttribute('data-boss-resume-accept')
);
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const cards = document.querySelectorAll(
  '.message-card, [class*="message-card"], [class*="request-card"], ' +
  '[class*="exchange-card"], .chat-message-list li, [class*="message-item"]'
);
for (const card of cards) {
  const text = clean(card.textContent);
  const isResumeRequest =
    (text.includes('附件简历') || text.includes('发一份简历') ||
     text.includes('发送简历')) &&
    !/附件简历.{0,40}已发送|查看了您的附件简历/.test(text);
  if (!isResumeRequest) continue;
  for (const el of card.querySelectorAll('button, a, span, div')) {
    const label = clean(el.textContent);
    if (label === '同意' || label === '发送简历') {
      el.setAttribute('data-boss-resume-accept', '1');
      return true;
    }
  }
}
return false;
"""
    )
    if not marked:
        return None
    return browser.find_css("[data-boss-resume-accept='1']")


def find_resume_send_entry(browser: EdgeBrowser) -> WebElement | None:
    element = browser.find_first_css(_sel("resume_send_entry"))
    if element is not None:
        return element
    return browser.find_clickable_by_text(["发简历", "发送简历"], tags=("a", "button", "span", "div"))


def find_resume_confirm_button(browser: EdgeBrowser) -> WebElement | None:
    element = browser.find_first_css(_sel("resume_confirm_button"))
    if element is not None:
        return element
    return browser.find_clickable_by_text(
        ["确认", "确定", "确认发送"], tags=("a", "button", "span")
    )
