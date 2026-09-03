from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage

from voice_agent.pipeline import voice_contracts, voice_speech_segments


@dataclass
class VoiceMessage:
    type: str
    content: object
    tool_calls: list[object] = field(default_factory=list)
    invalid_tool_calls: list[object] = field(default_factory=list)
    additional_kwargs: dict[str, object] = field(default_factory=dict)


def test_voice_speech_segments_returns_final_post_human_response() -> None:
    messages = [
        HumanMessage(content="Question"),
        AIMessage(content="Old reply."),
        AIMessage(content="Final reply for the caller."),
    ]

    assert voice_speech_segments.voice_speech_segments(messages) == ("Final reply for the caller.",)


def test_voice_speech_segments_rejects_tool_payloads() -> None:
    human = VoiceMessage("human", "Question")
    tool = VoiceMessage("ai", "I will call it.", tool_calls=[{"args": {"secret": "value"}}])
    invalid = VoiceMessage("ai", "I will call it.", invalid_tool_calls=[{"args": "value"}])
    function = VoiceMessage("ai", "I will call it.", additional_kwargs={"function_call": {"arguments": "value"}})

    assert voice_speech_segments.voice_speech_segments([human, tool]) == ()
    assert voice_speech_segments.voice_speech_segments([human, invalid]) == ()
    assert voice_speech_segments.voice_speech_segments([human, function]) == ()


def test_voice_speech_segments_strips_reasoning_and_rejects_non_text_blocks() -> None:
    human = VoiceMessage("human", "Question")
    safe = VoiceMessage(
        "ai",
        [
            {"type": "reasoning", "text": "Never say this."},
            {"type": "thinking", "text": "Nor this."},
            {"type": "text", "text": "This is safe to say."},
        ],
    )
    unsafe = VoiceMessage("ai", [{"type": "text", "text": "Visible."}, {"type": "image", "url": "x"}])

    assert voice_speech_segments.voice_speech_segments([human, safe]) == ("This is safe to say.",)
    assert voice_speech_segments.voice_speech_segments([human, unsafe]) == ()


def test_voice_speech_segments_removes_non_speech_markup() -> None:
    message = VoiceMessage(
        "ai",
        "# **Hello** [caller](https://example.test/path). ![secret](https://image.test/x) Visit https://unsafe.test. `<hidden>` ```never speak this``` <think>private chain</think> <b>Now</b>.",
    )

    segments = voice_speech_segments.voice_speech_segments([VoiceMessage("human", "Question"), message])

    assert " ".join(segments) == "Hello caller. Visit Now."


def test_voice_speech_segments_splits_unpunctuated_text_within_limit() -> None:
    text = "a" * (voice_contracts.VOICE_SEGMENT_MAX_CHARS * 2 + 13)
    messages = [VoiceMessage("human", "Question"), VoiceMessage("ai", text)]

    segments = voice_speech_segments.voice_speech_segments(messages)

    assert "".join(segments) == text
    assert all(len(segment) <= voice_contracts.VOICE_SEGMENT_MAX_CHARS for segment in segments)
    assert len(segments) == 3


def test_voice_speech_segments_prefers_punctuation_boundaries() -> None:
    first = "First sentence has enough words to make it comfortably longer than the minimum boundary."
    second = "Second sentence also has enough words to remain a separate spoken segment for the caller."
    messages = [VoiceMessage("human", "Question"), VoiceMessage("ai", first + " " + second)]

    assert voice_speech_segments.voice_speech_segments(messages) == (first, second)


def test_voice_speech_segments_requires_post_human_assistant_response() -> None:
    old = VoiceMessage("ai", "Old answer.")
    human = VoiceMessage("human", "New question")

    assert voice_speech_segments.voice_speech_segments([]) == ()
    assert voice_speech_segments.voice_speech_segments([old]) == ()
    assert voice_speech_segments.voice_speech_segments([old, human]) == ()


def test_a_long_comma_only_reply_splits_on_clause_boundaries() -> None:
    vclause = "и мы можем начать прямо сейчас, "
    vtext = "Всё хорошо, " + vclause * 8 + "если хотите."
    vmessages = [VoiceMessage("human", "Question"), VoiceMessage("ai", vtext)]

    vsegments = voice_speech_segments.voice_speech_segments(vmessages)

    assert len(vsegments) > 1
    assert all(len(vsegment) <= voice_contracts.VOICE_SEGMENT_MAX_CHARS for vsegment in vsegments)
    assert all(vsegment.endswith(",") for vsegment in vsegments[:-1])


def test_no_segment_is_shorter_than_the_spoken_word_minimum() -> None:
    vtext = "Да, нет, конечно, хорошо, я здесь и готов помочь вам прямо сейчас."
    vmessages = [VoiceMessage("human", "Question"), VoiceMessage("ai", vtext)]

    vsegments = voice_speech_segments.voice_speech_segments(vmessages)

    assert vsegments
    assert all(len(vsegment.split()) >= voice_contracts.VOICE_SEGMENT_MIN_WORDS for vsegment in vsegments)


def test_stream_segmenter_releases_clauses_once_they_reach_the_word_minimum() -> None:
    vstream = voice_speech_segments.VoiceSpeechStreamSegmenter()

    assert vstream.push("Всё хорошо, ") == ()
    assert vstream.push("спасибо! ") == ()
    vreleased = vstream.push("Я здесь и готов помочь, ")

    assert vreleased
    assert all(len(vsegment.split()) >= voice_contracts.VOICE_SEGMENT_MIN_WORDS for vsegment in vreleased)
    assert "".join(vreleased).startswith("Всё хорошо,")


def test_stream_segmenter_never_releases_an_unterminated_tail_before_flush() -> None:
    vstream = voice_speech_segments.VoiceSpeechStreamSegmenter()

    assert vstream.push("Я здесь и готов помочь вам прямо сейчас") == ()
    assert vstream.flush() == ("Я здесь и готов помочь вам прямо сейчас",)


def test_stream_segmenter_withholds_an_unclosed_code_fence() -> None:
    vstream = voice_speech_segments.VoiceSpeechStreamSegmenter()

    assert vstream.push("Вот пример кода: ```python\nx = 1. ") == ()

    vreleased = vstream.push("``` Этого достаточно для запуска. ")

    assert vreleased
    assert "x = 1" not in "".join(vreleased)
