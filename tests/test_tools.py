from __future__ import annotations

import datetime
import zoneinfo

from tools import DEFAULT_TIMEZONE, DUMMY_WEATHER, get_current_time, get_current_weather, persona_tools


def test_known_city_returns_its_dummy_reading():
    vreading = get_current_weather.invoke({"city": "London"})
    vtemperature, vsky = DUMMY_WEATHER["london"]
    assert f"{vtemperature} degrees Celsius" in vreading
    assert vsky in vreading


def test_city_lookup_ignores_case_and_padding_but_echoes_what_was_asked():
    vpadded = get_current_weather.invoke({"city": "  LONDON "})
    vtemperature, vsky = DUMMY_WEATHER["london"]
    assert vpadded.startswith("LONDON:")
    assert f"{vtemperature} degrees Celsius, {vsky}" in vpadded


def test_unknown_city_falls_back_instead_of_failing():
    assert "degrees Celsius" in get_current_weather.invoke({"city": "Narnia"})


def test_weather_is_labelled_as_placeholder():
    assert "(placeholder data)" in get_current_weather.invoke({"city": "Tokyo"})


def test_time_reports_the_requested_timezone():
    vnow = get_current_time.invoke({"timezone": "Europe/London"})
    assert vnow.endswith("in Europe/London")
    assert f"{datetime.datetime.now(zoneinfo.ZoneInfo('Europe/London')):%Y}" in vnow


def test_empty_timezone_means_utc():
    assert get_current_time.invoke({"timezone": ""}).endswith(f"in {DEFAULT_TIMEZONE}")


def test_unknown_timezone_returns_a_speakable_error():
    assert get_current_time.invoke({"timezone": "Mars/Olympus"}) == "Error: unknown timezone 'Mars/Olympus'"


def test_each_persona_gets_only_its_own_tool():
    assert [vtool.name for vtool in persona_tools("boss")] == []
    assert [vtool.name for vtool in persona_tools("alice")] == ["get_current_weather"]
    assert [vtool.name for vtool in persona_tools("bob")] == ["get_current_time"]
    assert persona_tools("nobody") == []
