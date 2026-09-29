import pytest

from examples.meet_addressing import MeetAddressing, addressee_prompt, mentions_name, parse_addressee


class ScriptedJudge:
    def __init__(self, vreply: str) -> None:
        self.vreply = vreply
        self.vprompts: list[str] = []

    async def __call__(self, vprompt: str) -> str:
        self.vprompts.append(vprompt)
        return self.vreply


def test_misheard_name_still_engages():
    assert mentions_name("hey jarvys what's the weather", "Jarvis")
    assert not mentions_name("we should check the invoice", "Jarvis")
    assert mentions_name("Caren, can you check the deadline?", "Karen")
    assert not mentions_name("we can check the deadline", "Karen")


def test_name_glued_into_one_word_still_engages():
    assert mentions_name("VoiceAgent, what time is it in Tokyo right now?", "Voice Agent")
    assert not mentions_name("my voice is gone today", "Voice Agent")


def test_cyrillic_name_engages():
    assert mentions_name("Карен, который час в Токио?", "Karen")
    assert mentions_name("Карин, проверь дедлайн", "Karen")
    assert not mentions_name("Как дела?", "Karen")


@pytest.mark.asyncio
async def test_the_name_needs_no_judge():
    vjudge = ScriptedJudge('{"to_assistant": false}')
    vaddressing = MeetAddressing("Karen", vjudge)
    assert await vaddressing.is_addressed("Anna", "Карен, который час?", "")
    assert vjudge.vprompts == []
    assert vaddressing.vengaged_speaker == "Anna"


@pytest.mark.asyncio
async def test_without_the_name_the_dialogue_decides():
    vjudge = ScriptedJudge('```json\n{"to_assistant": true}\n```')
    vaddressing = MeetAddressing("Karen", vjudge)
    vtranscript = "[Kirill] Karen, расскажи факт\n[Karen] Бананы слегка радиоактивны."
    assert await vaddressing.is_addressed("Kirill", "Почему?", vtranscript)
    assert vtranscript in vjudge.vprompts[0]
    assert "Latest line, from Kirill: Почему?" in vjudge.vprompts[0]


@pytest.mark.asyncio
async def test_a_no_leaves_karen_silent_and_unengaged():
    vaddressing = MeetAddressing("Karen", ScriptedJudge('{"to_assistant": false}'))
    assert not await vaddressing.is_addressed("Anna", "Carl, can you review it?", "")
    assert vaddressing.vengaged_speaker == ""


def test_anything_but_an_explicit_yes_is_a_no():
    assert parse_addressee('{"to_assistant": true}')
    assert not parse_addressee('{"to_assistant": false}')
    assert not parse_addressee("I think it probably is")
    assert not parse_addressee("")


def test_prompt_names_the_bot_and_marks_an_empty_meeting():
    vprompt = addressee_prompt("Karen", "", "Anna", "Как дела?")
    assert "said to Karen" in vprompt
    assert "(nothing yet)" in vprompt
