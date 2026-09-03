from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator

from voice_agent.agent.actions.commands import PreparedAction
from voice_agent.agent.actions.models import (
    ActionMetadata,
    ActionOutcome,
    ActionProposal,
    Command,
    EffectLevel,
    ExternalEffectStatus,
    FailureCode,
)


@dataclasses.dataclass
class Project:
    vproject_id: str
    vname: str
    vresource_count: int
    vversion: int = 1
    vdeleted: bool = False


class FakeProjectRepository:
    """Stands in for a database, including versions so optimistic concurrency can be tested."""

    def __init__(self) -> None:
        self.vprojects: dict[str, Project] = {
            "proj_prod": Project(vproject_id="proj_prod", vname="production", vresource_count=42),
            "proj_stage": Project(vproject_id="proj_stage", vname="staging", vresource_count=3),
        }
        self.vaudit_rows: list[str] = []
        self.vcommits = 0
        self.vrollbacks = 0

    def by_name(self, vname: str) -> Project | None:
        for vproject in self.vprojects.values():
            if vproject.vname == vname:
                return vproject
        return None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        vsnapshot = {vid: dataclasses.replace(vproject) for vid, vproject in self.vprojects.items()}
        vaudit_snapshot = list(self.vaudit_rows)
        try:
            yield
        except Exception:
            self.vprojects = vsnapshot
            self.vaudit_rows = vaudit_snapshot
            self.vrollbacks += 1
            raise
        self.vcommits += 1


@dataclasses.dataclass(frozen=True, slots=True)
class DeleteProject(Command):
    vproject_id: str
    vexpected_version: int


@dataclasses.dataclass(frozen=True, slots=True)
class MarkNotificationRead(Command):
    vnotification_id: str


class DeleteProjectHandler:
    vmetadata = ActionMetadata(
        vaction_type="delete_project",
        veffect_level=EffectLevel.IRREVERSIBLE,
        vrequires_confirmation=True,
        vidempotent=True,
        vrequires_fresh_state=True,
        vconfirmation_ttl_s=120.0,
    )

    def __init__(self, vrepository: FakeProjectRepository, *, vfail_with: Exception | None = None) -> None:
        self.vrepository = vrepository
        self.vfail_with = vfail_with
        self.vexecutions = 0

    async def prepare(self, varguments: dict[str, object]) -> PreparedAction:
        vname = str(varguments.get("name", ""))
        vproject = self.vrepository.by_name(vname)
        if vproject is None:
            raise KeyError(f"no project named {vname!r}")
        return PreparedAction(
            vcommand=DeleteProject(vproject_id=vproject.vproject_id, vexpected_version=vproject.vversion),
            vresolved_target={"project_id": vproject.vproject_id, "name": vproject.vname},
            vparameters={},
            vimpact={"resources_deleted": vproject.vresource_count},
            vsummary=f"Delete the {vproject.vname} project and its {vproject.vresource_count} associated resources?",
            vprecondition={"version": vproject.vversion},
        )

    async def execute(self, vcommand: Command, vproposal: ActionProposal) -> ActionOutcome:
        assert isinstance(vcommand, DeleteProject)
        self.vexecutions += 1
        vproject = self.vrepository.vprojects.get(vcommand.vproject_id)
        if vproject is None:
            return ActionOutcome(False, vfailure=FailureCode.NOT_FOUND, vmessage="project vanished")
        if vproject.vversion != vcommand.vexpected_version:
            return ActionOutcome(
                False,
                vfailure=FailureCode.CONFLICT,
                vmessage=f"expected version {vcommand.vexpected_version}, found {vproject.vversion}",
            )
        try:
            with self.vrepository.transaction():
                vproject.vdeleted = True
                vproject.vversion += 1
                self.vrepository.vaudit_rows.append(f"deleted {vproject.vproject_id}")
                if self.vfail_with is not None:
                    raise self.vfail_with
        except Exception as vexc:
            return ActionOutcome(False, vfailure=FailureCode.EXTERNAL_SERVICE_FAILED, vmessage=str(vexc))
        return ActionOutcome(
            True,
            vresult={"project_id": vproject.vproject_id, "resources_deleted": vproject.vresource_count},
            vexternal_status=ExternalEffectStatus.SUCCEEDED,
        )


class MarkNotificationReadHandler:
    vmetadata = ActionMetadata(
        vaction_type="mark_notification_read",
        veffect_level=EffectLevel.WRITE_REVERSIBLE,
        vrequires_confirmation=False,
        vidempotent=True,
    )

    def __init__(self) -> None:
        self.vread: list[str] = []

    async def prepare(self, varguments: dict[str, object]) -> PreparedAction:
        vnotification_id = str(varguments.get("notification_id", ""))
        return PreparedAction(
            vcommand=MarkNotificationRead(vnotification_id=vnotification_id),
            vresolved_target={"notification_id": vnotification_id},
            vparameters={},
            vimpact={"notifications": 1},
            vsummary=f"Mark notification {vnotification_id} as read",
        )

    async def execute(self, vcommand: Command, vproposal: ActionProposal) -> ActionOutcome:
        assert isinstance(vcommand, MarkNotificationRead)
        self.vread.append(vcommand.vnotification_id)
        return ActionOutcome(True, vresult={"notification_id": vcommand.vnotification_id, "read": True})
