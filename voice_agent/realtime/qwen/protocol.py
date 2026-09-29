"""Wire protocol for Qwen Omni Realtime over DashScope.

Stdlib only. The shape follows the OpenAI realtime protocol closely enough that the event names are
familiar, but the rates are not: audio goes **in at 16 kHz** and comes **out at 24 kHz**, so the two
directions are not symmetric and one of them always needs resampling.
"""

from __future__ import annotations

DEFAULT_MODEL = "qwen3.5-omni-plus-realtime"
SINGAPORE_WS_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
BEIJING_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

INPUT_SAMPLE_RATE_HZ = 16000
OUTPUT_SAMPLE_RATE_HZ = 24000
DEFAULT_VOICE = "Tina"
DEFAULT_INSTRUCTIONS = "You are a warm, concise voice assistant. Keep answers short and natural."

SESSION_UPDATE = "session.update"
INPUT_AUDIO_APPEND = "input_audio_buffer.append"
INPUT_AUDIO_COMMIT = "input_audio_buffer.commit"
CONVERSATION_ITEM_CREATE = "conversation.item.create"
RESPONSE_CREATE = "response.create"
RESPONSE_CANCEL = "response.cancel"

SESSION_CREATED = "session.created"
SESSION_UPDATED = "session.updated"
SPEECH_STARTED = "input_audio_buffer.speech_started"
SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
INPUT_TRANSCRIPTION_COMPLETED = "conversation.item.input_audio_transcription.completed"
RESPONSE_AUDIO_DELTA = "response.audio.delta"
RESPONSE_AUDIO_DONE = "response.audio.done"
RESPONSE_AUDIO_TRANSCRIPT_DELTA = "response.audio_transcript.delta"
RESPONSE_AUDIO_TRANSCRIPT_DONE = "response.audio_transcript.done"
RESPONSE_TEXT_DELTA = "response.text.delta"
FUNCTION_CALL_ARGUMENTS_DONE = "response.function_call_arguments.done"
RESPONSE_DONE = "response.done"
ERROR = "error"


def connection_url(vbase_url: str, vmodel: str) -> str:
    return f"{vbase_url}?model={vmodel}"


# Read off a live session.created rather than the docs, which describe a semantic_vad the endpoint
# does not actually default to and an ASR model it does not name.
DEFAULT_TRANSCRIPTION_MODEL = "qwen3-asr-flash-realtime"


def session_update_frame(
    vvoice: str,
    vinstructions: str,
    vtools: list[dict[str, object]],
    *,
    vsilence_ms: int = 800,
    vauto_response: bool = True,
) -> dict[str, object]:
    return {
        "type": SESSION_UPDATE,
        "session": {
            "modalities": ["text", "audio"],
            "voice": vvoice,
            "instructions": vinstructions,
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "input_audio_transcription": {"model": DEFAULT_TRANSCRIPTION_MODEL},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": vsilence_ms,
                # The endpoint stops the model itself on barge-in, so interruption is its job and
                # `interrupt()` only has to drop audio already queued for playback.
                "create_response": vauto_response,
                "interrupt_response": True,
            },
            "tools": vtools,
        },
    }


def function_output_frame(vcall_id: str, voutput: str) -> dict[str, object]:
    return {
        "type": CONVERSATION_ITEM_CREATE,
        "item": {"type": "function_call_output", "call_id": vcall_id, "output": voutput},
    }


def message_frame(vrole: str, vtext: str) -> dict[str, object]:
    return {
        "type": CONVERSATION_ITEM_CREATE,
        "item": {"type": "message", "role": vrole, "content": [{"type": "input_text", "text": vtext}]},
    }
