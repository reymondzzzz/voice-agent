import pytest

from examples.meet_addressing import calls_name, MeetAddressing, addressee_prompt, mentions_name, parse_addressee
from examples.meet_agent import MEET_DEFAULT_BOT_NAME


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
    vjudge = ScriptedJudge("IGNORE")
    vaddressing = MeetAddressing("Karen", vjudge)
    assert await vaddressing.is_addressed("Anna", "Карен, который час?", "")
    assert vjudge.vprompts == []
    assert vaddressing.vengaged_speaker == "Anna"


@pytest.mark.asyncio
async def test_without_the_name_the_dialogue_decides():
    vjudge = ScriptedJudge("RESPOND.")
    vaddressing = MeetAddressing("Karen", vjudge)
    vtranscript = "[Kirill] Karen, расскажи факт\n[Karen] Бананы слегка радиоактивны."
    assert await vaddressing.is_addressed("Kirill", "Почему?", vtranscript)
    assert vtranscript in vjudge.vprompts[0]
    assert "Latest line, from Kirill: Почему?" in vjudge.vprompts[0]


@pytest.mark.asyncio
async def test_a_no_leaves_karen_silent_and_unengaged():
    vaddressing = MeetAddressing("Karen", ScriptedJudge("IGNORE"))
    assert not await vaddressing.is_addressed("Anna", "Carl, can you review it?", "")
    assert vaddressing.vengaged_speaker == ""


def test_anything_but_an_explicit_yes_is_a_no():
    assert parse_addressee("RESPOND")
    assert parse_addressee("respond.")
    assert not parse_addressee("IGNORE")
    assert not parse_addressee("RESPOND or IGNORE")
    assert not parse_addressee("RESPONDING later")
    assert not parse_addressee("I think it probably is")
    assert not parse_addressee("")


def test_prompt_names_the_bot_and_marks_an_empty_meeting():
    vprompt = addressee_prompt("Karen", "", "Anna", "Как дела?")
    assert "said to Karen" in vprompt
    assert "(nothing yet)" in vprompt


def test_the_meet_name_survives_a_clipped_start_and_no_colleague_answers_to_it():
    for vheard in ("Мэгги, какая погода?", "Меги, который час?", "Эгги, как дела?", "Мегги!"):
        assert mentions_name(vheard, MEET_DEFAULT_BOT_NAME), vheard
    for vcolleague in ("Маша", "Мия", "Мэри", "Миша", "Егор", "Артем", "беги", "деньги", "мешки", "многие"):
        assert not mentions_name(f"{vcolleague}, привет", MEET_DEFAULT_BOT_NAME), vcolleague


def test_a_longer_word_is_never_the_name():
    for vword in ("руки", "брюки", "юбки", "штуки", "звуки"):
        assert not mentions_name(f"{vword}, привет", "Юки"), vword


def test_her_name_said_about_her_to_someone_else_is_not_a_call():
    assert calls_name("Мэгги, найди документы", "Мэгги")
    assert calls_name("а найди документы про вебхуки, Мэгги?", "Мэгги")
    assert not calls_name("Помнишь, значит, Мэгги там находила разную документацию, давай это обсудим", "Мэгги"), "she answered a line said to Михаил about her"
