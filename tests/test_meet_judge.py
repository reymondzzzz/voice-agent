import pytest

from examples import meet_judge
from examples.meet_judge import MeetJudge
from voice_agent.correlation import Correlation
from voice_agent.realtime import events

CORRELATION = Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)


MADE: list = []


class ScriptedSession:
    """A judge session that either answers or was closed by the server."""

    def __init__(self, **_vkwargs) -> None:
        self.vclosed_by_server = not MADE
        self.vrequests: list[events.ResponseRequest] = []
        MADE.append(self)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def correlation(self) -> Correlation:
        return CORRELATION

    async def request_response(self, vrequest: events.ResponseRequest) -> None:
        self.vrequests.append(vrequest)

    async def events(self):
        if self.vclosed_by_server:
            return
        while True:
            yield events.AssistantTranscript(vcorrelation=CORRELATION, vtext="RESPOND", vresponse_id="r")
            yield events.AssistantSpeechStopped(vcorrelation=CORRELATION, vresponse_id="r")


@pytest.mark.asyncio
async def test_a_judge_session_closed_by_the_server_is_reopened_for_the_same_question(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setattr(meet_judge, "QwenOmniSession", ScriptedSession)
    MADE.clear()
    vjudge = MeetJudge("room", "model")
    assert await vjudge("is it for her?") == "RESPOND"
    assert len(MADE) == 2 and MADE[1].vrequests[0].vtext_only
