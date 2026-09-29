from examples.meet_addressing import FOLLOW_UP_WINDOW_S, MeetAddressing, mentions_name


def test_humans_talking_to_each_other_stay_unanswered():
    vaddressing = MeetAddressing("Jarvis")
    assert not vaddressing.is_addressed("Anna", "Bob, did you ship the release?", 0.0)
    assert not vaddressing.is_addressed("Bob", "Yes, it went out this morning.", 1.0)


def test_name_engages_and_same_speaker_follows_up_without_it():
    vaddressing = MeetAddressing("Jarvis")
    assert vaddressing.is_addressed("Anna", "Jarvis, what time is it in Tokyo?", 0.0)
    vaddressing.bot_finished_speaking(3.0)
    assert vaddressing.is_addressed("Anna", "And in London?", 5.0)


def test_misheard_name_still_engages():
    assert mentions_name("hey jarvys what's the weather", "Jarvis")
    assert not mentions_name("we should check the invoice", "Jarvis")
    assert mentions_name("Caren, can you check the deadline?", "Karen")
    assert not mentions_name("we can check the deadline", "Karen")


def test_name_glued_into_one_word_still_engages():
    assert mentions_name("VoiceAgent, what time is it in Tokyo right now?", "Voice Agent")
    assert not mentions_name("my voice is gone today", "Voice Agent")


def test_follow_up_expires():
    vaddressing = MeetAddressing("Jarvis")
    vaddressing.is_addressed("Anna", "Jarvis, what time is it?", 0.0)
    vaddressing.bot_finished_speaking(2.0)
    assert not vaddressing.is_addressed("Anna", "And in London?", 2.0 + FOLLOW_UP_WINDOW_S + 1)


def test_other_participant_cannot_ride_the_follow_up():
    vaddressing = MeetAddressing("Jarvis")
    vaddressing.is_addressed("Anna", "Jarvis, what time is it?", 0.0)
    assert not vaddressing.is_addressed("Bob", "What about London?", 1.0)


def test_another_human_answering_ends_the_engagement():
    vaddressing = MeetAddressing("Jarvis")
    vaddressing.is_addressed("Anna", "Jarvis, what time is it?", 0.0)
    vaddressing.is_addressed("Bob", "It's almost noon here.", 1.0)
    assert not vaddressing.is_addressed("Anna", "Thanks, that helps a lot.", 2.0)


def test_turning_to_another_participant_ends_the_engagement():
    vaddressing = MeetAddressing("Jarvis")
    vaddressing.is_addressed("Bob Smith", "Morning everyone.", 0.0)
    vaddressing.is_addressed("Anna", "Jarvis, summarize the plan.", 1.0)
    assert not vaddressing.is_addressed("Anna", "Bob, does that match what you heard?", 2.0)
    assert not vaddressing.is_addressed("Anna", "Great, let's continue then.", 3.0)


def test_backchannel_is_not_a_question():
    vaddressing = MeetAddressing("Jarvis")
    vaddressing.is_addressed("Anna", "Jarvis, what time is it?", 0.0)
    assert not vaddressing.is_addressed("Anna", "okay", 1.0)


def test_cyrillic_name_engages():
    assert mentions_name("Карен, который час в Токио?", "Karen")
    assert mentions_name("Карин, проверь дедлайн", "Karen")
    assert not mentions_name("Как дела?", "Karen")
