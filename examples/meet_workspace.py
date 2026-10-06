from __future__ import annotations

import dataclasses
import datetime
import re

from examples.meet_addressing import mentions_name

WORKDAY_START_H = 10
WORKDAY_END_H = 19
SLOT_STEP_MIN = 30
DOCUMENT_HITS = 2


@dataclasses.dataclass(frozen=True)
class Person:
    vname: str
    vrole: str
    valiases: tuple[str, ...] = ()

    @property
    def vfirst(self) -> str:
        return self.vname.split()[0]

    def answers_to(self, vasked: str) -> bool:
        return any(mentions_name(vasked, vcandidate) for vcandidate in (*self.vname.split(), *self.valiases))


@dataclasses.dataclass(frozen=True)
class Task:
    vkey: str
    vtitle: str
    vassignee: str
    vstatus: str
    vdue: datetime.date
    vnote: str = ""


@dataclasses.dataclass(frozen=True)
class Busy:
    vperson: str
    vday: int
    vstart: datetime.time
    vend: datetime.time
    vtitle: str


@dataclasses.dataclass(frozen=True)
class Document:
    vtitle: str
    vtext: str


TEAM = (
    Person("Кирилл Старков", "team lead of the payments team, owns the traffic switch (тимлид, переключение трафика)", ("Kirill", "Starkov")),
    Person("Анна Петрова", "product manager, owns the provider contract (продакт, договор с провайдером)", ("Аня", "Anna")),
    Person("Дмитрий Волков", "backend engineer, owns webhooks and retries (бэкенд, вебхуки, ретраи)", ("Дима", "Dmitry", "Dmitriy")),
    Person("Мария Соколова", "QA engineer, owns the test plan and load tests (тестирование, нагрузочные тесты)", ("Маша", "Maria")),
    Person("Алексей Иванов", "frontend engineer, owns the payment screens (фронтенд, экраны оплаты)", ("Лёша", "Alexey")),
    Person("Ольга Ким", "product designer (дизайн, макеты)", ("Оля", "Olga")),
)

# She says the status out loud, so it is described in Russian; the filter takes either name.
STATUS_RU = {"to do": "не начата", "in progress": "в работе", "in review": "на ревью", "blocked": "заблокирована", "done": "готово"}

PROJECT = "миграция платежей на нового провайдера"
PROJECT_DEADLINE = datetime.date(2026, 10, 15)

TASKS = (
    Task("PAY-101", "Перенести вебхуки на нового провайдера", "Дмитрий Волков", "in progress", datetime.date(2026, 10, 9), "about 3 days of work left"),
    Task("PAY-102", "Ретраи и идемпотентность платежей", "Дмитрий Волков", "to do", datetime.date(2026, 10, 12)),
    Task("PAY-103", "Тест-план миграции", "Мария Соколова", "in progress", datetime.date(2026, 10, 8)),
    Task("PAY-104", "Экран статуса платежа", "Алексей Иванов", "in review", datetime.date(2026, 10, 6)),
    Task("PAY-105", "Макеты экрана ошибок оплаты", "Ольга Ким", "done", datetime.date(2026, 9, 28)),
    Task("PAY-106", "Согласовать договор с провайдером", "Анна Петрова", "blocked", datetime.date(2026, 10, 3), "waiting for legal since 29 September"),
    Task("PAY-107", "План переключения трафика и отката", "Кирилл Старков", "to do", datetime.date(2026, 10, 13)),
    Task("PAY-108", "Нагрузочное тестирование", "Мария Соколова", "to do", datetime.date(2026, 10, 14), "cannot start before PAY-101 is done"),
)

# Days are counted from today, so "today" and "tomorrow" stay true whenever the demo runs.
CALENDAR = (
    Busy("Кирилл Старков", 0, datetime.time(11), datetime.time(12), "1:1 с Анной"),
    Busy("Анна Петрова", 0, datetime.time(11), datetime.time(12), "1:1 с Кириллом"),
    Busy("Анна Петрова", 0, datetime.time(15), datetime.time(16, 30), "Созвон с юристами по договору"),
    Busy("Дмитрий Волков", 0, datetime.time(14), datetime.time(15), "Техдизайн ретраев"),
    Busy("Мария Соколова", 0, datetime.time(10), datetime.time(13), "Регресс перед релизом"),
    Busy("Кирилл Старков", 1, datetime.time(10), datetime.time(10, 30), "Стендап"),
    Busy("Анна Петрова", 1, datetime.time(10), datetime.time(10, 30), "Стендап"),
    Busy("Дмитрий Волков", 1, datetime.time(10), datetime.time(10, 30), "Стендап"),
    Busy("Мария Соколова", 1, datetime.time(10), datetime.time(10, 30), "Стендап"),
    Busy("Алексей Иванов", 1, datetime.time(10), datetime.time(10, 30), "Стендап"),
    Busy("Дмитрий Волков", 1, datetime.time(12), datetime.time(18), "Фокус-время: вебхуки"),
    Busy("Анна Петрова", 1, datetime.time(14), datetime.time(15), "Демо для заказчика"),
)

DOCUMENTS = (
    Document("ADR-12: выбор нового платёжного провайдера", "Decided on 10 September: move to the new provider for lower fees and instant refunds. Old provider contract ends 31 October, so the migration must finish by 15 October with two weeks of overlap."),
    Document("Runbook: переключение трафика платежей", "Switch 10%, then 50%, then 100% of traffic, one hour apart, watching the error rate. Roll back if errors exceed 0.5%. Owner: Кирилл Старков. Needs PAY-101 and PAY-102 in production first."),
    Document("Протокол встречи 24 сентября", "Webhooks are the critical path (Дмитрий, about three days). Legal review of the provider contract is late; Анна escalates if nothing by 2 October. Load tests start only after webhooks."),
    Document("Политика отпусков", "Vacations are agreed with the team lead two weeks ahead. Анна Петрова is on vacation 16 to 20 October; Кирилл covers the provider contract."),
)


def stems(vtext: str) -> set[str]:
    # Five letters are enough to match "вебхуки" with "вебхуков" and "migration" with "migrate" without a stemmer.
    return {vword[:5] for vword in re.findall(r"\w+", vtext.casefold()) if len(vword) > 2}


def find_person(vasked: str, vrequester: str) -> Person | None:
    vasked = vasked.strip()
    if not vasked:
        return None
    if vasked.casefold() in ("me", "i", "я", "мне", "мои", "меня"):
        vasked = vrequester
    return next((vperson for vperson in TEAM if vperson.answers_to(vasked)), None)


def describe_task(vtask: Task) -> str:
    vnote = f"; {vtask.vnote}" if vtask.vnote else ""
    return f"{vtask.vkey} «{vtask.vtitle}»: {vtask.vassignee}, {STATUS_RU[vtask.vstatus]}, due {vtask.vdue:%a %d %b}{vnote}"


def list_tasks(vassignee: str, vstatus: str, vrequester: str) -> str:
    vtasks = list(TASKS)
    if vassignee:
        vperson = find_person(vassignee, vrequester)
        if vperson is None:
            return f"No one called {vassignee} on the team. The team: {', '.join(vperson.vname for vperson in TEAM)}."
        vtasks = [vtask for vtask in vtasks if vtask.vassignee == vperson.vname]
    if vstatus:
        vtasks = [vtask for vtask in vtasks if vstatus.casefold() in vtask.vstatus or stems(vstatus) & stems(STATUS_RU[vtask.vstatus])]
    if not vtasks:
        return "No matching tasks."
    return f"Project: {PROJECT}, deadline {PROJECT_DEADLINE:%d %B}.\n" + "\n".join(describe_task(vtask) for vtask in vtasks)


def get_task(vkey: str) -> str:
    vnumber = re.sub(r"\D", "", vkey)
    vtask = next((vtask for vtask in TASKS if vtask.vkey == f"PAY-{vnumber}"), None)
    return describe_task(vtask) if vtask else f"No task {vkey}. Tasks are PAY-101 to PAY-108."


def day_offset(vday: str, vtoday: datetime.date) -> int:
    vday = vday.strip().casefold()
    if vday in ("", "today", "сегодня"):
        return 0
    if vday in ("tomorrow", "завтра"):
        return 1
    return (datetime.date.fromisoformat(vday) - vtoday).days


def get_schedule(vperson_asked: str, vday: str, vrequester: str, vtoday: datetime.date) -> str:
    vperson = find_person(vperson_asked, vrequester)
    if vperson is None:
        return f"No one called {vperson_asked} on the team."
    voffset = day_offset(vday, vtoday)
    vbusy = sorted((vslot for vslot in CALENDAR if vslot.vperson == vperson.vname and vslot.vday == voffset), key=lambda vslot: vslot.vstart)
    vdate = vtoday + datetime.timedelta(days=voffset)
    if not vbusy:
        return f"{vperson.vname} has nothing booked on {vdate:%a %d %b}, working hours {WORKDAY_START_H}:00-{WORKDAY_END_H}:00."
    return f"{vperson.vname} on {vdate:%a %d %b}: " + "; ".join(f"{vslot.vstart:%H:%M}-{vslot.vend:%H:%M} {vslot.vtitle}" for vslot in vbusy)


def find_free_slot(vpeople_asked: list[str], vminutes: int, vday: str, vrequester: str, vnow: datetime.datetime) -> str:
    vpeople = [find_person(vasked, vrequester) for vasked in vpeople_asked]
    vunknown = [vasked for vasked, vperson in zip(vpeople_asked, vpeople, strict=True) if vperson is None]
    if vunknown:
        return f"No one called {', '.join(vunknown)} on the team."
    vnames = {vperson.vname for vperson in vpeople if vperson is not None}
    voffset = day_offset(vday, vnow.date())
    vbusy = [vslot for vslot in CALENDAR if vslot.vperson in vnames and vslot.vday == voffset]
    vdate = vnow.date() + datetime.timedelta(days=voffset)
    vstart = datetime.datetime.combine(vdate, datetime.time(WORKDAY_START_H))
    # Today, the hours already gone are not on offer: start at the next half hour from now.
    while vstart < vnow:
        vstart += datetime.timedelta(minutes=SLOT_STEP_MIN)
    vlength = datetime.timedelta(minutes=vminutes or SLOT_STEP_MIN)
    while vstart + vlength <= datetime.datetime.combine(vdate, datetime.time(WORKDAY_END_H)):
        vend = vstart + vlength
        if not any(vstart.time() < vslot.vend and vslot.vstart < vend.time() for vslot in vbusy):
            return f"First slot all of {', '.join(sorted(vnames))} have free on {vdate:%a %d %b}: {vstart:%H:%M}-{vend:%H:%M}."
        vstart += datetime.timedelta(minutes=SLOT_STEP_MIN)
    return f"No common {vlength.seconds // 60}-minute slot on {vdate:%a %d %b}."


def who_is(vasked: str, vrequester: str) -> str:
    vperson = find_person(vasked, vrequester)
    if vperson is not None:
        return f"{vperson.vname}: {vperson.vrole}."
    vowners = [vperson for vperson in TEAM if stems(vasked) & stems(vperson.vrole)]
    if vowners:
        return "; ".join(f"{vperson.vname}: {vperson.vrole}" for vperson in vowners) + "."
    return f"Nobody on the team matches {vasked}. The team: " + "; ".join(f"{vperson.vname} ({vperson.vrole})" for vperson in TEAM) + "."


def search_documents(vquery: str) -> str:
    vscores = [(len(stems(vquery) & stems(f"{vdocument.vtitle} {vdocument.vtext}")), vdocument) for vdocument in DOCUMENTS]
    vhits = [vdocument for vscore, vdocument in sorted(vscores, key=lambda vpair: -vpair[0]) if vscore][:DOCUMENT_HITS]
    if not vhits:
        return f"Nothing in the team documents about {vquery}."
    return "\n".join(f"«{vdocument.vtitle}»: {vdocument.vtext}" for vdocument in vhits)
