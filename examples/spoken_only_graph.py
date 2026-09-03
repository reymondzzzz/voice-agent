from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import ToolMessage


def _streamed_token(vitem: Any) -> Any:
    if not isinstance(vitem, tuple):
        return vitem
    if len(vitem) == 2 and not isinstance(vitem[1], tuple):
        return vitem[0]
    if len(vitem) == 2 and isinstance(vitem[1], tuple) and len(vitem[1]) == 2:
        return vitem[1][0]
    if len(vitem) == 3 and isinstance(vitem[2], tuple) and len(vitem[2]) == 2:
        return vitem[2][0]
    return None


def is_tool_result(vitem: Any) -> bool:
    return isinstance(_streamed_token(vitem), ToolMessage)


class SpokenOnlyGraph:
    """Keeps tool results out of the audio while leaving them in the model's context.

    livekit's LangGraph adapter drops the metadata identifying which node produced a token and
    turns every message it receives into assistant speech, so a tool's return value is spoken
    verbatim before the agent's own sentence: "Friday 04 September 2026, 02:16 in Asia/Tokyo"
    followed by "It's 2:16 AM in Tokyo." Filtering the graph's own stream is the narrowest place
    to fix that without reimplementing the adapter.
    """

    def __init__(self, vgraph: Any) -> None:
        self._vgraph = vgraph

    def __getattr__(self, vname: str) -> Any:
        return getattr(self._vgraph, vname)

    def astream(self, *vargs: Any, **vkwargs: Any) -> AsyncIterator[Any]:
        return self._spoken_only(self._vgraph.astream(*vargs, **vkwargs))

    async def _spoken_only(self, vinner: AsyncIterator[Any]) -> AsyncIterator[Any]:
        async for vitem in vinner:
            if not is_tool_result(vitem):
                yield vitem
