from __future__ import annotations

import os
import pathlib
import secrets

from aiohttp import web
from dotenv import load_dotenv
from livekit import api


load_dotenv(".env.local")

WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"
REQUIRED_ENV = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
ROOM_PREFIX = "voice-"
PORT = 8080


def require_env() -> None:
    vmissing = [vname for vname in REQUIRED_ENV if not os.environ.get(vname)]
    if vmissing:
        raise SystemExit(f"missing in .env.local: {', '.join(vmissing)}")


def mint_caller_token(vroom: str, videntity: str) -> str:
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(videntity)
        .with_name("Caller")
        .with_grants(api.VideoGrants(room_join=True, room=vroom))
        .to_jwt()
    )


async def handle_token(vrequest: web.Request) -> web.Response:
    vroom = vrequest.query.get("room", "").strip() or f"voice-{secrets.token_hex(4)}"
    videntity = f"caller-{secrets.token_hex(3)}"
    return web.json_response(
        {
            "vroom": vroom,
            "videntity": videntity,
            "vlk_url": os.environ["LIVEKIT_URL"],
            "vlk_token": mint_caller_token(vroom, videntity),
        }
    )


async def handle_index(vrequest: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEB_DIR / "index.html")


def build_app() -> web.Application:
    vapp = web.Application()
    vapp.router.add_get("/", handle_index)
    vapp.router.add_get("/token", handle_token)
    vapp.router.add_static("/static", WEB_DIR)
    return vapp


if __name__ == "__main__":
    require_env()
    web.run_app(build_app(), port=PORT)
