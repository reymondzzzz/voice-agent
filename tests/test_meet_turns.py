import asyncio
import collections
import time

import pytest

from examples.meet_addressing import MeetAddressing
from examples import meet_agent
from examples.meet_agent import DELIVERY_ATTEMPTS, MeetCall, PendingResult, RoomAudioSink, delivery_line, heard_part, is_spoken_verdict, parse_yes
from examples.meet_memory import MeetMemory, MeetTurn
from voice_agent.agent.tasks.models import TaskStatus
from voice_agent.correlation import Correlation
from voice_agent.realtime import events

CORRELATION = Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)


class Session:
    def __init__(self) -> None:
        self.vresponses = 0
        self.vresults: list[bool] = []
        self.vinterrupts = 0
        self.vcontext: list[str] = []

    def correlation(self):
        return CORRELATION

    async def request_response(self, _vrequest) -> None:
        self.vresponses += 1

    async def send_tool_result(self, _vresult, *, vrespond: bool = True) -> None:
        self.vresults.append(vrespond)

    async def interrupt(self, _vrequest) -> None:
        self.vinterrupts += 1

    async def add_context(self, vupdate) -> None:
        self.vcontext.append(vupdate.vtext)


class Registry:
    def __init__(self, vrecords: list) -> None:
        self.vrecords = vrecords

    def all(self) -> list:
        return self.vrecords


class Sink:
    vmuted = False
    vfirst_audio_at = None
    vqueued_at_first_audio = 0.0
    vtimeline_s = 0.0

    async def clear(self) -> None:
        pass

    async def cut(self) -> float:
        return 0.0


def bare_call() -> MeetCall:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vsession, vcall.vsink = Session(), Sink()
    vcall.vreply_parts, vcall.vroute_parts, vcall.vrouting = [], [], None
    vcall.vneeds_followup, vcall.vturn_tools, vcall.vturn_spoke, vcall.vverdict_leaked, vcall.vdiscard_next = False, 0, False, False, False
    vcall.vturn_done, vcall.vfloor = asyncio.Event(), asyncio.Lock()
    vcall.vuser_speaking, vcall.vlast_bot_activity, vcall.vlast_response_at = False, 0.0, 0.0
    vcall.vtrace = {}
    vcall.vturn_results, vcall.vpending, vcall.vpending_added = [], collections.deque(), asyncio.Event()
    vcall.vreply_seq, vcall.vopen_replies, vcall.vplayed_replies, vcall.vcut_replies, vcall.vplayout = 0, set(), set(), set(), asyncio.Condition()
    vcall.vspawned = []
    vcall.spawn = lambda vcoro: vcall.vspawned.append(vcoro) or vcoro.close()
    vcall.requester = lambda: "Kirill"
    vcall.vanswering, vcall.vanswering_heard_at, vcall.vcontinued, vcall.vdropping = None, 0.0, None, False
    vcall.current_speaker = lambda: "Kirill"
    vcall.vturn_reply = ""
    vcall.vowned, vcall.vawaiting, vcall.vresults_followup, vcall.vheard_speakers = "", False, False, {}
    vcall.vaddressing = MeetAddressing("Мэгги", lambda _vprompt: asyncio.sleep(0, "IGNORE"))
    vcall.vreply_audio, vcall.vheard_by_reply = {}, {}
    vcall.vtool_calls, vcall.vlatest_call = 0, {}
    vcall.vlast_bot_played, vcall.vlast_human_speech, vcall.vlast_bot_reply_done = 0.0, 0.0, 0.0
    vcall.vpublished = []
    vcall.publish = lambda **vevent: vcall.vpublished.append(vevent)
    vcall.vregistry = Registry([])
    vcall.remember = lambda *_vargs, **_vkwargs: None
    vcall.vstarted = []
    vcall.start_background = lambda vtool, _vargs: vcall.vstarted.append(vtool.vname) or "Started in the background; the answer arrives later."
    return vcall


def tool_call(vname: str, varguments: dict, vid: str) -> events.RealtimeToolCallRequested:
    return events.RealtimeToolCallRequested(vcorrelation=CORRELATION, vtool_call_id=vid, vtool_name=vname, varguments=varguments)


def test_parsers():
    assert is_spoken_verdict("RESPOND") and is_spoken_verdict("Respond.") and is_spoken_verdict("IGN")
    assert not is_spoken_verdict("Кирилл, в Лондоне сейчас дождь.") and not is_spoken_verdict("Re")
    assert parse_yes("YES") and parse_yes("yes.") and not parse_yes("NO") and not parse_yes("")


@pytest.mark.asyncio
async def test_two_tools_in_one_response_get_one_follow_up_not_two():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "London"}, "a"))
    await vcall.on_tool_call(tool_call("science_fact", {}, "b"))
    assert vcall.vsession.vresults == [False, False] and vcall.vsession.vresponses == 0
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and vcall.vfloor.locked()
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and not vcall.vfloor.locked() and vcall.vturn_done.is_set()


@pytest.mark.asyncio
async def test_a_spoken_verdict_is_muted_and_the_answer_asked_for_again():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="RESPOND"))
    assert vcall.vsink.vmuted and vcall.vsession.vinterrupts == 0
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert not vcall.vsink.vmuted and vcall.vsession.vresponses == 1 and vcall.vfloor.locked()


@pytest.mark.asyncio
async def test_a_spoken_verdict_does_not_cancel_the_tool_call_after_it():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="RESPOND"))
    await vcall.on_tool_call(tool_call("science_fact", {}, "a"))
    assert vcall.vsession.vinterrupts == 0 and vcall.vstarted == ["science_fact"]
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and vcall.vfloor.locked()
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and vcall.vturn_done.is_set()


@pytest.mark.asyncio
async def test_a_request_cut_off_before_any_answer_is_asked_again_once():
    vcall = bare_call()
    vcall.vaddressing = MeetAddressing("Karen", lambda _vprompt: asyncio.sleep(0, "IGNORE"))
    vcall.publish = lambda **_vevent: None
    vspoken: list[str] = []

    async def speak(vline: str) -> None:
        vspoken.append(vline)
        vcall.vturn_done.clear()
        vcall.vturn_spoke = len(vspoken) > 1
        vcall.vturn_tools = 0
        vcall.end_turn()

    async def quiet(_vpause_s: float) -> None:
        pass

    async def no_promise(_vreply: str) -> None:
        pass

    vcall.speak, vcall.wait_until_quiet, vcall.check_promise = speak, quiet, no_promise
    await vcall.consider(MeetTurn(0.0, "Kirill", "Карен, какая погода в Лондоне?"), "", {}, vfollow_up=False)
    assert len(vspoken) == 2
    assert vspoken[1].startswith("[Kirill asked you this a moment ago and you have not answered yet]")


@pytest.mark.asyncio
async def test_a_reply_that_promised_work_without_a_tool_is_corrected():
    vcall = bare_call()
    vspoken: list[str] = []
    vasked: list[str] = []

    async def route(vprompt: str) -> str:
        vasked.append(vprompt)
        return "YES"

    async def speak(vline: str) -> None:
        vspoken.append(vline)

    vcall.route, vcall.speak = route, speak
    await vcall.check_promise("Я уже начала проверку, скоро скажу.")
    assert "«Я уже начала проверку, скоро скажу.»" in vasked[0], "the reply is quoted, or earlier promises count too"
    assert vspoken and vspoken[0].startswith("[internal] Your last reply promised")


@pytest.mark.asyncio
async def test_a_tool_result_talked_over_before_it_was_said_is_queued_not_lost():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_tool_call(tool_call("get_current_time", {}, "a"))
    vcall.vuser_speaking = True
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 0 and not vcall.vfloor.locked()
    assert [vresult.vgoal for vresult in vcall.vpending] == ["get_current_time()"] and vcall.vpending_added.is_set()


@pytest.mark.asyncio
async def test_a_waiting_result_rides_along_with_the_tool_answer():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    vcall.vpending.append(PendingResult("Kirill", "science fact", "Octopuses have three hearts."))
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "London"}, "a"))
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and not vcall.vpending
    assert "Octopuses have three hearts." in vcall.vsession.vcontext[0]
    assert "requeue_if_talked_over" in [vcoro.__name__ for vcoro in vcall.vspawned]


def follow_up_call(vspeech_after_reply_s: float) -> MeetCall:
    vcall = bare_call()
    vcall.vaddressing = MeetAddressing("Мэгги", lambda _vprompt: asyncio.sleep(0, "IGNORE"))
    vcall.vaddressing.engage("Kirill Starkov")
    vcall.vlast_bot_played = 100.0
    vcall.vspeech_started_at = 100.0 + vspeech_after_reply_s
    vcall.vmemory = MeetMemory()
    vcall.vmemory.add(MeetTurn(1.0, "Kirill Starkov", "Мэгги, какая погода в Лондоне?"))
    vcall.vmemory.add(MeetTurn(2.0, "Anna Petrova", "А мне интересно про Токио."))
    return vcall


def test_the_person_she_just_answered_needs_no_routing_step():
    assert follow_up_call(2.0).is_follow_up("Kirill Starkov", "А в Париже?")


def test_turning_to_a_colleague_or_waiting_long_goes_through_routing():
    assert not follow_up_call(2.0).is_follow_up("Kirill Starkov", "Анна, а ты что думаешь?")
    assert not follow_up_call(2.0).is_follow_up("Anna Petrova", "А в Париже?")
    assert not follow_up_call(20.0).is_follow_up("Kirill Starkov", "А в Париже?")





def answering_call(vheard_ago_s: float) -> MeetCall:
    vcall = bare_call()
    vcall.vanswering = MeetTurn(0.0, "Kirill", "Мэгги, найди новый факт")
    vcall.vanswering_heard_at = time.monotonic() - vheard_ago_s
    return vcall


@pytest.mark.asyncio
async def test_the_same_person_going_on_drops_the_answer_to_the_first_half():
    vcall = answering_call(1.0)
    await vcall.vfloor.acquire()
    assert vcall.is_continuation()
    await vcall.drop_answer_for_continuation()
    assert vcall.vsession.vinterrupts == 1 and vcall.vsink.vmuted and "hold_for_rest" in [vcoro.__name__ for vcoro in vcall.vspawned]
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert not vcall.vsink.vmuted and vcall.vturn_done.is_set() and not vcall.vfloor.locked()


def test_a_tool_already_running_another_speaker_or_a_late_start_is_not_a_continuation():
    vtool_ran = answering_call(1.0)
    vtool_ran.vturn_tools = 1
    vsomeone_else = answering_call(1.0)
    vsomeone_else.current_speaker = lambda: "Anna"
    assert not vtool_ran.is_continuation() and not vsomeone_else.is_continuation() and not answering_call(5.0).is_continuation()


@pytest.mark.asyncio
async def test_the_rest_is_answered_together_with_the_first_half():
    vcall = bare_call()
    vcall.vcontinued = MeetTurn(0.0, "Kirill", "Мэгги, найди новый факт")
    vcall.vmemory, vcall.vlast_human_speech = MeetMemory(), 0.0
    vasked = []

    async def consider(vturn, _vcontext, _vtrace, *, vfollow_up):
        vasked.append((vturn.vtext, vfollow_up))

    vcall.consider = consider
    vcall.spawn = asyncio.ensure_future
    await vcall.on_heard("и покажи какая погода", "Kirill")
    await asyncio.sleep(0)
    assert vasked == [("Мэгги, найди новый факт и покажи какая погода", True)] and vcall.vcontinued is None
    vmerged = vcall.vpublished[-1]
    assert (vmerged["type"], vmerged["into"], vmerged["text"]) == ("merged", 0.0, "Мэгги, найди новый факт и покажи какая погода")


def answering_once(vcall: MeetCall, vspoken: list[str], vpromises: list[str]) -> None:
    vcall.vaddressing = MeetAddressing("Мэгги", lambda _vprompt: asyncio.sleep(0, "IGNORE"))
    vcall.publish = lambda **_vevent: None

    async def speak(vline: str) -> None:
        vspoken.append(vline)
        vcall.vturn_done.clear()
        vcall.vturn_spoke, vcall.vturn_tools, vcall.vturn_reply = True, 0, "Как только появится, скажу."
        vcall.end_turn()

    async def check_promise(vreply: str) -> None:
        vpromises.append(vreply)

    vcall.speak, vcall.check_promise = speak, check_promise


@pytest.mark.asyncio
async def test_a_result_waiting_when_someone_asks_is_told_in_the_same_answer():
    vcall = bare_call()
    vspoken: list[str] = []
    answering_once(vcall, vspoken, [])
    vcall.vpending.append(PendingResult("Kirill", "science fact", "Octopuses have three hearts."))
    await vcall.consider(MeetTurn(0.0, "Kirill", "Ну что там с фактом?"), "", {}, vfollow_up=True)
    assert len(vspoken) == 1 and "Octopuses have three hearts." in vspoken[0] and not vcall.vpending


@pytest.mark.asyncio
async def test_saying_it_will_come_back_while_work_runs_is_not_a_broken_promise():
    vcall = bare_call()
    vpromises: list[str] = []
    answering_once(vcall, [], vpromises)
    vcall.vregistry = Registry([type("Record", (), {"vstatus": TaskStatus.RUNNING})()])
    await vcall.consider(MeetTurn(0.0, "Kirill", "Ну что там с фактом?"), "", {}, vfollow_up=True)
    assert vpromises == []


@pytest.mark.asyncio
async def test_a_line_from_someone_still_talking_waits_for_the_rest():
    vcall = bare_call()
    vspoken: list[str] = []
    answering_once(vcall, vspoken, [])
    vcall.vuser_speaking = True
    await vcall.consider(MeetTurn(0.0, "Kirill", "Мэгги, расскажи что-нибудь"), "", {}, vfollow_up=True)
    assert vspoken == [] and "hold_for_rest" in [vcoro.__name__ for vcoro in vcall.vspawned]


@pytest.mark.asyncio
async def test_when_the_rest_never_comes_the_first_half_is_answered(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(meet_agent, "CONTINUATION_HOLD_S", 0.0)
    vcall = bare_call()
    vspoken: list[str] = []
    answering_once(vcall, vspoken, [])
    await vcall.hold_for_rest(MeetTurn(0.0, "Kirill", "Мэгги, расскажи что-нибудь"))
    assert len(vspoken) == 1 and "расскажи что-нибудь" in vspoken[0] and vcall.vcontinued is None


def test_a_result_told_while_her_answer_plays_goes_on_as_the_same_answer():
    vfact = PendingResult("Kirill", "science fact", "Octopuses have three hearts.")
    assert "same answer" in delivery_line([vfact], vgoing_on=True)
    assert "same answer" not in delivery_line([vfact], vgoing_on=False)
    vfact.vattempts = 1
    assert "cut off" in delivery_line([vfact], vgoing_on=True), "a talked-over result picks the thread back up"
    assert "same answer" in delivery_line([vfact], vgoing_on=True), "and, right behind her answer, as part of it"


def running_call() -> tuple[MeetCall, list[asyncio.Future]]:
    vcall = bare_call()
    vtasks: list[asyncio.Future] = []
    vcall.spawn = lambda vcoro: vtasks.append(asyncio.ensure_future(vcoro))
    return vcall, vtasks


@pytest.mark.asyncio
async def test_a_quick_tool_result_survives_a_follow_up_cancelled_before_a_word():
    vcall, vtasks = running_call()
    await vcall.vfloor.acquire()
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "London"}, "a"))
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION, vcompleted=False))
    await asyncio.gather(*vtasks)
    assert [vresult.vgoal for vresult in vcall.vpending] == ["get_current_weather(city='London')"]


@pytest.mark.asyncio
async def test_a_quick_tool_answer_talked_over_while_it_plays_is_told_again():
    vcall, vtasks = running_call()
    await vcall.vfloor.acquire()
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "London"}, "a"))
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="В Лондоне 14 градусов."))
    vcall.rearm_after_playout = lambda _vreply: asyncio.sleep(3600)
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert not vcall.vpending, "generated is not heard: still playing"
    vcall.hand_back_results()
    await asyncio.gather(*[vtask for vtask in vtasks if not vtask.done() and vtask.get_coro().__name__ == "requeue_if_talked_over"])
    assert [vresult.vgoal for vresult in vcall.vpending] == ["get_current_weather(city='London')"]
    for vtask in vtasks:
        vtask.cancel()


@pytest.mark.asyncio
async def test_a_stale_completion_does_not_end_the_newer_turn():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    vcall.vowned = "resp_new"
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION, vresponse_id="resp_old"))
    assert vcall.vfloor.locked() and not vcall.vturn_done.is_set()


@pytest.mark.asyncio
async def test_a_request_owns_the_next_response_and_only_that_one():
    vcall = bare_call()
    await vcall.request(events.ResponseRequest(vcorrelation=CORRELATION))
    await vcall.on_event(events.AssistantSpeechStarted(vcorrelation=CORRELATION, vresponse_id="resp_1"))
    await vcall.on_event(events.AssistantSpeechStarted(vcorrelation=CORRELATION, vresponse_id="resp_2"))
    assert vcall.vowned == "resp_1"


@pytest.mark.asyncio
async def test_words_belong_to_whoever_said_them_not_to_whoever_talks_when_the_text_arrives():
    vcall = bare_call()
    vheard: list[tuple[str, str]] = []

    async def on_heard(vtext: str, vspeaker: str) -> None:
        vheard.append((vtext, vspeaker))

    vcall.on_heard = on_heard
    vcall.current_speaker = lambda: "Kirill"
    await vcall.on_event(events.UserSpeechStopped(vcorrelation=CORRELATION, vitem_id="item_lost"))
    await vcall.on_event(events.UserSpeechStopped(vcorrelation=CORRELATION, vitem_id="item_1"))
    vcall.current_speaker = lambda: "Anna"
    await vcall.on_event(events.UserSpeechStopped(vcorrelation=CORRELATION, vitem_id="item_2"))
    await vcall.on_event(events.UserTranscriptFinal(vcorrelation=CORRELATION, vtext="Мэгги, какая погода?", vitem_id="item_1"))
    await vcall.on_event(events.UserTranscriptFinal(vcorrelation=CORRELATION, vtext="А в Париже?", vitem_id="item_2"))
    assert vheard == [("Мэгги, какая погода?", "Kirill"), ("А в Париже?", "Anna")], "a lost transcript shifts nobody"


@pytest.mark.asyncio
async def test_a_routing_step_that_times_out_neither_mutes_the_next_answer_nor_keeps_the_floor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(meet_agent, "ROUTE_TIMEOUT_S", 0.01)
    vcall = bare_call()
    assert await vcall.route("is it for Мэгги?") == ""
    assert vcall.vsession.vinterrupts == 1 and not vcall.vsink.vmuted and not vcall.vfloor.locked()
    await vcall.vfloor.acquire()
    await vcall.request(events.ResponseRequest(vcorrelation=CORRELATION))
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION, vresponse_id="resp_route"))
    await vcall.on_event(events.AssistantSpeechStarted(vcorrelation=CORRELATION, vresponse_id="resp_answer"))
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="Привет!", vresponse_id="resp_answer"))
    vcall.rearm_after_playout = lambda _vreply: asyncio.sleep(0)
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION, vresponse_id="resp_answer"))
    assert vcall.vturn_spoke and vcall.vturn_done.is_set() and not vcall.vfloor.locked()


def test_the_silence_between_two_of_her_replies_is_measured():
    vcall = bare_call()
    vcall.vlast_bot_played, vcall.vlast_human_speech = 10.0, 5.0
    vcall.vsink.vfirst_audio_at = 11.5
    assert vcall.silence_since_her_last_reply() == 1.5
    vcall.vsink.vqueued_at_first_audio = 0.4
    assert vcall.silence_since_her_last_reply() == 0.0, "queued behind the previous reply, no pause at all"
    vcall.vlast_human_speech = 10.5
    assert vcall.silence_since_her_last_reply() is None, "someone spoke in between: not her pause"


@pytest.mark.asyncio
async def test_a_result_already_cut_off_once_is_retold_on_its_own_not_folded_into_another_answer():
    vcall = bare_call()
    vspoken: list[str] = []
    answering_once(vcall, vspoken, [])
    vweather = PendingResult("Kirill", "get_current_weather(city='Tokyo')", "Tokyo: 23 degrees, clear", vattempts=1)
    vfact = PendingResult("Kirill", "science fact", "Octopuses have three hearts.")
    vcall.vpending.extend([vweather, vfact])
    await vcall.consider(MeetTurn(0.0, "Kirill", "и давай новый факт"), "", {}, vfollow_up=True)
    assert "Octopuses have three hearts." in vspoken[0] and "Tokyo" not in vspoken[0]
    assert list(vcall.vpending) == [vweather]


def test_the_heard_part_of_a_reply_follows_the_share_of_its_audio_that_played():
    vreply = "В Токио сейчас 23 градуса и ясное небо."
    assert heard_part(vreply, 0.0, 3.0) == ""
    assert heard_part(vreply, 0.5, 3.0) == "В"
    assert heard_part(vreply, 1.5, 3.0) == "В Токио сейчас 23"
    assert heard_part(vreply, 3.0, 3.0) == vreply


@pytest.mark.asyncio
async def test_a_cut_counts_what_was_still_queued_as_never_heard():
    class Source:
        queued_duration = 0.0

        async def capture_frame(self, _vframe) -> None:
            pass

        def clear_queue(self) -> None:
            pass

    vsink = RoomAudioSink(Source())
    await vsink.write(bytes(24000 * 2), 24000)
    vsink.vsource.queued_duration = 0.6
    assert round(await vsink.cut(), 2) == 0.4


def test_a_retelling_starts_from_the_words_that_were_heard():
    vweather = PendingResult("Kirill", "get_current_weather(city='Tokyo')", "Tokyo: 23 degrees, clear", vattempts=1, vheard="В То")
    assert "after saying only «В То…»" in delivery_line([vweather], vgoing_on=False)
    vweather.vheard = ""
    assert "before they heard any of it" in delivery_line([vweather], vgoing_on=False)


@pytest.mark.asyncio
async def test_a_lead_in_that_plays_out_does_not_count_as_the_answer_after_it_being_heard():
    vcall, vtasks = running_call()
    vcall.vsource = type("Source", (), {"wait_for_playout": staticmethod(lambda: asyncio.sleep(0)), "queued_duration": 0})()
    await vcall.vfloor.acquire()
    await vcall.request(events.ResponseRequest(vcorrelation=CORRELATION))
    await vcall.on_event(events.AssistantSpeechStarted(vcorrelation=CORRELATION, vresponse_id="resp_lead_in"))
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="Сейчас гляну.", vresponse_id="resp_lead_in"))
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "Tokyo"}, "a"))
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION, vresponse_id="resp_lead_in"))
    await asyncio.sleep(0.01)
    assert 1 in vcall.vplayed_replies and not vcall.vpending, "the lead-in played out; the answer is still to come"
    await vcall.on_event(events.AssistantSpeechStarted(vcorrelation=CORRELATION, vresponse_id="resp_answer"))
    await vcall.cut_her_off()
    await asyncio.sleep(0.01)
    assert [vresult.vgoal for vresult in vcall.vpending] == ["get_current_weather(city='Tokyo')"]
    for vtask in vtasks:
        vtask.cancel()


@pytest.mark.asyncio
async def test_a_reply_that_had_played_out_before_the_cut_counts_as_heard_and_only_the_next_is_cut():
    vcall = bare_call()
    vcall.vopen_replies = {1, 2}
    vcall.vreply_audio = {1: (0.0, 2.5, "В Токио сейчас 23 градуса и ясное небо."), 2: (2.5, 6.5, "А вот и факт: шахматных партий больше, чем атомов.")}

    async def cut() -> float:
        return 4.5

    vcall.vsink.cut = cut
    await vcall.cut_her_off()
    assert vcall.vplayed_replies == {1} and vcall.vcut_replies == {2}
    assert vcall.vheard_by_reply == {2: "А вот и факт: шахматных"}, "half of the second reply's audio, not the first's"


@pytest.mark.asyncio
async def test_a_value_the_same_person_looked_up_again_is_dropped_as_stale():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "Tokyo"}, "a"))
    vtokyo = vcall.vturn_results[0]
    vcall.vturn_results.clear()
    vfact = PendingResult("Kirill", "science fact", "Octopuses have three hearts.")
    vanna = PendingResult("Anna", "get_current_weather(city='Rome')", "Rome: 25", vtool="get_current_weather", vcall=0)
    vcall.vpending.extend([vtokyo, vfact, vanna])
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "Paris"}, "b"))
    vcall.drop_stale()
    assert list(vcall.vpending) == [vfact, vanna], "only Kirill's older weather goes; a fact and Anna's lookup stay"


@pytest.mark.asyncio
async def test_a_result_cut_off_twice_is_offered_once_then_let_go():
    vcall = bare_call()
    vweather = PendingResult("Kirill", "get_current_weather(city='Tokyo')", "Tokyo: 23", vattempts=DELIVERY_ATTEMPTS - 1)
    vcall.vcut_replies = {1}
    await vcall.requeue_if_talked_over([vweather], 1)
    assert list(vcall.vpending) == [vweather] and vweather.voffered
    assert "offer to finish" in delivery_line([vweather], vgoing_on=False) and "[background results ready]" not in delivery_line([vweather], vgoing_on=False)
    vcall.vpending.clear()
    vcall.vcut_replies = {1, 2}
    await vcall.requeue_if_talked_over([vweather], 2)
    assert not vcall.vpending, "the offer itself talked over: let it go"
