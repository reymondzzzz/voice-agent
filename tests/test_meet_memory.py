from examples.meet_memory import MEET_CONTEXT_WINDOW_S, MeetMemory, MeetTurn


def meeting() -> MeetMemory:
    vmemory = MeetMemory()
    vmemory.add(MeetTurn(0.0, "Carl", "The deadline is October 15th."))
    vmemory.add(MeetTurn(300.0, "Anna", "Dmitry owns the webhooks."))
    vmemory.add(MeetTurn(700.0, "Carl", "Karen, check the details."))
    return vmemory


def test_the_window_keeps_the_last_ten_minutes():
    assert MEET_CONTEXT_WINDOW_S == 600.0
    vmemory = meeting()
    vmemory.forget_before(700.0)
    assert vmemory.transcript() == "[Anna] Dmitry owns the webhooks.\n[Carl] Karen, check the details."


def test_nothing_inside_the_window_is_forgotten():
    vmemory = meeting()
    vmemory.forget_before(MEET_CONTEXT_WINDOW_S)
    assert len(vmemory.vturns) == 3
