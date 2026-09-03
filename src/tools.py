from __future__ import annotations

import datetime
import zoneinfo

from langchain_core.tools import BaseTool, tool


DUMMY_WEATHER: dict[str, tuple[int, str]] = {
    "london": (14, "light rain"),
    "moscow": (9, "overcast"),
    "san francisco": (18, "fog"),
    "tokyo": (23, "clear skies"),
}
DUMMY_WEATHER_FALLBACK = (21, "clear skies")

DEFAULT_TIMEZONE = "UTC"


@tool
def get_current_weather(city: str) -> str:
    """Current weather for a city, as placeholder data for development.

    city is the place the caller asked about. The reading is invented, not observed, so tell the
    caller it is placeholder data instead of presenting it as a real forecast.
    """
    vcity = city.strip()
    vtemperature, vsky = DUMMY_WEATHER.get(vcity.lower(), DUMMY_WEATHER_FALLBACK)
    return f"{vcity or 'that location'}: {vtemperature} degrees Celsius, {vsky} (placeholder data)"


@tool
def get_current_time(timezone: str = "") -> str:
    """Current date and time in an IANA timezone such as Europe/London.

    timezone may be left empty, which means UTC. Returns an error string the caller can be told
    verbatim if the timezone is not a real one.
    """
    vname = timezone.strip() or DEFAULT_TIMEZONE
    try:
        vzone = zoneinfo.ZoneInfo(vname)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return f"Error: unknown timezone {vname!r}"
    return f"{datetime.datetime.now(vzone):%A %d %B %Y, %H:%M} in {vname}"


PERSONA_TOOLS: dict[str, tuple[BaseTool, ...]] = {
    "boss": (),
    "alice": (get_current_weather,),
    "bob": (get_current_time,),
}


def persona_tools(vagent_id: str) -> list[BaseTool]:
    return list(PERSONA_TOOLS.get(vagent_id, ()))
