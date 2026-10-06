from __future__ import annotations

from voice_agent.agent.delivery.policy import DeliveryContext, DeliveryDecision, DeliveryPolicy
from voice_agent.agent.events import (
    ActionAwaitingConfirmation,
    BackgroundTaskCompleted,
    BackgroundTaskProgress,
    Criticality,
)
from voice_agent.agent.actions.models import EffectLevel
from voice_agent.agent.actions.policy import ActionPolicy
from voice_agent.correlation import Correlation

from tests.voice_agent.fake_domain import DeleteProjectHandler


def correlation(*, vepoch: int = 0) -> Correlation:
    return Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=vepoch, vturn_id="t")


def test_policy_queues_while_user_speaks():
    vpolicy = DeliveryPolicy()
    vevent = BackgroundTaskCompleted(vcorrelation=correlation(), vtask_id="task_1")
    vdecision = vpolicy.decide(vevent, DeliveryContext(vuser_speaking=True, vassistant_speaking=False, vconversation_epoch=0))
    assert vdecision is DeliveryDecision.QUEUE


def test_policy_waits_while_assistant_speaks_for_normal_results():
    vpolicy = DeliveryPolicy()
    vevent = BackgroundTaskCompleted(vcorrelation=correlation(), vtask_id="task_1")
    vdecision = vpolicy.decide(vevent, DeliveryContext(vuser_speaking=False, vassistant_speaking=True, vconversation_epoch=0))
    assert vdecision is DeliveryDecision.QUEUE


def test_policy_interrupts_only_for_a_correction():
    vpolicy = DeliveryPolicy()
    vevent = BackgroundTaskCompleted(vcorrelation=correlation(), vtask_id="task_1", vcriticality=Criticality.CORRECTION)
    vdecision = vpolicy.decide(vevent, DeliveryContext(vuser_speaking=False, vassistant_speaking=True, vconversation_epoch=0))
    assert vdecision is DeliveryDecision.INTERRUPT


def test_policy_stores_a_result_from_an_older_epoch_without_speaking():
    vpolicy = DeliveryPolicy()
    vevent = BackgroundTaskCompleted(vcorrelation=correlation(vepoch=0), vtask_id="task_1")
    vdecision = vpolicy.decide(vevent, DeliveryContext(vuser_speaking=False, vassistant_speaking=False, vconversation_epoch=3, vidle_seconds=10))
    assert vdecision is DeliveryDecision.STORE_SILENTLY


def test_policy_never_speaks_progress():
    vpolicy = DeliveryPolicy()
    vevent = BackgroundTaskProgress(vcorrelation=correlation(), vtask_id="task_1", vprogress="halfway")
    vdecision = vpolicy.decide(vevent, DeliveryContext(vuser_speaking=False, vassistant_speaking=False, vconversation_epoch=0, vidle_seconds=10))
    assert vdecision is DeliveryDecision.STORE_SILENTLY


def test_policy_surfaces_a_confirmation_prompt_when_the_floor_is_free():
    vpolicy = DeliveryPolicy()
    vevent = ActionAwaitingConfirmation(vcorrelation=correlation(), vtask_id=None)
    vdecision = vpolicy.decide(vevent, DeliveryContext(vuser_speaking=False, vassistant_speaking=False, vconversation_epoch=0, vidle_seconds=5))
    assert vdecision is DeliveryDecision.SPEAK_NOW




def test_policy_never_lets_a_background_task_auto_commit_an_irreversible_action():
    vpolicy = ActionPolicy()
    vmetadata = DeleteProjectHandler.vmetadata
    assert vpolicy.may_auto_commit(vmetadata, vfrom_background=True) is False
    assert vpolicy.may_auto_commit(vmetadata, vfrom_background=False) is False
    vdecision = vpolicy.decide(vmetadata, vfrom_background=True)
    assert vdecision.vrequires_confirmation is True
    assert vdecision.vallowed_in_background is False


def test_effect_levels_that_always_require_confirmation():
    vpolicy = ActionPolicy()
    assert EffectLevel.IRREVERSIBLE in vpolicy.ALWAYS_CONFIRM_LEVELS
    assert EffectLevel.WRITE_IMPORTANT in vpolicy.ALWAYS_CONFIRM_LEVELS
    assert EffectLevel.READ not in vpolicy.ALWAYS_CONFIRM_LEVELS
