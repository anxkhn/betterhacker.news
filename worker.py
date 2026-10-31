import concurrent.futures
import http.client
import json
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.previews import (
    UnsafeURL,
    article_preview,
    atomic_write,
    editorial_thumbnail,
    fetch_public,
    save_thumbnail,
    validate_url,
)

LOGGER = logging.getLogger(__name__)
DATA_ROOT = Path(os.environ.get("BETTERHACKER_DATA", Path(__file__).resolve().parent / "data"))
API_ROOT = "https://hacker-news.firebaseio.com/v0"
FEEDS = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
    "show": "showstories",
    "ask": "askstories",
    "jobs": "jobstories",
}
STORY_LIMIT = 40
ITEM_CONCURRENCY = 12
PREVIEW_CONCURRENCY = 4
PREVIEW_RETRIES = 24
PREVIEW_TTL = 24 * 60 * 60
RUN_SECONDS = 220


def fetch_json(url, deadline):
    data, _ = fetch_public(url, 512 * 1024, {"application/json"}, deadline)
    return json.loads(data)


def read_json(path, default):
    try:
        with Path(path).open() as source:
            return json.load(source)
    except (OSError, ValueError):
        return default


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def topic_for(item):
    title = item["title"].lower()
    if item.get("type") == "job":
        return "Jobs"
    if title.startswith("ask hn:"):
        return "Ask HN"
    if title.startswith("show hn:"):
        return "Show HN"
    for topic, terms in (
        ("AI", ("artificial intelligence", "machine learning", "llm", "openai", "neural")),
        ("Science", ("physics", "space", "biology", "quantum", "science")),
        ("Programming", ("python", "rust", "javascript", "compiler", "programming", "github")),
        ("Security", ("security", "vulnerability", "encryption", "malware")),
    ):
        if any(term in title for term in terms):
            return topic
    return "Technology"


def story_from_item(item, rank):
    if not isinstance(item, dict) or item.get("deleted") or item.get("dead"):
        return None
    if item.get("type") not in {"story", "job", "poll"} or not isinstance(item.get("title"), str):
        return None
    story_id = item.get("id")
    if type(story_id) is not int or story_id <= 0:
        return None
    discussion = f"https://news.ycombinator.com/item?id={story_id}"
    try:
        url = validate_url(item.get("url", ""))
    except UnsafeURL:
        url = discussion
    return {
        "id": story_id,
        "title": item["title"],
        "url": url,
        "domain": urlsplit(url).hostname,
        "score": item.get("score", 0),
        "descendants": item.get("descendants", 0),
        "by": item.get("by", ""),
        "time": item.get("time", 0),
        "topic": topic_for(item),
        "rank": rank,
        "thumbnail": f"/media/{story_id}.webp",
        "image_kind": "generated",
    }


def bounded_results(keys, function, workers, deadline):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        iterator = iter(keys)
        for _ in range(workers):
            key = next(iterator, None)
            if key is not None:
                pending[pool.submit(function, key)] = key
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = concurrent.futures.wait(
                pending, timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                key = pending.pop(future)
                try:
                    results[key] = (True, future.result())
                except (OSError, ValueError, SyntaxError, http.client.HTTPException) as exc:
                    LOGGER.warning("Fetch failed for %s: %s", key, type(exc).__name__)
                    results[key] = (False, None)
                if time.monotonic() < deadline:
                    key = next(iterator, None)
                    if key is not None:
                        pending[pool.submit(function, key)] = key
        for future in pending:
            future.cancel()
    return results


def refresh_thumbnails(stories, root, deadline, now):
    media = root / "media"
    cache = read_json(root / "thumbnail-cache.json", {})
    unique = {story["id"]: story for story in stories}
    updated = {}
    eligible = []
    for story_id, story in unique.items():
        old = cache.get(str(story_id), {})
        path = media / f"{story_id}.webp"
        same_source = old.get("url") == story["url"]
        if (
            not path.is_file()
            or not same_source
            or (old.get("kind") != "preview" and old.get("title") != story["title"])
        ):
            save_thumbnail(path, editorial_thumbnail(story))
            old = {"kind": "generated", "attempted_at": 0}
        entry = {**old, "url": story["url"], "title": story["title"]}
        updated[str(story_id)] = entry
        if entry.get("kind") != "preview" or now - entry.get("preview_at", 0) >= PREVIEW_TTL:
            eligible.append(story_id)
    eligible.sort(key=lambda story_id: (updated[str(story_id)].get("attempted_at", 0), story_id))
    selected = eligible[:PREVIEW_RETRIES]

    def preview(story_id):
        return article_preview(unique[story_id]["url"], deadline=deadline)

    results = bounded_results(selected, preview, PREVIEW_CONCURRENCY, deadline)
    for story_id, (success, image) in results.items():
        entry = updated[str(story_id)]
        entry["attempted_at"] = now
        if success:
            save_thumbnail(media / f"{story_id}.webp", image)
            entry.update(kind="preview", preview_at=now)
    for story in stories:
        story["image_kind"] = updated[str(story["id"])]["kind"]
    write_json(root / "thumbnail-cache.json", updated)
    previews = sum(entry["kind"] == "preview" for entry in updated.values())
    return {
        "total": len(unique),
        "preview": previews,
        "generated": len(unique) - previews,
        "attempted": len(results),
        "successful": sum(success for success, _ in results.values()),
    }


def _run(root):
    deadline = time.monotonic() + RUN_SECONDS
    previous = read_json(root / "current.json", {"feeds": {}})
    old_feeds = previous.get("feeds", {})
    old_timestamps = previous.get("feed_generated_at", {})

    def fetch_feed(name):
        ids = fetch_json(f"{API_ROOT}/{FEEDS[name]}.json", deadline)
        if not isinstance(ids, list) or any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError("Invalid HN feed")
        return list(dict.fromkeys(ids))[:STORY_LIMIT]

    feed_results = bounded_results(FEEDS, fetch_feed, 6, deadline)
    if not any(success for success, _ in feed_results.values()):
        LOGGER.warning("All feeds failed; retaining current snapshot")
        return previous
    feed_ids = {}
    feed_timestamps = {}
    for name in FEEDS:
        success, ids = feed_results.get(name, (False, None))
        feed_ids[name] = ids if success else [story["id"] for story in old_feeds.get(name, [])][:STORY_LIMIT]
        feed_timestamps[name] = (
            datetime.now(UTC).isoformat()
            if success
            else old_timestamps.get(name, previous.get("generated_at") if name in old_feeds else None)
        )
    old_stories = {story["id"]: story for stories in old_feeds.values() for story in stories}
    ids = list(dict.fromkeys(story_id for values in feed_ids.values() for story_id in values))

    def fetch_item(story_id):
        item = fetch_json(f"{API_ROOT}/item/{story_id}.json", deadline)
        if item is not None and (not isinstance(item, dict) or item.get("id") != story_id):
            raise ValueError("Invalid HN item")
        return item

    items = bounded_results(ids, fetch_item, ITEM_CONCURRENCY, deadline)
    feeds = {}
    for name, values in feed_ids.items():
        stories = []
        for rank, story_id in enumerate(values, 1):
            success, item = items.get(story_id, (False, None))
            if success:
                story = story_from_item(item, rank)
            else:
                old = old_stories.get(story_id)
                story = dict(old, rank=rank) if old else None
            if story:
                stories.append(story)
        feeds[name] = stories
    if time.monotonic() >= deadline:
        return previous
    stats = refresh_thumbnails([story for stories in feeds.values() for story in stories], root, deadline, time.time())
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(),
        "feeds": feeds,
        "feed_generated_at": feed_timestamps,
        "thumbnail_stats": stats,
    }
    write_json(root / "current.json", snapshot)
    active_ids = {story["id"] for stories in feeds.values() for story in stories}
    for path in (root / "media").glob("*.webp"):
        if path.stem.isdigit() and int(path.stem) not in active_ids and time.time() - path.stat().st_mtime > 86400:
            path.unlink(missing_ok=True)
    return snapshot


def betterhacker_news_worker(data_root=None):
    # DNS and TLS implementations can block beyond socket timeouts. The scheduled CLI
    # has a hard process deadline as well as the cooperative per-request deadlines.
    guarded = threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM")
    if guarded:
        previous_handler = signal.signal(signal.SIGALRM, lambda *_: os._exit(1))
        previous_alarm = signal.alarm(235)
    try:
        return _run(Path(data_root) if data_root is not None else DATA_ROOT)
    finally:
        if guarded:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_alarm:
                signal.alarm(previous_alarm)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    betterhacker_news_worker()
