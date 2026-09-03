import json

from voice_agent.pipeline.hallu_structs import (
    HalluInvalidToolCall,
    HalluStructuredRequest,
    HalluStructuredResult,
    HalluToolCall,
    HalluUsage,
    dump_req_body,
)

OPENROUTER_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://flexus.ai",
    "X-Title": "Flexus",
}


def openrouter_request_headers(api_key: str) -> dict:
    return {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        **OPENROUTER_ATTRIBUTION_HEADERS,
    }


def _model_rejects_temperature(model_name: str) -> bool:
    m = (model_name or "").lower()
    if "/" in m:
        m = m.split("/", 1)[1]
    return m.startswith("gpt-5") or m.startswith(("o1", "o3", "o4"))


# GMICloud often returns empty content for z-ai/glm-5.2; exclude it from routing.
_GLM_52_OPENROUTER_MODEL_PREFIX = "z-ai/glm-5.2"
_GLM_52_OPENROUTER_PROVIDER_ROUTING = {"ignore": ["GMICloud", "Decart"]}


def _model_needs_glm_52_provider_routing(model_name: str) -> bool:
    return (model_name or "").strip().lower().startswith(_GLM_52_OPENROUTER_MODEL_PREFIX)


def normalize_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        fn = t.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            out.append({"type": "function", "function": fn})
        elif t.get("type") == "function" and t.get("name"):
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                }
            )
        else:
            out.append(t)
    return out


def build_openrouter_body(req: HalluStructuredRequest, messages: list[dict], has_pdf: bool, stream: bool) -> dict:
    body: dict = {"messages": messages}
    if req.provm_fallback_names:
        body["models"] = [req.provm_name] + req.provm_fallback_names
    else:
        body["model"] = req.provm_name
    if stream:
        body["stream"] = True
        body["usage"] = {"include": True}
    if req.output_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": req.output_schema_name,
                "schema": req.output_schema,
                "strict": True,
            },
        }
    if req.tools:
        body["tools"] = normalize_tools(req.tools)
        if req.output_schema:
            body["tool_choice"] = "none"
        elif req.tool_choice is not None:
            body["tool_choice"] = req.tool_choice
    if req.max_tokens > 0:
        body["max_tokens"] = req.max_tokens
    if req.reasoning_effort == "none":
        body["reasoning"] = {"enabled": False}
    elif req.reasoning_effort:
        effort = "xhigh" if req.reasoning_effort == "max" else req.reasoning_effort
        body["reasoning"] = {"effort": effort}
    reasoning_blocks_temperature = req.reasoning_effort and req.reasoning_effort != "none"
    if req.temperature is not None and not reasoning_blocks_temperature and not _model_rejects_temperature(req.provm_name):
        body["temperature"] = req.temperature
    if has_pdf:
        body["plugins"] = [{"id": "file-parser", "pdf": {"engine": "native"}}]
    if req.session_id:
        body["session_id"] = req.session_id
    if req.user_id:
        body["user"] = req.user_id
    if req.trace_metadata:
        body["trace"] = req.trace_metadata
    if _model_needs_glm_52_provider_routing(req.provm_name):
        body["provider"] = dict(_GLM_52_OPENROUTER_PROVIDER_ROUTING)
    return body


def map_usage(usage: dict) -> HalluUsage:
    prompt_tokens = usage.get("prompt_tokens", 0)
    prompt_details = usage.get("prompt_tokens_details") or {}
    cached_tokens = prompt_details.get("cached_tokens", 0)
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = completion_details.get("reasoning_tokens", 0)
    return HalluUsage(
        prompt_noncached=prompt_tokens - cached_tokens,
        cache_creation_input_tokens=prompt_details.get("cache_write_tokens", 0),
        cache_read_input_tokens=cached_tokens,
        output_tokens=usage.get("completion_tokens", 0),
        including_reasoning_tokens=reasoning_tokens,
    )


def provider_cost_from_usage(usage: dict):
    cost = usage.get("cost")
    if cost is None:
        return None
    return float(cost)


_TOOL_ARGS_PREVIEW_MAX = 300


def _tool_args_preview(args_raw: str) -> str:
    if len(args_raw) <= _TOOL_ARGS_PREVIEW_MAX:
        return args_raw
    return args_raw[:_TOOL_ARGS_PREVIEW_MAX] + "..."


def _malformed_tool_args_diagnostic(tool_name: str) -> str:
    label = (tool_name or "").strip() or "unknown_tool"
    return "Tool call arguments were malformed JSON for %s; retry with valid JSON arguments." % label


def _parse_tool_call_arguments(
    call_id: str,
    tool_name: str,
    args_raw: str | dict,
) -> tuple[dict | None, str | None, str | None]:
    if isinstance(args_raw, dict):
        return args_raw, None, None
    args_str = str(args_raw) if args_raw is not None else ""
    if not args_str.strip():
        return {}, None, None
    try:
        arguments = json.loads(args_str)
    except json.JSONDecodeError as exc:
        preview = _tool_args_preview(args_str)
        diagnostic = _malformed_tool_args_diagnostic(tool_name)
        log_line = "malformed tool-call arguments: tool=%r call_id=%r error=%s preview=%r" % (tool_name, call_id, exc, preview)
        return None, diagnostic, log_line
    return arguments, None, None


def _raw_arguments_string(args_raw: str | dict | None) -> str:
    if isinstance(args_raw, dict):
        return json.dumps(args_raw, ensure_ascii=False)
    if args_raw is None:
        return ""
    return str(args_raw)


def parse_tool_calls_or_text(
    entries: list[tuple[str, str, str | dict]],
) -> tuple[list[HalluToolCall], list[HalluInvalidToolCall], str | None, list[str]]:
    tool_calls: list[HalluToolCall] = []
    invalid_tool_calls: list[HalluInvalidToolCall] = []
    parse_logs: list[str] = []
    # Avoid passing a partial tool-call set when any arguments are malformed.
    for call_id, name, args_raw in entries:
        arguments, diagnostic, log_line = _parse_tool_call_arguments(
            call_id,
            name,
            args_raw,
        )
        if diagnostic:
            if log_line:
                parse_logs.append(log_line)
            invalid_tool_calls.append(
                HalluInvalidToolCall(
                    call_id=call_id,
                    name=name,
                    raw_arguments=_raw_arguments_string(args_raw),
                    error=diagnostic,
                )
            )
            continue
        tool_calls.append(
            HalluToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
            )
        )
    if invalid_tool_calls:
        combined_diagnostic = "\n".join(tc.error for tc in invalid_tool_calls)
        return [], invalid_tool_calls, combined_diagnostic, parse_logs
    return tool_calls, [], None, parse_logs


def tool_calls_from_message(message: dict) -> tuple[list[HalluToolCall], list[HalluInvalidToolCall], str | None, list[str]]:
    entries: list[tuple[str, str, str | dict]] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        entries.append(
            (
                tc.get("id", ""),
                fn.get("name", ""),
                fn.get("arguments", "{}"),
            )
        )
    return parse_tool_calls_or_text(entries)


def tool_calls_from_streaming_slots(
    pending_tool_calls: dict[int, dict],
) -> tuple[list[HalluToolCall], list[HalluInvalidToolCall], str | None, list[str]]:
    entries: list[tuple[str, str, str | dict]] = []
    for idx in sorted(pending_tool_calls.keys()):
        slot = pending_tool_calls[idx]
        args_str = "".join(slot.get("arguments") or [])
        entries.append(
            (
                slot.get("id", ""),
                slot.get("name", ""),
                args_str,
            )
        )
    return parse_tool_calls_or_text(entries)


def merge_tool_parse_diagnostic(raw_text: str, diagnostic: str | None) -> str:
    if not diagnostic:
        return raw_text
    if raw_text.strip():
        return raw_text + "\n\n" + diagnostic
    return diagnostic


def reasoning_from_message(message: dict) -> tuple[str, object | None]:
    thinking_parts = []
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        thinking_parts.append(reasoning)
    details = message.get("reasoning_details")
    if details is None:
        return "\n".join(thinking_parts), None
    return "\n".join(thinking_parts), details


def compute_parsed(raw_text: str, tool_calls: list, req: HalluStructuredRequest):
    if tool_calls:
        return None
    if not raw_text.strip():
        return None
    if req.output_schema is None:
        return raw_text
    return json.loads(raw_text)


def build_result_from_parts(
    req: HalluStructuredRequest,
    raw_text: str,
    tool_calls: list[HalluToolCall],
    thinking_text: str,
    provider_specific_stuff,
    usage: dict,
    actual_model: str,
    stop_reason: str,
    response_id: str,
    sse_log: list | None = None,
    tool_arguments_parse_failed: bool = False,
    invalid_tool_calls: list[HalluInvalidToolCall] | None = None,
) -> HalluStructuredResult:
    invalid_tool_calls = list(invalid_tool_calls or [])
    has_invalid_tool_calls = bool(invalid_tool_calls)
    if not raw_text.strip() and not tool_calls and not has_invalid_tool_calls and not req.allow_empty_llm_output:
        raise RuntimeError("openrouter: no text output and no tool calls, max_tokens=%d, model=%r" % (req.max_tokens, req.provm_name))
    if tool_arguments_parse_failed or has_invalid_tool_calls:
        parsed = None
    else:
        parsed = compute_parsed(raw_text, tool_calls, req)
    return HalluStructuredResult(
        parsed=parsed,
        raw_text=raw_text,
        thinking_text=thinking_text,
        provider_specific_stuff=provider_specific_stuff,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls,
        usage=map_usage(usage),
        provider_cost_usd=provider_cost_from_usage(usage),
        actual_model=actual_model or req.provm_name,
        stop_reason=stop_reason or "",
        response_id=response_id or "",
        provider_usage_json=usage,
        sse_log=sse_log or [],
    )


def post_openrouter(req: HalluStructuredRequest, body: dict, stream: bool):
    if not req.prov_endpoint:
        raise ValueError(f"prov_endpoint is empty for model {req.provm_name!r}; it must come from the provider row in the DB")
    endpoint = req.prov_endpoint.rstrip("/") + "/chat/completions"
    dump_req_body(req, body)
    return endpoint, body
