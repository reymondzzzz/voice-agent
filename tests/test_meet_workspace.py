import datetime

from examples import meet_workspace

TODAY = datetime.date(2026, 10, 1)


def test_tasks_are_found_by_name_nickname_or_me():
    assert "PAY-101" in meet_workspace.list_tasks("Дмитрий", "", "Anna Petrova")
    assert "PAY-102" in meet_workspace.list_tasks("Дима", "", "Anna Petrova")
    vmine = meet_workspace.list_tasks("мне", "", "Kirill Starkov")
    assert "PAY-107" in vmine and "PAY-101" not in vmine
    assert "No one called Пётр" in meet_workspace.list_tasks("Пётр", "", "Kirill Starkov")


def test_status_filter_takes_english_or_russian_and_is_spoken_in_russian():
    for vstatus in ("blocked", "заблокированные", "заблокирована"):
        vblocked = meet_workspace.list_tasks("", vstatus, "Kirill Starkov")
        assert "PAY-106" in vblocked and "PAY-101" not in vblocked, vstatus
    vtodo = meet_workspace.list_tasks("", "to do", "Kirill Starkov")
    assert "PAY-102" in vtodo and "не начата" in vtodo and "to do" not in vtodo


def test_a_task_key_survives_speech_recognition():
    assert meet_workspace.get_task("пэй 104").startswith("PAY-104")
    assert meet_workspace.get_task("PAY-999").startswith("No task")
    assert meet_workspace.get_task("PAY-1").startswith("No task"), "a partial number is not some task that ends in it"
    assert meet_workspace.get_task("пэй").startswith("No task")


def test_owners_are_found_by_area_in_either_language():
    assert meet_workspace.who_is("вебхуки", "Kirill Starkov").startswith("Дмитрий Волков")
    assert meet_workspace.who_is("provider contract", "Kirill Starkov").startswith("Анна Петрова")


def test_a_free_slot_respects_everyone_and_the_hours_already_gone():
    vmorning = datetime.datetime.combine(TODAY, datetime.time(9))
    assert "10:30-11:00" in meet_workspace.find_free_slot(["Кирилл", "Анна", "Дмитрий"], 30, "tomorrow", "Kirill Starkov", vmorning)
    vafternoon = datetime.datetime.combine(TODAY, datetime.time(14, 10))
    assert "16:30-17:30" in meet_workspace.find_free_slot(["Кирилл", "Анна"], 60, "today", "Kirill Starkov", vafternoon)
    vevening = datetime.datetime.combine(TODAY, datetime.time(18, 40))
    assert meet_workspace.find_free_slot(["Кирилл", "Анна"], 60, "today", "Kirill Starkov", vevening).startswith("No common")


def test_documents_are_searched_by_meaning_words_and_an_unknown_topic_finds_nothing():
    assert "Runbook" in meet_workspace.search_documents("откат при переключении трафика")
    assert meet_workspace.search_documents("квартальный бюджет маркетинга").startswith("Nothing")
