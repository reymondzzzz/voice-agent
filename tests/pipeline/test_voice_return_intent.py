import pytest

from voice_agent.pipeline import voice_return_intent


@pytest.mark.parametrize(
    "vtext",
    (
        "Boss, come back.",
        "boss come back",
        "COME BACK BOSS",
        "go back to boss",
        "Return to Boss!",
        "switch back to boss",
        "back to boss",
        "Boss, vuelve.",
        "vuelve con boss",
        "regresa a boss",
        "regresar a boss",
        "vuelve a boss",
        "Босс, вернись.",
        "вернись к боссу",
        "вернуться к боссу",
        "назад к боссу",
        "переключи на босса",
    ),
)
def test_high_confidence_return_commands_are_accepted(vtext: str) -> None:
    assert voice_return_intent.classify_voice_return_command(vtext) is voice_return_intent.VoiceReturnIntent.RETURN_TO_ORIGINATOR


@pytest.mark.parametrize(
    "vtext",
    (
        "",
        "tell Boss later",
        "I'll ask Boss about that",
        "ask boss to remind me",
        "come back later",
        "I'll come back to this later",
        "boss, tell me about that",
        "when will you come back",
        "remind boss to come back to this",
    ),
)
def test_ambiguous_references_to_boss_never_route(vtext: str) -> None:
    assert voice_return_intent.classify_voice_return_command(vtext) is voice_return_intent.VoiceReturnIntent.AMBIGUOUS
