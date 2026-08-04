"""OpenAI 兼容的大模型审核 Provider。

本模块只提供实现，不会被 GUI 或命令行入口自动加载。配置中的 ``enabled``
默认为 ``False``；在完成真实接口测试并显式启用前，任何审核调用都会被拒绝。
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from boss_assistant.page import JobPageData
from boss_assistant.resume import ResumeData

from .control import AutomationStopRequested
from .models import (
    AutomationPolicy,
    ChatConversation,
    ChatMessage,
    JobCard,
    JobIntentData,
)
from .policy import card_review_rejection_reason
from .review import (
    CardReviewResult,
    ChatReviewResult,
    DetailReviewResult,
    GreetingGroundingError,
    GreetingLengthError,
    GreetingRepairError,
    GreetingStyleError,
    ReviewError,
    _GREETING_MAX_LENGTH,
    _GREETING_MIN_LENGTH,
    _greeting_job_label,
    _greeting_job_name,
    _safely_compact_greeting,
    _safely_expand_short_greeting,
    _synthesize_grounded_greeting,
    _validate_chinese_greeting,
    _CHAT_REVIEW_INSTRUCTIONS,
    _validate_detail_rejection_evidence,
    _validate_greeting_grounding,
    merge_combined_directions,
    recruiter_requested_resume_card,
    resume_already_sent,
    validate_chat_reply,
)


class ApiProviderError(ReviewError):
    """API 配置、请求或结构化返回不符合自动审核要求。"""


class _TransportError(ApiProviderError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


JsonTransport = Callable[
    [str, Mapping[str, str], dict[str, object], float],
    dict[str, object],
]


@dataclass(frozen=True)
class ApiProviderConfig:
    """API Provider 配置；密钥不会出现在对象的 repr 中。"""

    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    api_key: str = field(default="", repr=False)
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_base_seconds: float = 1.0
    poll_interval_seconds: float = 0.5
    temperature: float = 0.1
    max_tokens: int = 4096
    thinking_mode: str = "disabled"
    use_json_response_format: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled必须是布尔值")
        for name in ("base_url", "model", "api_key", "api_key_env"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name}必须是字符串")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds必须大于0")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries必须是0-5整数")
        if self.retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds必须大于0")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds必须大于0")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature必须在0-2之间")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("max_tokens必须是正整数")
        if self.thinking_mode not in {"enabled", "disabled"}:
            raise ValueError("thinking_mode必须是enabled或disabled")
        if type(self.use_json_response_format) is not bool:
            raise ValueError("use_json_response_format必须是布尔值")

    @classmethod
    def from_json(cls, path: str | Path) -> ApiProviderConfig:
        config_path = Path(path)
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiProviderError(f"无法读取API配置 {config_path.resolve()}：{exc}") from exc
        if not isinstance(value, dict):
            raise ApiProviderError("API配置必须是JSON对象")
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ApiProviderError(f"API配置包含未知字段：{', '.join(unknown)}")
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise ApiProviderError(f"API配置无效：{exc}") from exc

    def resolved_api_key(self) -> str:
        return self.api_key.strip() or os.environ.get(self.api_key_env, "").strip()

    def validate_for_request(self) -> str:
        if not self.enabled:
            raise ApiProviderError(
                "API Provider尚未启用；完成配置审阅和真实接口测试前请保持enabled=false"
            )
        if not self.base_url.strip().startswith(("https://", "http://")):
            raise ApiProviderError("base_url必须是HTTP或HTTPS地址")
        if not self.model.strip():
            raise ApiProviderError("model尚未配置")
        api_key = self.resolved_api_key()
        if not api_key:
            raise ApiProviderError(
                f"API Key尚未配置；请填写api_key或设置环境变量{self.api_key_env}"
            )
        return api_key


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    body: dict[str, object],
    timeout_seconds: float,
) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except (OSError, IncompleteRead):
            detail = ""
        suffix = f"：{detail}" if detail else ""
        raise _TransportError(
            f"大模型API返回HTTP {exc.code}{suffix}",
            retryable=exc.code == 429 or exc.code >= 500,
        ) from exc
    except IncompleteRead as exc:
        raise _TransportError(
            f"大模型API响应传输不完整：{exc}",
            retryable=True,
        ) from exc
    except (RemoteDisconnected, ConnectionError) as exc:
        raise _TransportError(
            f"大模型API连接被远端中断：{exc}",
            retryable=True,
        ) from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise _TransportError(f"无法连接大模型API：{exc}", retryable=True) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _TransportError("大模型API返回的HTTP正文不是JSON", retryable=False) from exc
    if not isinstance(value, dict):
        raise _TransportError("大模型API返回的HTTP正文必须是JSON对象", retryable=False)
    return value


class OpenAICompatibleReviewProvider:
    """通过 OpenAI 兼容 Chat Completions 接口完成两阶段岗位审核。"""

    def __init__(
        self,
        config: ApiProviderConfig,
        *,
        transport: JsonTransport = _default_transport,
        sleep: Callable[[float], None] = time.sleep,
        control_point: Callable[[], None] | None = None,
        audit_directory: str | Path | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.sleep = sleep
        self.control_point = control_point
        self.audit_directory = Path(audit_directory) if audit_directory else None

    @staticmethod
    def _redact_audit_value(value: object, *, key: str = "") -> object:
        sensitive_keys = {
            "api_key",
            "authorization",
            "phone",
            "email",
            "wechat",
            "resume",
            "source_pdf_path",
            "resume_text",
        }
        if key.casefold() in sensitive_keys:
            if key == "resume_text" and isinstance(value, str):
                return f"[REDACTED:{len(value)} chars]"
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): OpenAICompatibleReviewProvider._redact_audit_value(
                    item_value, key=str(item_key)
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                OpenAICompatibleReviewProvider._redact_audit_value(item)
                for item in value
            ]
        if isinstance(value, str):
            value = re.sub(
                r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
                "[REDACTED_EMAIL]",
                value,
            )
            return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED_PHONE]", value)
        return value

    def _write_audit(
        self,
        *,
        kind: str,
        attempt: int,
        instructions: tuple[str, ...],
        payload: dict[str, object],
        output_schema: dict[str, object],
        raw_response: dict[str, object] | None,
        parsed_response: dict[str, object] | None,
        error: str | None,
    ) -> Path | None:
        if self.audit_directory is None:
            return None
        self.audit_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.audit_directory / (
            f"{stamp}_{kind}_attempt{attempt + 1}_{uuid.uuid4().hex[:8]}.json"
        )
        audit = {
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "kind": kind,
            "attempt": attempt + 1,
            "model": self.config.model,
            "request": {
                "instructions": list(instructions),
                "payload": payload,
                "output_schema": output_schema,
            },
            "raw_response": raw_response,
            "parsed_response": parsed_response,
            "error": error,
        }
        redacted = self._redact_audit_value(audit)
        serialized = json.dumps(redacted, ensure_ascii=False, indent=2) + "\n"
        resume_value = payload.get("resume")
        if isinstance(resume_value, dict):
            basic_info = resume_value.get("basic_info")
            if isinstance(basic_info, dict):
                personal_values = [basic_info.get("name")]
                contact = basic_info.get("contact")
                if isinstance(contact, dict):
                    personal_values.extend(contact.values())
                for personal_value in personal_values:
                    if isinstance(personal_value, str) and len(personal_value.strip()) >= 2:
                        serialized = serialized.replace(
                            personal_value.strip(), "[REDACTED_PERSONAL]"
                        )
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            serialized,
            encoding="utf-8",
        )
        temporary.replace(path)
        return path.resolve()

    @staticmethod
    def _card_payload(card: JobCard) -> dict[str, object]:
        return {
            "job_name": card.job_name,
            "company_name": card.company_name,
            "salary": card.salary,
            "company_scale": card.company_scale,
            "location": card.location,
            "recruiter_activity": card.recruiter_activity,
            "tags": list(card.tags),
        }

    @staticmethod
    def _policy_payload(policy: AutomationPolicy) -> dict[str, object]:
        return {
            "excluded_companies": list(policy.excluded_companies),
            "target_job_directions": list(policy.allowed_job_keywords),
            "excluded_job_directions": list(policy.excluded_job_directions),
            "target_cities": list(policy.allowed_locations),
            "minimum_score": policy.minimum_score,
            "salary_min_k": policy.salary_min_k,
            "salary_max_k": policy.salary_max_k,
            "minimum_company_size": policy.minimum_company_size,
        }

    @staticmethod
    def _required_bool(response: dict[str, object], key: str) -> bool:
        value = response.get(key)
        if type(value) is not bool:
            raise ApiProviderError(f"大模型返回字段 {key} 必须是布尔值")
        return value

    @staticmethod
    def _required_text(response: dict[str, object], key: str) -> str:
        value = response.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ApiProviderError(f"大模型返回字段 {key} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _text_tuple(
        response: dict[str, object],
        key: str,
        *,
        allow_empty: bool = False,
    ) -> tuple[str, ...]:
        value = response.get(key)
        if (
            not isinstance(value, list)
            or (not allow_empty and not value)
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            requirement = "字符串数组" if allow_empty else "非空字符串数组"
            raise ApiProviderError(f"大模型返回字段 {key} 必须是{requirement}")
        return tuple(item.strip() for item in value)

    @staticmethod
    def _extract_content(response: dict[str, object]) -> dict[str, object]:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ApiProviderError("大模型API返回缺少choices[0]")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ApiProviderError("大模型API返回缺少choices[0].message")
        parsed = message.get("parsed")
        if isinstance(parsed, dict):
            return parsed
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            finish_reason = choices[0].get("finish_reason")
            raise ApiProviderError(
                "大模型API返回的message.content为空"
                f"（finish_reason={finish_reason or 'unknown'}）"
            )
        text = content.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as original_error:
            # 部分OpenAI兼容接口即使启用json_object，仍会在JSON前后附带一句说明
            # 或Markdown围栏。只提取由标准JSON解码器确认完整闭合的对象；不使用
            # 正则补括号，也不接受截断JSON，避免把损坏结果送入自动化流程。
            decoder = json.JSONDecoder()
            value = None
            for index, character in enumerate(text):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(text[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    value = candidate
                    break
            if value is None:
                finish_reason = choices[0].get("finish_reason")
                raise ApiProviderError(
                    "大模型返回内容不包含完整有效的JSON对象"
                    f"（finish_reason={finish_reason or 'unknown'}，"
                    f"首次解析错误位于字符{original_error.pos}）"
                ) from original_error
        if not isinstance(value, dict):
            raise ApiProviderError("大模型返回内容必须是JSON对象")
        return value

    def _run_transport_interruptible(
        self,
        endpoint: str,
        headers: Mapping[str, str],
        body: dict[str, object],
    ) -> dict[str, object]:
        """执行一次 HTTP 请求，使暂停/停止能在请求进行中生效。

        HTTP 请求本身不可打断，因此放到后台守护线程执行，主线程按
        ``poll_interval_seconds`` 轮询：每轮经过一次 ``control_point``——遇到停止
        会抛出并向上传播（后台线程为守护线程，随自身超时结束，不阻塞进程退出），
        遇到暂停则阻塞到继续。未配置 control_point 时退化为直接同步请求，
        与原行为完全一致。
        """

        if self.control_point is None:
            return self.transport(
                endpoint, headers, body, self.config.timeout_seconds
            )

        result: dict[str, dict[str, object]] = {}
        error: list[BaseException] = []
        done = threading.Event()

        def worker() -> None:
            try:
                result["value"] = self.transport(
                    endpoint, headers, body, self.config.timeout_seconds
                )
            except BaseException as exc:  # noqa: BLE001 - 原样带回主线程重抛
                error.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while not done.wait(self.config.poll_interval_seconds):
            # 请求仍在进行：经过控制点。停止会在此抛出，暂停会在此阻塞到继续。
            self.control_point()
        if error:
            raise error[0]
        return result["value"]

    def _exchange(
        self,
        kind: str,
        instructions: tuple[str, ...],
        payload: dict[str, object],
        output_schema: dict[str, object],
    ) -> dict[str, object]:
        api_key = self.config.validate_for_request()
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        body: dict[str, object] = {
            "model": self.config.model.strip(),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "thinking": {"type": self.config.thinking_mode},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是Boss直聘岗位审核器。必须严格依据输入事实判断，不得虚构；"
                        "只返回符合指定结构的JSON对象，不要返回Markdown或额外说明。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": kind,
                            "instructions": list(instructions),
                            "payload": payload,
                            "output_schema": output_schema,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        if self.config.use_json_response_format:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        for attempt in range(self.config.max_retries + 1):
            if self.control_point:
                self.control_point()
            try:
                raw_response = self._run_transport_interruptible(
                    endpoint,
                    headers,
                    body,
                )
                try:
                    parsed_response = self._extract_content(raw_response)
                except ApiProviderError as exc:
                    self._write_audit(
                        kind=kind,
                        attempt=attempt,
                        instructions=instructions,
                        payload=payload,
                        output_schema=output_schema,
                        raw_response=raw_response,
                        parsed_response=None,
                        error=str(exc),
                    )
                    raise
                self._write_audit(
                    kind=kind,
                    attempt=attempt,
                    instructions=instructions,
                    payload=payload,
                    output_schema=output_schema,
                    raw_response=raw_response,
                    parsed_response=parsed_response,
                    error=None,
                )
                return parsed_response
            except AutomationStopRequested:
                # 停止信号必须原样向上传播，绝不能被下面的 Exception 兜底吞成
                # ApiProviderError——否则请求进行中的暂停/停止就在这里失效了。
                raise
            except _TransportError as exc:
                self._write_audit(
                    kind=kind,
                    attempt=attempt,
                    instructions=instructions,
                    payload=payload,
                    output_schema=output_schema,
                    raw_response=None,
                    parsed_response=None,
                    error=str(exc),
                )
                if not exc.retryable or attempt >= self.config.max_retries:
                    raise
                self.sleep(self.config.retry_base_seconds * (2**attempt))
            except ApiProviderError:
                raise
            except Exception as exc:
                raise ApiProviderError(f"大模型API调用失败：{exc}") from exc
        raise ApiProviderError("大模型API调用失败")

    def _parse_card_response(
        self,
        response: dict[str, object],
        card: JobCard,
        policy: AutomationPolicy,
    ) -> CardReviewResult:
        job_match = self._required_bool(response, "job_direction_match")
        location_match = self._required_bool(response, "location_match")
        excluded_match = self._required_bool(response, "excluded_direction_match")
        reason = self._required_text(response, "reason")
        resume_directions = self._text_tuple(
            response, "resume_inferred_directions", allow_empty=True
        )
        # 合并是纯机械操作：模型漏项时由程序补齐，不再因此终止整轮运行。
        combined_directions = merge_combined_directions(
            policy.allowed_job_keywords,
            resume_directions,
            self._text_tuple(response, "combined_directions"),
        )
        matched_keywords = self._text_tuple(
            response, "matched_direction_keywords", allow_empty=True
        )
        matched_excluded_keywords = self._text_tuple(
            response, "matched_excluded_direction_keywords", allow_empty=True
        )
        if job_match != bool(matched_keywords):
            raise ApiProviderError(
                "job_direction_match必须与matched_direction_keywords是否非空一致"
            )
        if excluded_match != bool(matched_excluded_keywords):
            raise ApiProviderError(
                "excluded_direction_match必须与排除方向命中关键词是否非空一致"
            )
        if not policy.excluded_job_directions and excluded_match:
            raise ApiProviderError("未配置排除岗位方向时不得返回排除命中")
        eligible = job_match and location_match and not excluded_match
        result = CardReviewResult(
            eligible,
            job_match,
            location_match,
            reason,
            resume_directions,
            combined_directions,
            matched_keywords,
            excluded_match,
            matched_excluded_keywords,
        )
        hard_rejection = card_review_rejection_reason(card, result)
        if hard_rejection:
            return CardReviewResult(
                False,
                job_match,
                location_match,
                hard_rejection,
                resume_directions,
                combined_directions,
                matched_keywords,
                excluded_match,
                matched_excluded_keywords,
            )
        return result

    def review_card(
        self,
        card: JobCard,
        policy: AutomationPolicy,
        resume: ResumeData,
        resume_text: str,
    ) -> CardReviewResult:
        instructions = (
            "不得使用固定岗位关键词表或预设方向示例。policy.target_job_directions 是用户输入的主要方向；根据结构化简历和简历原文语义推断出的方向只能作为辅助方向。不得用手机求职意向职位替代这两部分。",
            "先返回resume_inferred_directions，再把用户主要方向与简历辅助方向去重合并为combined_directions；用户输入的所有方向必须保留。",
            "排除方向最高优先。只根据card.job_name判断：岗位名在语义上包含policy.excluded_job_directions中任一方向的全部或部分有效关键词、简称或近义表达时，excluded_direction_match=true并返回matched_excluded_direction_keywords。排除方向为空时返回false和空数组。",
            "excluded_direction_match=true时该岗位不得进入详情、生成招呼语或打招呼，即使目标方向匹配也不例外。",
            "方向初筛只判断card.job_name：岗位名在语义上包含combined_directions中任一方向的全部或部分有效关键词时，job_direction_match=true并返回matched_direction_keywords；matched_direction_keywords必须来自combined_directions并实际出现在岗位名语义中，标签、公司名和招聘者状态不能作为方向命中依据。",
            "可以接受标题中的合理简称、近义表达或方向中的部分有效关键词，但HR、文员、人事、行政等词只有在combined_directions明确包含对应方向时才可命中。岗位名未实际命中任何合并方向时，必须job_direction_match=false且matched_direction_keywords为空，不得用高召回放行明显无关岗位。",
            "location_match只有在card.location能确认属于policy.target_cities任一目标城市时才为true；地点为空、解析不完整或无法确认归属时必须返回false，不能进入详情。",
            "招聘者活跃状态只作记录，不能决定卡片是否通过；标签缺失不能单独拒绝。",
            "不要返回eligible字段；程序会根据job_direction_match、location_match和excluded_direction_match计算最终卡片资格。",
        )
        payload = {
            "card": self._card_payload(card),
            "policy": self._policy_payload(policy),
            "resume": resume.to_dict(),
            "resume_text": resume_text[:30000],
        }
        output_schema = {
            "job_direction_match": "boolean",
            "location_match": "boolean",
            "excluded_direction_match": "boolean",
            "resume_inferred_directions": "string array; may be empty",
            "combined_directions": "non-empty string array",
            "matched_direction_keywords": "string array; non-empty exactly when job_direction_match is true",
            "matched_excluded_direction_keywords": "string array; non-empty exactly when excluded_direction_match is true",
            "reason": "non-empty string",
        }
        validation_error: ApiProviderError | None = None
        for attempt in range(3):
            attempt_instructions = instructions
            if validation_error is not None:
                attempt_instructions += (
                    "上一次卡片返回未通过程序校验："
                    f"{validation_error}。请重新返回完整JSON并只修正结构和字段一致性；"
                    "不得改变用户方向、排除方向或地点事实。",
                )
            response = self._exchange(
                "card_review",
                attempt_instructions,
                payload,
                output_schema,
            )
            try:
                return self._parse_card_response(response, card, policy)
            except ApiProviderError as exc:
                validation_error = exc
        raise ApiProviderError(
            f"卡片审核连续3次未通过程序校验：{validation_error}"
        )

    def review_detail(
        self,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        intents: JobIntentData,
        resume: ResumeData,
        resume_text: str,
    ) -> DetailReviewResult:
        greeting_job_name = _greeting_job_name(card.job_name)
        greeting_job_label = _greeting_job_label(card.job_name)
        instructions = (
            "结合完整岗位职责、任职要求、求职意向、结构化简历和简历原文判断，不得虚构经历。",
            "岗位名称已经通过方向初筛。详情阶段只允许两类拒投依据：完整详情明确提及并实际命中policy.excluded_job_directions中的排除方向；或详情明确提出硬性最低工作年限，且resume与resume_text明确无法满足。除此之外不得判断为不投递。",
            "少量或多数技术栈/工具/技能未覆盖、加分项缺失、学历/专业/行业背景差异、职责经验不足、泛化的‘有经验优先’以及未写明年数的经验要求，都不能单独拒投。详情出现简历未提及的技术栈、工具或技能时，greeting可以写掌握或熟悉，但绝不能捏造使用该技能的项目、实习、工作、职责、成果或交付经验。",
            "年限只把最低门槛达到3年的明确硬性要求作为候选冲突，例如3-5年或3年以上；1-3年不影响投递。只有简历明确无法满足时才写入hard_experience_requirement_conflicts。没有上述两类明确冲突时should_apply必须为true且score不得低于minimum_score。",
            "matched_detail_excluded_direction_keywords只列详情正文逐字出现且命中的用户排除方向词；hard_experience_requirement_conflicts只逐字复制详情中的硬性最低年限原文（工作年限主要来自job_detail.fields.experience字段，也可能出现在职责正文，须原样复制该文本），简历为何不满足写入reasons。两个数组都为空时should_apply必须为true；任一非空时才可为false。",
            "score为0-100，并须与should_apply和minimum_score保持一致。",
            "greeting必须使用简体中文，硬范围80-150个Unicode字符是第一规则，推荐用90-120字符简洁表达；必须以“您好，我…”或“我…”等自然第一人称表达开头，全文不得出现“本人”等生硬书面称呼，不得直接以项目名、技术名或经历名词起句。问候应简短，前20个字符仍需自然呈现最强相关证据。",
            "为通过前20字符硬校验，决定投递时greeting前20个字符中必须自然包含“我有”“我曾”“我能”“经验”“项目”“实习”“负责”“完成”“具备”“掌握”“熟悉”“擅长”“主导”“业绩”或“成果”至少一项，并且事实必须来自简历。",
            "招呼语的行业属性由你依据结构化简历与简历原文自行判断：技术类简历用技术职责与技术栈表达，非技术类（如市场、运营、销售、财务、行政、人力、教育、设计、客服等）用该行业真实的职责、业绩与成果表达；用词贴合简历所属行业，不得为了套用示例把非技术经历硬写成技术经历，也不得反之。",
            f"greeting采用完整闭环：自然第一人称开场→强证据→简历真实项目/工作或职责→明确覆盖3项以上岗位核心技能（技术岗为技术栈，非技术岗为该岗位核心能力；不足3项时全部覆盖）→结尾主动写“希望与您进一步沟通{greeting_job_label}”或同义表达。结尾只能使用payload.greeting_job_name，不得复制card.job_name中的五险一金、双休、薪资、急招等后缀。不得为了满足格式强行加入完成、交付、部署、落地、上线、达成或复盘；这些词只有在简历对同一项目或经历明确陈述时才能使用。",
            "项目经历与实习经历是独立事实区块，不得因为PDF提取后文本相邻，就把个人项目写成实习公司项目。启动部署文档只代表存在文档，测试用例只代表包含测试，不等于项目已经部署、上线或对外交付。",
            "凡是写到‘在某公司实习期间做了某项目’、‘项目已部署/交付/上线’、‘独立完成/主导’等高风险事实，整句必须能从resume_text中直接检索；不能把两个区块的词拼成新结论。",
            "参考的是结构而不是固定模板；专有名词（技术岗的技术名、非技术岗的工具/系统/证书/方法名）必须采用简历或岗位中的正确写法。技术岗不得为了套模板虚构LLM、RAG、Agent、Vibe Coding等经历；非技术岗同样不得虚构未写明的项目、业绩、客户、渠道或职责。",
            "greeting必须是简体中文，无论最终是仅填充还是实际发送都直接使用它；不得返回英文或拼音版本。",
            "仅在上述两类明确冲突导致不投递时，仍需返回理由和任职要求概括，greeting填写不发送说明。",
        )
        payload = {
            "card": self._card_payload(card),
            "job_detail": job.to_dict(),
            "policy": self._policy_payload(policy),
            "job_intents": asdict(intents),
            "resume": resume.to_dict(),
            "resume_text": resume_text[:30000],
            "greeting_job_name": greeting_job_name,
        }
        output_schema = {
            "should_apply": "boolean",
            "score": "integer 0-100",
            "reasons": "non-empty string array",
            "matched_skills": "string array; may be empty",
            "matched_detail_excluded_direction_keywords": "string array; empty unless job detail matches a configured excluded direction",
            "hard_experience_requirement_conflicts": "string array; each item cites an explicit minimum work-year requirement the resume cannot meet",
            "greeting": "non-empty Simplified Chinese string",
            "qualifications_summary": "non-empty string",
        }
        validation_error: ReviewError | None = None
        for attempt in range(3):
            attempt_instructions = instructions
            if validation_error is not None:
                attempt_instructions += (
                    "上一次返回未通过程序硬校验："
                    f"{validation_error}。请重新生成完整JSON并严格修正该问题，不得降低匹配标准或虚构事实。",
                )
            response = self._exchange(
                "detail_review",
                attempt_instructions,
                payload,
                output_schema,
            )
            try:
                return self._parse_detail_response(
                    response,
                    policy,
                    resume_text,
                    job=job,
                    job_name=card.job_name,
                )
            except GreetingLengthError as exc:
                return self._repair_greeting(
                    response,
                    card=card,
                    job=job,
                    policy=policy,
                    resume_text=resume_text,
                    initial_error=exc,
                )
            except GreetingGroundingError as exc:
                return self._repair_greeting(
                    response,
                    card=card,
                    job=job,
                    policy=policy,
                    resume_text=resume_text,
                    initial_error=exc,
                )
            except GreetingStyleError as exc:
                return self._repair_greeting(
                    response,
                    card=card,
                    job=job,
                    policy=policy,
                    resume_text=resume_text,
                    initial_error=exc,
                )
            except ReviewError as exc:
                validation_error = exc
                if attempt == 2:
                    raise ApiProviderError(
                        f"DeepSeek详情返回连续三次未通过硬校验：{exc}"
                    ) from exc
        raise ApiProviderError("DeepSeek详情审核未返回有效结果")

    def review_chat_message(
        self,
        conversation: ChatConversation,
        messages: tuple[ChatMessage, ...],
        *,
        system_notes: tuple[str, ...] = (),
    ) -> ChatReviewResult:
        trailing_recruiter: list[str] = []
        for item in reversed(messages):
            if item.from_me:
                break
            trailing_recruiter.append(item.text)
        trailing_recruiter.reverse()
        payload = {
            "recruiter_name": conversation.recruiter_name,
            "company_name": conversation.company_name,
            "position_name": conversation.position_name,
            "unread_count": conversation.unread_count,
            "conversation": [
                {
                    "speaker": "candidate" if item.from_me else "recruiter",
                    "text": item.text,
                }
                for item in messages
            ],
            # 只有“我方最后一次回应之后”HR 说的话才需要判断。
            "unanswered_recruiter_messages": trailing_recruiter,
            "system_notes": list(system_notes),
            "resume_already_sent": resume_already_sent(system_notes),
            "recruiter_requested_resume_card": recruiter_requested_resume_card(
                system_notes
            ),
        }
        output_schema = {
            "resume_requested": "boolean",
            "contact_requested": "boolean",
            "no_action_needed": "boolean",
            "reply": "non-empty string",
            "reason": "non-empty string",
        }
        validation_error: ApiProviderError | None = None
        for _ in range(3):
            instructions = _CHAT_REVIEW_INSTRUCTIONS
            if validation_error is not None:
                instructions += (
                    f"上一次返回未通过程序校验：{validation_error}。"
                    "请重新返回完整JSON并只修正结构问题，不得改变判断标准。",
                )
            response = self._exchange(
                "chat_review", instructions, payload, output_schema
            )
            try:
                resume_requested = self._required_bool(response, "resume_requested")
                contact_requested = self._required_bool(
                    response, "contact_requested"
                )
                no_action_needed = self._required_bool(
                    response, "no_action_needed"
                )
                reason = self._required_text(response, "reason")
                reply = self._required_text(response, "reply")
                if no_action_needed and (resume_requested or contact_requested):
                    raise ApiProviderError(
                        "no_action_needed=true时不能同时索要简历或联系方式"
                    )
            except ApiProviderError as exc:
                validation_error = exc
                continue
            return ChatReviewResult(
                resume_requested=resume_requested,
                contact_requested=contact_requested,
                no_action_needed=no_action_needed,
                reply=(
                    validate_chat_reply(reply)
                    if resume_requested or contact_requested
                    else ""
                ),
                reason=reason,
            )
        raise ApiProviderError(f"消息意图审核连续3次未通过程序校验：{validation_error}")

    def _repair_greeting(
        self,
        detail_response: dict[str, object],
        *,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        resume_text: str,
        initial_error: ReviewError,
    ) -> DetailReviewResult:
        """只修复招呼语格式与事实，保留已经完成的详情匹配决定。"""

        original_greeting = self._required_text(detail_response, "greeting")
        matched_skills = self._text_tuple(
            detail_response,
            "matched_skills",
            allow_empty=True,
        )
        required_skill_count = min(3, len(matched_skills))
        normalized_original = original_greeting.casefold().replace(" ", "")
        skills_already_present = [
            skill
            for skill in matched_skills
            if skill.casefold().replace(" ", "") in normalized_original
        ]
        skills_not_present = [
            skill for skill in matched_skills if skill not in skills_already_present
        ]
        locked_skills = tuple(
            (skills_already_present + skills_not_present)[:required_skill_count]
        )
        locked_skills_text = "、".join(f"“{skill}”" for skill in locked_skills)
        instructions = (
            "只修复既有招呼语的格式、长度和事实依据，不重新判断岗位，不修改评分、理由、匹配技能或其他字段。",
            "一次生成3份简体中文候选，长度分别约95、115、135个Unicode字符；每份都必须满足80-150字符这一第一硬规则，并保持简洁。",
            "原招呼语可能本身含有错误归属或虚构成果，不能把它当作事实来源。所有事实只能来自resume_text；原文超过150字符时删除重复修饰，候选不足80字符时只能补充resume_text中的原句。",
            "项目经历与实习经历必须分开，不得把个人项目写成实习公司项目；‘启动部署文档’不等于项目已部署，‘测试用例’不等于项目已交付。",
            "公司实习归属、项目部署/交付/上线、独立完成或主导等高风险整句必须能在resume_text中直接检索，否则必须删除。",
            "不得加入resume_text和matched_skills之外的新经历、新成果或新技术。",
            "招呼语的行业属性依据resume_text判断：技术类用技术职责与技术栈表达，非技术类（市场、运营、销售、财务、行政、人力、教育、设计、客服等）用该行业真实职责与成果表达，用词贴合简历，不得把非技术经历硬写成技术经历，也不得反之。",
            "必须以“您好，我…”或“我…”等自然第一人称表达开头，全文不得出现“本人”等生硬书面称呼，不得直接以项目名、技术名或经历名词起句；问候应简短，前20个字符仍需展示最强项目、经验或能力证据。",
            "为通过前20字符硬校验，每份候选前20个字符必须自然包含“我有”“我曾”“我能”“经验”“项目”“实习”“负责”“完成”“具备”“掌握”或“熟悉”至少一项，建议直接用“您好，我有…”或“您好，我曾…”开场，且该证据必须来自resume_text。",
            "事实可对resume_text做自然改写，但严禁把实习与具体项目/成果写进同一小句（例如“在X实习期间完成/部署了某项目”），也严禁在简历未写“部署/上线/交付”时凭空声称项目已部署、上线或交付；实习归属只需点到公司名，项目只需点到简历出现过的项目名。",
            f"必须逐字包含这{required_skill_count}项技能：{locked_skills_text}；它们也列在required_verbatim_skills中，不得改成缩写、同义词或合并表达；使用简历可直接支持的职责证据即可（技术岗如设计、实现、开发、参与；非技术岗如负责、运营、策划、组织、达成、服务），不强制使用部署或交付。",
            f"每份候选必须以“希望与您沟通{_greeting_job_label(card.job_name)}”或“希望与您进一步沟通{_greeting_job_label(card.job_name)}”结尾，不得带入原岗位名中的五险一金、双休、薪资、急招等后缀，也不得改写这两种固定句式。",
            "仅返回包含greetings字段的JSON对象，greetings必须是恰好3项的字符串数组；返回前逐份按Unicode字符计数。",
        )
        payload = {
            "job_name": card.job_name,
            "greeting_job_name": _greeting_job_name(card.job_name),
            "original_greeting": original_greeting,
            "original_length": len(original_greeting),
            "matched_skills": list(matched_skills),
            "required_verbatim_skills": list(locked_skills),
            "resume_text": resume_text[:30000],
        }
        output_schema = {
            "greetings": "exactly 3 Simplified Chinese strings, approximately 95, 115 and 135 Unicode characters",
        }
        validation_error: ReviewError = initial_error
        for attempt in range(3):
            attempt_instructions = instructions + (
                "上一版长度调整结果未通过程序硬校验："
                f"{validation_error}。低于80字符时必须补充原文事实，高于150字符时才继续压缩；不得新增事实。",
            )
            rewrite_response = self._exchange(
                "greeting_rewrite",
                attempt_instructions,
                payload,
                output_schema,
            )
            try:
                rewritten_greetings = self._text_tuple(
                    rewrite_response,
                    "greetings",
                )
            except ReviewError as exc:
                validation_error = exc
                rewritten_greetings = ()
            if len(rewritten_greetings) != 3:
                validation_error = ApiProviderError(
                    "greetings必须恰好包含3份候选招呼语"
                )
            candidate_errors: list[str] = []
            candidates_to_check = rewritten_greetings if len(rewritten_greetings) == 3 else ()
            for rewritten_greeting in candidates_to_check:
                candidate = dict(detail_response)
                candidate["greeting"] = rewritten_greeting
                try:
                    return self._parse_detail_response(
                        candidate,
                        policy,
                        resume_text,
                        job=job,
                        job_name=card.job_name,
                    )
                except GreetingLengthError as exc:
                    adjusted_candidate = dict(candidate)
                    adjusted_candidate["greeting"] = (
                        _safely_expand_short_greeting(
                            rewritten_greeting,
                            resume_text,
                        )
                        if exc.actual_length < _GREETING_MIN_LENGTH
                        else _safely_compact_greeting(rewritten_greeting)
                    )
                    try:
                        return self._parse_detail_response(
                            adjusted_candidate,
                            policy,
                            resume_text,
                            job=job,
                            job_name=card.job_name,
                        )
                    except ReviewError as exc:
                        candidate_errors.append(str(exc))
                except ReviewError as exc:
                    candidate_errors.append(str(exc))
            if candidate_errors:
                validation_error = ReviewError(
                    "3份候选均未通过：" + "；".join(candidate_errors)
                )
            payload["previous_greetings"] = list(rewritten_greetings)
            payload["previous_lengths"] = [len(item) for item in rewritten_greetings]
            if attempt == 2:
                fallback = self._grounded_greeting_fallback(
                    detail_response,
                    card=card,
                    job=job,
                    policy=policy,
                    resume_text=resume_text,
                    matched_skills=matched_skills,
                )
                if fallback is not None:
                    return fallback
                raise GreetingRepairError(
                    "DeepSeek招呼语修复连续三次未通过硬校验："
                    f"{validation_error}",
                    failed_greeting=original_greeting,
                ) from validation_error
        raise ApiProviderError("DeepSeek招呼语修复未返回有效结果")

    def _grounded_greeting_fallback(
        self,
        detail_response: dict[str, object],
        *,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        resume_text: str,
        matched_skills: tuple[str, ...],
    ) -> DetailReviewResult | None:
        """用简历原文本地合成招呼语兜底；仍过不了同一套硬校验时返回 None。"""

        greeting = _synthesize_grounded_greeting(
            resume_text, matched_skills, card.job_name
        )
        if greeting is None:
            return None
        candidate = dict(detail_response)
        candidate["greeting"] = greeting
        try:
            return self._parse_detail_response(
                candidate,
                policy,
                resume_text,
                job=job,
                job_name=card.job_name,
            )
        except ReviewError:
            return None

    def _parse_detail_response(
        self,
        response: dict[str, object],
        policy: AutomationPolicy,
        resume_text: str,
        *,
        job: JobPageData,
        job_name: str,
    ) -> DetailReviewResult:
        should_apply = self._required_bool(response, "should_apply")
        score = response.get("score")
        if type(score) is not int or not 0 <= score <= 100:
            raise ApiProviderError("大模型返回字段 score 必须是0-100整数")
        if should_apply != (score >= policy.minimum_score):
            raise ApiProviderError("详情审核should_apply必须与minimum_score阈值一致")
        detail_excluded_keywords = (
            self._text_tuple(
                response,
                "matched_detail_excluded_direction_keywords",
                allow_empty=True,
            )
            if "matched_detail_excluded_direction_keywords" in response
            else ()
        )
        experience_conflicts = (
            self._text_tuple(
                response,
                "hard_experience_requirement_conflicts",
                allow_empty=True,
            )
            if "hard_experience_requirement_conflicts" in response
            else ()
        )
        if detail_excluded_keywords and not policy.excluded_job_directions:
            raise ApiProviderError("未配置排除岗位方向时，详情不得返回排除方向命中")
        policy_should_apply = not detail_excluded_keywords and not experience_conflicts
        if should_apply != policy_should_apply:
            raise ApiProviderError(
                "详情页只有命中排除岗位方向或存在不满足的硬性工作年限要求时才可不投递"
            )
        _validate_detail_rejection_evidence(
            job,
            policy,
            detail_excluded_keywords,
            experience_conflicts,
        )
        reasons = self._text_tuple(response, "reasons")
        matched_skills = self._text_tuple(
            response,
            "matched_skills",
            allow_empty=True,
        )
        greeting = self._required_text(response, "greeting")
        _validate_chinese_greeting(
            greeting,
            should_apply=should_apply,
            matched_skills=matched_skills,
            job_name=job_name,
        )
        _validate_greeting_grounding(
            greeting,
            should_apply=should_apply,
            resume_text=resume_text,
        )
        qualifications = self._required_text(response, "qualifications_summary")
        return DetailReviewResult(
            should_apply,
            score,
            reasons,
            matched_skills,
            greeting,
            qualifications,
            detail_excluded_keywords,
            experience_conflicts,
        )
