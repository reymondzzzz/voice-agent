from examples.meet_memory import MEET_CONTEXT_WINDOW_S, MeetMemory, MeetTurn, background_brief


def meeting() -> MeetMemory:
    vmemory = MeetMemory()
    vmemory.add(MeetTurn(0.0, "Carl", "The deadline is October 15th."))
    vmemory.add(MeetTurn(200.0, "Anna", "Dmitry owns the webhooks."))
    vmemory.add(MeetTurn(400.0, "Carl", "Karen, check the details."))
    return vmemory


def test_only_turns_older_than_the_window_expire():
    vexpired = meeting().expired(400.0)
    assert [vturn.vspeaker for vturn in vexpired] == ["Carl"]
    assert meeting().expired(MEET_CONTEXT_WINDOW_S - 1) == []


def test_folding_replaces_exactly_the_folded_turns_with_notes():
    vmemory = meeting()
    vmemory.fold(vmemory.expired(400.0), "- deadline October 15th (Carl)")
    assert vmemory.vnotes == "- deadline October 15th (Carl)"
    assert vmemory.transcript() == "[Anna] Dmitry owns the webhooks.\n[Carl] Karen, check the details."


def test_turns_heard_while_folding_are_kept():
    vmemory = meeting()
    vexpired = vmemory.expired(400.0)
    vmemory.add(MeetTurn(401.0, "Anna", "Also the old system stays a month."))
    vmemory.fold(vexpired, "notes")
    assert vmemory.transcript().endswith("[Anna] Also the old system stays a month.")


def test_background_brief_carries_notes_and_recent_transcript():
    vmemory = meeting()
    vmemory.fold(vmemory.expired(400.0), "- deadline October 15th (Carl)")
    vbrief = background_brief("check the deadline leaves Dmitry three days", "Carl", vmemory)
    assert "Carl asked" in vbrief
    assert "- deadline October 15th (Carl)" in vbrief
    assert "[Anna] Dmitry owns the webhooks." in vbrief
