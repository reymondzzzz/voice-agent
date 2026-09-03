import email.utils
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Callable, Awaitable, Union

import httpx

logger = logging.getLogger(__name__)

# Upper bound for Retry-After seconds exposed to migration retry orchestrators.
_MAX_RETRY_AFTER_SECONDS = 86400.0
# Safe HalluApiError message length cap so metrics/logs never carry provider bodies.
_MAX_HALLU_API_MESSAGE_LENGTH = 256
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_OPENROUTER_LEAKY_MESSAGE_PATTERN = re.compile(
    r"^openrouter(?: streaming)? API error (\d+):",
    re.IGNORECASE,
)


def bound_retry_after_seconds(raw_value: Any) -> float | None:
    try:
        if raw_value is None:
            return None
        if isinstance(raw_value, bool):
            return None
        parsed = float(raw_value)
        if parsed != parsed or parsed < 0:
            return None
        if parsed > _MAX_RETRY_AFTER_SECONDS:
            return _MAX_RETRY_AFTER_SECONDS
        return parsed
    except (TypeError, ValueError, OverflowError) as exc:
        logger.warning("Invalid Retry-After seconds value", exc_info=exc)
        return None
    except RuntimeError:
        raise
    except AttributeError as exc:
        raise RuntimeError("bound_retry_after_seconds failed raw_value=%r: %s" % (raw_value, exc)) from exc


def parse_retry_after_header_value(
    header_value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse Retry-After from numeric seconds or HTTP-date; untrusted values become None."""
    try:
        text = str(header_value or "").strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") is False and re.fullmatch(r"\d+(?:\.\d+)?", text)):
            return bound_retry_after_seconds(float(text))
        parsed_dt = email.utils.parsedate_to_datetime(text)
        if parsed_dt is None:
            return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        reference = now if now is not None else datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        delta_seconds = (parsed_dt - reference).total_seconds()
        return bound_retry_after_seconds(delta_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        logger.warning("Invalid Retry-After header value", exc_info=exc)
        return None
    except RuntimeError:
        raise
    except AttributeError as exc:
        raise RuntimeError("parse_retry_after_header_value failed: %s" % exc) from exc


def safe_optional_headers(headers: Any) -> Mapping[str, str] | None:
    """Return a header mapping when present without requiring httpx response objects."""
    try:
        if headers is None:
            return None
        if isinstance(headers, Mapping):
            return headers
        return None
    except AttributeError as exc:
        logger.warning("Invalid optional HTTP headers", exc_info=exc)
        return None


def parse_retry_after_from_headers(
    headers: Mapping[str, str] | None,
    *,
    now: datetime | None = None,
) -> float | None:
    try:
        normalized_headers = safe_optional_headers(headers)
        if normalized_headers is None:
            return None
        header_value = None
        for key, value in normalized_headers.items():
            if str(key or "").strip().lower() == "retry-after":
                header_value = value
                break
        return parse_retry_after_header_value(header_value, now=now)
    except RuntimeError:
        raise
    except AttributeError as exc:
        logger.warning("Invalid Retry-After headers mapping", exc_info=exc)
        return None


def safe_optional_http_status(status_code: Any) -> int | None:
    try:
        if status_code is None:
            return None
        if isinstance(status_code, bool):
            return None
        code = int(status_code)
        if 100 <= code <= 599:
            return code
        return None
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid optional HTTP status", exc_info=exc)
        return None
    except RuntimeError:
        raise
    except AttributeError as exc:
        raise RuntimeError("safe_optional_http_status failed status_code=%r: %s" % (status_code, exc)) from exc


def http_status_to_safe_category(status_code: int) -> str:
    try:
        code = int(status_code)
        if code == 429:
            return "rate_limited"
        if code == 503:
            return "service_unavailable"
        if code == 408:
            return "http_408"
        if code == 409:
            return "http_409"
        if code == 425:
            return "http_425"
        if code == 400:
            return "http_400"
        if code == 401:
            return "http_401"
        if code == 403:
            return "http_403"
        if code == 404:
            return "http_404"
        if 500 <= code <= 599:
            return "http_%d" % code
        if 400 <= code < 500:
            return "http_4xx"
        return "http_%d" % code
    except (TypeError, ValueError) as exc:
        raise RuntimeError("http_status_to_safe_category failed status_code=%r: %s" % (status_code, exc)) from exc


def sanitize_hallu_api_message(message: str, *, status_code: int | None = None) -> str:
    """Strip URLs, provider bodies, and secrets from HalluApiError text for safe logs/metrics."""
    try:
        text = str(message or "").strip()
        if not text:
            if status_code is not None:
                return "hallucitron API error %d" % int(status_code)
            return "hallucitron API error"
        leaky_match = _OPENROUTER_LEAKY_MESSAGE_PATTERN.match(text)
        if leaky_match is not None:
            return "hallucitron API error %s" % leaky_match.group(1)
        text = _URL_PATTERN.sub("[url]", text)
        lowered = text.lower()
        for secret_marker in ("api_key", "apikey", "bearer ", "authorization:", "sk-", "prompt"):
            if secret_marker in lowered:
                if status_code is not None:
                    return "hallucitron API error %d" % int(status_code)
                return "hallucitron API error"
        if len(text) > _MAX_HALLU_API_MESSAGE_LENGTH:
            text = text[:_MAX_HALLU_API_MESSAGE_LENGTH]
        return text
    except (TypeError, ValueError) as exc:
        raise RuntimeError("sanitize_hallu_api_message failed: %s" % exc) from exc


def hallu_api_error_from_http_response(
    status_code: int,
    *,
    headers: Mapping[str, str] | None = None,
    source: str = "openrouter",
) -> "HalluApiError":
    """Build a safe HalluApiError from an HTTP failure without response body or URL leakage."""
    try:
        code = int(status_code)
        retry_after_seconds = parse_retry_after_from_headers(safe_optional_headers(headers))
        category = http_status_to_safe_category(code)
        safe_message = "hallucitron API error %d" % code
        if str(source or "").strip().lower() == "openrouter":
            safe_message = "openrouter API error %d" % code
        return HalluApiError(
            code,
            safe_message,
            category=category,
            retry_after_seconds=retry_after_seconds,
        )
    except RuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "hallu_api_error_from_http_response failed status_code=%r source=%r: %s" % (status_code, source, exc),
        ) from exc


# Safe category for provider responses that violate the Hallu HTTP contract.
HALLU_INVALID_CONTRACT_CATEGORY = "invalid_contract"


class HalluApiError(RuntimeError):
    """Safe OpenRouter/HTTP API failure with status, category, and optional Retry-After."""

    def __init__(
        self,
        status_code: Any,
        message: str = "",
        *,
        category: str | None = None,
        retry_after_seconds: float | None = None,
    ):
        try:
            normalized_status = safe_optional_http_status(status_code)
            self.status_code = normalized_status
            explicit_category = str(category or "").strip().lower()
            if explicit_category:
                self.category = explicit_category
            elif normalized_status is not None:
                self.category = http_status_to_safe_category(normalized_status)
            else:
                self.category = HALLU_INVALID_CONTRACT_CATEGORY
            bounded_retry = bound_retry_after_seconds(retry_after_seconds)
            self.retry_after_seconds = bounded_retry
            safe_message = sanitize_hallu_api_message(message, status_code=self.status_code)
            super().__init__(safe_message)
        except RuntimeError:
            raise
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError("HalluApiError init failed status_code=%r: %s" % (status_code, exc)) from exc

    @property
    def is_client_error(self) -> bool:
        """Return True only for bounded 4xx statuses; invalid or missing status is never client."""
        try:
            if self.status_code is None:
                return False
            return 400 <= int(self.status_code) < 500
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid Hallu API client status", exc_info=exc)
            return False


# Safe transport-failure categories exposed to callers and retry logic (no URLs, bodies, or secrets).
_HALLU_TRANSPORT_CATEGORIES = frozenset(
    {
        "connect",
        "read",
        "timeout",
        "pool",
        "network",
    }
)


class HalluTransportError(RuntimeError):
    """Retryable low-level network failure before a parsed OpenRouter API response.

    Subclasses RuntimeError so migration load-probe (G7) and invoke_model_tool_artifact
    can treat transport failures like other retryable RuntimeError paths without leaking
    httpx internals, URLs, response bodies, prompts, or credentials in the message.
    """

    def __init__(self, category: str):
        try:
            normalized = str(category or "").strip().lower() or "network"
            if normalized not in _HALLU_TRANSPORT_CATEGORIES:
                normalized = "network"
            self.category = normalized
            super().__init__("hallucitron transport failure: %s" % normalized)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("HalluTransportError init failed: %s" % exc) from exc


def hallu_transport_error_from_httpx(exc: httpx.TransportError) -> HalluTransportError:
    try:
        if isinstance(exc, httpx.PoolTimeout):
            return HalluTransportError("pool")
        if isinstance(exc, httpx.TimeoutException):
            return HalluTransportError("timeout")
        if isinstance(exc, httpx.ConnectError):
            return HalluTransportError("connect")
        if isinstance(exc, httpx.ReadError):
            return HalluTransportError("read")
        return HalluTransportError("network")
    except (TypeError, AttributeError) as map_exc:
        raise RuntimeError("hallu_transport_error_from_httpx failed: %s" % map_exc) from map_exc


@dataclass
class HalluMessage:
    role: str  # system, user, assistant, tool, cd_instruction, context_file, hint, diff, cork, title, plain_text
    content: Union[str, List[Dict[str, Any]], None]  # str or [{m_type, m_content, ...}]
    author_label: str  # name of the user, wrapped by the adapter, e.g. "Humberto" or "727893532, via Telegram"
    tool_calls: Optional[List[Dict[str, Any]]] = None  # [{id, type, function: {name, arguments}}]
    call_id: str = ""
    provider_specific_stuff: Any = None  # provider-native blocks to prepend in assistant messages (e.g. anthropic thinking blocks with signatures)
    debug_key: str = ""  # xxxyyy:001:042 used for log prefixes


@dataclass
class HalluStructuredRequest:
    prov_name: str = ""  # e.g. "OpenRouter" from flexus_workspace_provider
    prov_endpoint: str = ""
    prov_api_key: str = ""
    provm_name: str = ""
    provm_fallback_names: List[str] = field(default_factory=list)
    provm_prices: Dict[str, Any] = field(default_factory=dict)
    provm_margin: Optional[float] = None
    messages: List[HalluMessage] = field(default_factory=list)
    output_schema: Optional[Dict[str, Any]] = None
    output_schema_name: str = ""
    tools: Optional[List[Dict[str, Any]]] = None  # [{type: "function", name, description, parameters}]
    tool_choice: Optional[Any] = None
    max_tokens: int = 4096
    http_timeout_seconds: float | None = None
    reasoning_effort: Optional[str] = None  # pick one of modelcap_reasoning_effort or leave unset
    temperature: Optional[float] = None
    streaming: bool = False
    allow_empty_llm_output: bool = False
    on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None
    on_tool_call_arguments_delta: Optional[Callable[[str, str, str], Awaitable[None]]] = None
    on_tool_call_complete: Optional[Callable[[str, str, Dict[str, Any]], Awaitable[None]]] = None
    on_stream_progress: Optional[Callable[[], Awaitable[None]]] = None
    req_dump_path: str = ""
    session_id: str = ""
    user_id: str = ""
    trace_metadata: Dict[str, Any] = field(default_factory=dict)
    require_provider_usage_cost: bool = False


@dataclass
class HalluUsage:
    prompt_noncached: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    including_reasoning_tokens: int = 0
    input_images: int = 0
    call_web_search: int = 0
    call_x_search: int = 0
    call_code_interpreter: int = 0
    call_document_search: int = 0
    call_file_search: int = 0

    def __str__(self):
        parts = ["prompt_noncached=%d output=%d" % (self.prompt_noncached, self.output_tokens)]
        if self.including_reasoning_tokens > 0:
            parts.append("reasoning=%d" % self.including_reasoning_tokens)
        if self.cache_creation_input_tokens > 0:
            parts.append("cache_creation=%d" % self.cache_creation_input_tokens)
        if self.cache_read_input_tokens > 0:
            parts.append("cache_read=%d" % self.cache_read_input_tokens)
        if self.input_images > 0:
            parts.append("images=%d" % self.input_images)
        if self.call_web_search > 0:
            parts.append("web_search=%d" % self.call_web_search)
        if self.call_x_search > 0:
            parts.append("x_search=%d" % self.call_x_search)
        if self.call_code_interpreter > 0:
            parts.append("code_interpreter=%d" % self.call_code_interpreter)
        if self.call_document_search > 0:
            parts.append("document_search=%d" % self.call_document_search)
        if self.call_file_search > 0:
            parts.append("file_search=%d" % self.call_file_search)
        return " ".join(parts)


@dataclass
class HalluToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class HalluInvalidToolCall:
    call_id: str
    name: str
    raw_arguments: str
    error: str


# XXX add error message, return error within structure, for example ran out of completion tokens,
# cannot parse json, still have usage, therefore should be there in this structure.
# Then fix scenarios


@dataclass
class HalluStructuredResult:
    parsed: Any = None
    raw_text: str = ""
    thinking_text: str = ""  # anthropic thinking block text (empty if thinking not enabled)
    provider_specific_stuff: Any = None  # provider-native blocks from the response (e.g. anthropic thinking+signature), copy into assistant HalluMessage for multi-turn
    tool_calls: List[HalluToolCall] = field(default_factory=list)
    invalid_tool_calls: List[HalluInvalidToolCall] = field(default_factory=list)
    usage: HalluUsage = field(default_factory=HalluUsage)
    coins: int = 0
    provider_cost_usd: Optional[float] = None
    applied_margin_pct: Optional[float] = None
    actual_model: str = ""
    stop_reason: str = ""
    response_id: str = ""
    provider_usage_json: Dict[str, Any] = field(default_factory=dict)
    sse_log: List[Dict[str, Any]] = field(default_factory=list)


def dump_req_body(req, body):
    if not req.req_dump_path:
        return
    d = os.path.dirname(req.req_dump_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(req.req_dump_path, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
