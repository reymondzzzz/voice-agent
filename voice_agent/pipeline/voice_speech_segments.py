from __future__ import annotations

import re
from collections.abc import Sequence

from voice_agent.pipeline import voice_contracts


_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_HIDDEN_REASONING_RE = re.compile(r"<(?:analysis|reasoning|think|thinking)\b[^>]*>.*?</(?:analysis|reasoning|think|thinking)\s*>", re.IGNORECASE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]*>")
_MARKDOWN_RE = re.compile(r"(?m)^\s{0,3}(?:#{1,6}|[-*+]\s+|\d+[.)]\s+|>\s+)|[*_~]+")
_SPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?])")
_SENTENCE_RE = re.compile(r"(?<=[.!?;:,])\s+")


def voice_speech_segments(vmessages: Sequence[object]) -> tuple[str, ...]:
    message = _last_post_human_ai_message(vmessages)
    if message is None or _has_tool_payload(message):
        return ()
    text = _safe_message_text(message)
    if not text:
        return ()
    return _segment_text(text)


def _last_post_human_ai_message(vmessages: Sequence[object]) -> object | None:
    human_index = -1
    for index, message in enumerate(vmessages):
        if _message_kind(message) in {"human", "user"}:
            human_index = index
    if human_index < 0:
        return None
    for message in reversed(vmessages[human_index + 1 :]):
        if _message_kind(message) == "ai":
            return message
    return None


def _message_kind(vmessage: object) -> str:
    kind = getattr(vmessage, "type", "")
    if type(kind) is str:
        return kind.lower()
    return ""


def _has_tool_payload(vmessage: object) -> bool:
    if getattr(vmessage, "tool_calls", None) or getattr(vmessage, "invalid_tool_calls", None):
        return True
    additional = getattr(vmessage, "additional_kwargs", {})
    if type(additional) is not dict:
        return True
    return bool(additional.get("tool_calls") or additional.get("function_call"))


def _safe_message_text(vmessage: object) -> str:
    content = getattr(vmessage, "content", None)
    if type(content) is str:
        return _clean_text(content)
    if type(content) is not list:
        return ""
    texts = []
    for block in content:
        if type(block) is not dict:
            return ""
        block_type = block.get("type")
        if block_type in {"reasoning", "thinking"}:
            continue
        if block_type != "text" or type(block.get("text")) is not str:
            return ""
        texts.append(block["text"])
    return _clean_text(" ".join(texts))


def _clean_text(vtext: str) -> str:
    text = _HIDDEN_REASONING_RE.sub(" ", vtext)
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _IMAGE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    text = _HTML_RE.sub(" ", text)
    text = _MARKDOWN_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", text)
    return text.strip(" -–—*_~`#>[](){}")


def _segment_text(vtext: str) -> tuple[str, ...]:
    pieces = []
    for sentence in _SENTENCE_RE.split(vtext):
        pieces.extend(_split_overlong(sentence))
    return _merge_short_segments([piece for piece in pieces if piece])


def _merge_short_segments(vsegments: list[str]) -> tuple[str, ...]:
    maximum = voice_contracts.VOICE_SEGMENT_MAX_CHARS
    segments: list[str] = []
    for segment in vsegments:
        if segments and _is_below_speech_minimum(segment):
            candidate = segments[-1] + " " + segment
            if len(candidate) <= maximum:
                segments[-1] = candidate
                continue
        segments.append(segment)
    if len(segments) > 1 and _is_below_speech_minimum(segments[0]):
        candidate = segments[0] + " " + segments[1]
        if len(candidate) <= maximum:
            segments[0:2] = [candidate]
    return tuple(segments)


def _is_below_speech_minimum(vsegment: str) -> bool:
    return len(vsegment.split()) < voice_contracts.VOICE_SEGMENT_MIN_WORDS


def _split_overlong(vtext: str) -> tuple[str, ...]:
    text = vtext.strip()
    maximum = voice_contracts.VOICE_SEGMENT_MAX_CHARS
    if len(text) <= maximum:
        return (text,) if text else ()
    parts: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else current + " " + word
        if len(candidate) <= maximum:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = word
            continue
        parts.extend(word[index : index + maximum] for index in range(0, len(word), maximum))
        current = ""
    if current:
        parts.append(current)
    return tuple(parts)


class VoiceSpeechStreamSegmenter:
    def __init__(self) -> None:
        self._vbuffer = ""

    def push(self, vdelta: str) -> tuple[str, ...]:
        if type(vdelta) is not str or not vdelta:
            return ()
        self._vbuffer += vdelta
        vboundary = _last_sentence_boundary(self._vbuffer)
        if vboundary <= 0:
            return ()
        vprefix = self._vbuffer[:vboundary]
        if not _is_safe_to_release(vprefix):
            return ()
        vtext = _clean_text(vprefix)
        if not vtext or _is_below_speech_minimum(vtext):
            return ()
        self._vbuffer = self._vbuffer[vboundary:]
        return _segment_text(vtext)

    def flush(self) -> tuple[str, ...]:
        vtail = self._vbuffer
        self._vbuffer = ""
        vtext = _clean_text(vtail)
        return _segment_text(vtext) if vtext else ()


def _last_sentence_boundary(vtext: str) -> int:
    vboundary = 0
    for vmatch in _SENTENCE_RE.finditer(vtext):
        vboundary = vmatch.end()
    return vboundary


def _is_safe_to_release(vtext: str) -> bool:
    if vtext.count("```") % 2:
        return False
    return vtext.count("[") == vtext.count("]")
