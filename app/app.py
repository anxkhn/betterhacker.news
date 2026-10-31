import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("BETTERHACKER_DATA", ROOT / "data"))
FEEDS = ["top", "new", "best", "show", "ask", "jobs"]
PUBLIC_URL = os.environ.get("BETTERHACKER_PUBLIC_URL", "http://localhost:8791")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["betterhacker.anaskhan.me", "localhost", "127.0.0.1", "testserver", "*.trycloudflare.com"],
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Strict-Transport-Security": "max-age=31536000",
            "Cache-Control": "public, max-age=30" if request.url.path == "/" else "no-cache",
        }
    )
    return response


def snapshot():
    try:
        return json.loads((DATA / "current.json").read_text())
    except (OSError, ValueError):
        return {"generated_at": None, "feeds": {}}


def timestamp_age(stamp):
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return None


def status(data):
    stamp = data.get("generated_at")
    age = timestamp_age(stamp)
    feed_timestamps = data.get("feed_generated_at", {})
    stale_feeds = [
        feed
        for feed in FEEDS
        if feed_timestamps
        and (timestamp_age(feed_timestamps.get(feed)) is None or timestamp_age(feed_timestamps.get(feed)) > 900)
    ]
    return {
        "generated_at": stamp,
        "stale": age is None or age > 900 or bool(stale_feeds),
        "stale_feeds": stale_feeds,
        "age_seconds": round(age) if age is not None else None,
        "refresh_interval_seconds": 300,
        "stories": {feed: len(data.get("feeds", {}).get(feed, [])) for feed in FEEDS},
        "thumbnail_stats": data.get("thumbnail_stats", {}),
    }


@app.get("/")
def get_articles(request: Request, feed: str = "top"):
    feed = feed if feed in FEEDS else "top"
    data = snapshot()
    state = status(data)
    stories = data.get("feeds", {}).get(feed, [])
    stamp = data.get("feed_generated_at", {}).get(feed, state["generated_at"])
    fetched_at = datetime.fromisoformat(stamp).strftime("%H:%M UTC") if stamp else "Waiting for first refresh"
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "feed": feed,
            "public_url": PUBLIC_URL,
            "feeds": FEEDS,
            "stories": stories,
            "generated_at": state["generated_at"] or "",
            "fetched_at": fetched_at,
            "stale": timestamp_age(stamp) is None or timestamp_age(stamp) > 900,
            "topics": sorted({story["topic"] for story in stories}),
        },
    )


@app.get("/api/status")
def get_status():
    return status(snapshot())


@app.get("/healthz")
def health():
    state = status(snapshot())
    return JSONResponse({"ok": not state["stale"], **state}, status_code=503 if state["stale"] else 200)


@app.get("/media/{story_id}.webp")
def thumbnail(story_id: int):
    path = DATA / "media" / f"{story_id}.webp"
    if story_id < 1 or not path.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=300"})
