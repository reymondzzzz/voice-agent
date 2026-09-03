from __future__ import annotations

import dataclasses


VOICE_BACKCHANNELS = frozenset({"mhm", "mm-hmm", "uh-huh", "мгм", "угу"})


@dataclasses.dataclass(frozen=True)
class VoiceInterruptionConfig:
    venergy_threshold: int = 500
    vpre_roll_ms: int = 150
    vprobable_speech_ms: int = 100
    vminimum_speech_ms: int = 250
    vend_silence_ms: int = 600
    vutterance_end_silence_ms: int = 1200
    vcandidate_stt_deadline_s: float = 3.0

    def validated(self) -> VoiceInterruptionConfig:
        vvalues = (
            self.venergy_threshold,
            self.vpre_roll_ms,
            self.vprobable_speech_ms,
            self.vminimum_speech_ms,
            self.vend_silence_ms,
            self.vutterance_end_silence_ms,
        )
        if any(type(vvalue) is not int or vvalue <= 0 for vvalue in vvalues):
            raise ValueError("voice interruption thresholds must be positive integers")
        if self.vutterance_end_silence_ms < self.vend_silence_ms:
            raise ValueError("utterance end silence cannot be shorter than interruption end silence")
        if self.vprobable_speech_ms > self.vminimum_speech_ms:
            raise ValueError("probable speech cannot exceed minimum speech")
        if type(self.vcandidate_stt_deadline_s) is not float or self.vcandidate_stt_deadline_s <= 0:
            raise ValueError("candidate STT deadline must be positive")
        return self


VOICE_DEFAULT_INTERRUPTION_CONFIG = VoiceInterruptionConfig()


def normalize_interruption_text(vtext: str) -> str:
    return " ".join(vtext.casefold().strip().rstrip(".!?,").split())


def transcript_commits_interruption(vtext: str) -> bool:
    vnormalized = normalize_interruption_text(vtext)
    return bool(vnormalized) and vnormalized not in VOICE_BACKCHANNELS


def candidate_commits_interruption(vpeak_speech_ms: int, vtext: str, vconfig: VoiceInterruptionConfig) -> bool:
    return vpeak_speech_ms >= vconfig.vminimum_speech_ms and transcript_commits_interruption(vtext)
