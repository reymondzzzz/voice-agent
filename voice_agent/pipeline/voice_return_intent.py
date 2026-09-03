from __future__ import annotations

import enum


class VoiceReturnIntent(enum.Enum):
    RETURN_TO_ORIGINATOR = enum.auto()
    AMBIGUOUS = enum.auto()


_RETURN_PHRASES = frozenset(
    {
        "boss come back",
        "come back boss",
        "go back to boss",
        "return to boss",
        "switch back to boss",
        "back to boss",
        "boss vuelve",
        "vuelve con boss",
        "regresa a boss",
        "regresar a boss",
        "vuelve a boss",
        "босс вернись",
        "вернись к боссу",
        "вернуться к боссу",
        "назад к боссу",
        "переключи на босса",
    }
)

_STRIP_PUNCTUATION = str.maketrans("", "", ".,!?¡¿")


def classify_voice_return_command(vtranscript_text: str) -> VoiceReturnIntent:
    vnormalized = " ".join(vtranscript_text.casefold().translate(_STRIP_PUNCTUATION).split())
    if vnormalized in _RETURN_PHRASES:
        return VoiceReturnIntent.RETURN_TO_ORIGINATOR
    return VoiceReturnIntent.AMBIGUOUS
