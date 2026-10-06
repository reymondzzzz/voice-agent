from __future__ import annotations

import dataclasses
import time


@dataclasses.dataclass(slots=True)
class ConversationRuntime:
    """Fast-changing facts the delivery layer needs, kept out of LangGraph state.

    LangGraph owns the canonical semantic conversation; this owns who currently holds the floor.
    Routing a graph invocation through the checkpointer just to learn whether the user is still
    talking would be far too slow for a realtime loop.
    """

    vconversation_id: str
    vsession_id: str
    vconversation_epoch: int = 0
    vcurrent_turn_id: str | None = None
    vuser_speaking: bool = False
    vassistant_speaking: bool = False
    vlast_user_speech_ended_s: float = dataclasses.field(default_factory=time.time)
    vcurrent_intent: str = ""
    vseen_event_ids: set[str] = dataclasses.field(default_factory=set)
    vhandled_tool_calls: set[str] = dataclasses.field(default_factory=set)

    @property
    def vidle_seconds(self) -> float:
        if self.vuser_speaking or self.vassistant_speaking:
            return 0.0
        return max(0.0, time.time() - self.vlast_user_speech_ended_s)

    def bump_epoch(self, vreason: str) -> int:
        self.vconversation_epoch += 1
        return self.vconversation_epoch

    def seen(self, vevent_id: str) -> bool:
        if vevent_id in self.vseen_event_ids:
            return True
        self.vseen_event_ids.add(vevent_id)
        return False

    def tool_call_seen(self, vtool_call_id: str) -> bool:
        if vtool_call_id in self.vhandled_tool_calls:
            return True
        self.vhandled_tool_calls.add(vtool_call_id)
        return False
