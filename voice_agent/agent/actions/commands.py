from __future__ import annotations

import dataclasses
from typing import Protocol

from voice_agent.agent.actions.models import ActionMetadata, ActionOutcome, ActionProposal, Command


@dataclasses.dataclass(frozen=True, slots=True)
class PreparedAction:
    vcommand: Command
    vresolved_target: dict[str, object]
    vparameters: dict[str, object]
    vimpact: dict[str, object]
    vsummary: str
    vprecondition: dict[str, object] = dataclasses.field(default_factory=dict)


class CommandHandler(Protocol):
    """Owns how one action type is validated and executed.

    prepare resolves entities and computes impact but performs no effect. execute performs the
    effect for an already prepared command, inside its own transaction, and returns a structured
    outcome rather than raising for expected failures.
    """

    @property
    def vmetadata(self) -> ActionMetadata: ...

    async def prepare(self, varguments: dict[str, object]) -> PreparedAction: ...

    async def execute(self, vcommand: Command, vproposal: ActionProposal) -> ActionOutcome: ...


class CommandRegistry:
    def __init__(self) -> None:
        self._vhandlers: dict[str, CommandHandler] = {}

    def register(self, vhandler: CommandHandler) -> CommandHandler:
        self._vhandlers[vhandler.vmetadata.vaction_type] = vhandler
        return vhandler

    def get(self, vaction_type: str) -> CommandHandler | None:
        return self._vhandlers.get(vaction_type)

    def require(self, vaction_type: str) -> CommandHandler:
        vhandler = self._vhandlers.get(vaction_type)
        if vhandler is None:
            raise KeyError(f"no handler registered for action type {vaction_type!r}")
        return vhandler

    def action_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._vhandlers))
