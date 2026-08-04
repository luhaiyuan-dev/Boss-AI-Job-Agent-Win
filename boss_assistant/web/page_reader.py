"""从 Boss直聘 Web 端职位详情页读取结构化数据。

对应 Android 端 ``page/reader.py`` 的 ``read_current_page``：Android 从 uiautomator XML
结构化五个字段，Web 端从详情 DOM 读取同样的字段并构造相同的 ``JobPageData`` /
``PageSnapshot``，让 ``storage`` 与 ``review`` 复用模块无需改动。Web 详情页没有“查看
更多”，点开卡片即展示完整职责，因此直接读取即可。
"""

from __future__ import annotations

from dataclasses import replace

from boss_assistant.automation.models import JobCard
from boss_assistant.browser import EdgeBrowser
from boss_assistant.page import (
    AccessibleText,
    NodeSample,
    PageSnapshot,
    build_job_page_data,
    make_field,
)

from .selectors import _clean, _sel


_READ_DETAIL_JS = r"""
const [nameSels, salarySels, companySels, locSels, expSels, descSels, tagSels] = arguments;
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
const pick = (sels) => { for (const s of sels){ try{ const e=document.querySelector(s); if(e){ const v=clean(e.textContent); if(v) return v; } }catch(err){} } return ''; };
const joinAll = (sels) => {
  for (const s of sels){
    try {
      const nodes = Array.from(document.querySelectorAll(s));
      if (nodes.length){
        const parts = nodes.map(n => (n.innerText || n.textContent || '').trim()).filter(Boolean);
        if (parts.length) return parts.join('\n');
      }
    } catch(err){}
  }
  return '';
};
const tags = [];
for (const s of tagSels){ try{ document.querySelectorAll(s).forEach(t=>{const v=clean(t.textContent); if(v) tags.push(v);}); }catch(err){} if(tags.length) break; }
return {
  job_name: pick(nameSels),
  salary: pick(salarySels),
  company_name: pick(companySels),
  location: pick(locSels),
  experience: pick(expSels),
  description: joinAll(descSels),
  tags: Array.from(new Set(tags)),
  node_count: document.querySelectorAll('*').length,
};
"""


def read_job_detail(browser: EdgeBrowser) -> PageSnapshot:
    """读取当前详情页并结构化为 PageSnapshot。"""

    raw = browser.js(
        _READ_DETAIL_JS,
        _sel("detail_job_name"),
        _sel("detail_salary"),
        _sel("detail_company"),
        _sel("detail_location"),
        _sel("detail_experience"),
        _sel("detail_description"),
        _sel("detail_tags"),
    )
    data = raw if isinstance(raw, dict) else {}

    job_name = _clean(data.get("job_name"))
    company_name = _clean(data.get("company_name"))
    salary = _clean(data.get("salary"))
    location = _clean(data.get("location"))
    experience = _clean(data.get("experience"))
    description = data.get("description") or ""
    tags = [t for t in (data.get("tags") or []) if _clean(t)]

    # 右侧固定面板把公司和招聘者职位合并为“公司 · 人事”，只保留公司名，
    # 以便和左侧岗位卡片、30 天投递记录做稳定比对。
    if "·" in company_name:
        company_name = company_name.split("·", 1)[0].strip()

    # 经验字段没读到时，从详情标签里按词义补一条（如“3-5年”）。
    if not experience:
        for tag in tags:
            text = _clean(tag)
            if "年" in text or "应届" in text or "经验" in text:
                experience = text
                break

    # 详情正文并入标签，供硬性年限/周末休息等文本校验。
    description_text = description
    if tags:
        description_text = (description_text + "\n" + " ".join(tags)).strip()

    is_detail_page = bool(job_name and (description or company_name))
    node_count = int(data.get("node_count") or 0)

    job_data = build_job_page_data(
        current_url=browser.current_url,
        job_name=job_name or None,
        company_name=company_name or None,
        salary=salary or None,
        location=location or None,
        job_description=description_text or None,
        experience=experience or None,
        is_detail_page=is_detail_page,
        node_count=node_count,
        accessible_text_count=len(tags) + (1 if description else 0),
    )

    texts: list[AccessibleText] = []
    if job_name:
        texts.append(AccessibleText(job_name, "dom", "detail_job_name", ""))
    if company_name:
        texts.append(AccessibleText(company_name, "dom", "detail_company", ""))
    for tag in tags:
        texts.append(AccessibleText(_clean(tag), "dom", "detail_tag", ""))

    samples = [NodeSample(node_index=0, attributes={"url": browser.current_url})]
    raw_content = description_text or browser.page_text()[:8000]

    return PageSnapshot(
        raw_xml=raw_content,
        texts=texts,
        node_count=node_count,
        node_samples=samples,
        job_data=job_data,
    )


def align_detail_identity(
    snapshot: PageSnapshot,
    card: JobCard,
) -> PageSnapshot:
    """用已审核的列表卡片锁定岗位身份，详情面板只提供正文等详情字段。

    Boss 完整详情页和推荐侧栏可能同时存在多组 ``job/company`` 节点，宽泛详情
    选择器曾把当前安点科技读取成字节跳动、益普科技读取成意思网络。卡片才是本轮
    被审核和点击的稳定身份来源，详情页不得反向覆盖它。
    """

    job_data = replace(
        snapshot.job_data,
        job_name=make_field(card.job_name, source="job_card"),
        company_name=make_field(card.company_name, source="job_card"),
    )
    try:
        return replace(snapshot, job_data=job_data)
    except TypeError:
        # 兼容离线测试中的轻量快照替身；真实运行始终是 PageSnapshot。
        snapshot.job_data = job_data  # type: ignore[misc]
        return snapshot
