import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import worker
from app import previews


def item(story_id, **changes):
    return {
        "id": story_id,
        "type": "story",
        "title": "Original HN title & punctuation",
        "url": "https://example.com/article",
        "score": 11,
        "descendants": 4,
        "by": "author",
        "time": 123,
        **changes,
    }


class PreviewTests(unittest.TestCase):
    def test_unsafe_destinations(self):
        urls = [
            "file:///etc/passwd",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://169.254.169.254/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[fc00::1]/",
            "http://[::ffff:8.8.8.8]/",
            "http://[2001:db8::1]/",
            "http://[2002:0808:0808::1]/",
            "http://224.0.0.1/",
            "https://user:pass@example.com/",
            "https://example.com:8443/",
            "https://example.com\\@localhost/",
            "http://localhost/",
            "http://service.internal/",
            "https://example.com/\r\nHost: localhost",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(previews.UnsafeURL):
                previews.validate_url(url)

    def test_all_dns_answers_must_be_public(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443)),
        ]
        with patch.object(socket, "getaddrinfo", return_value=answers), self.assertRaises(previews.UnsafeURL):
            previews.resolve_public("example.com", 443)

    def test_redirect_revalidates_before_connecting(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.getheader.return_value = "http://169.254.169.254/latest/meta-data"
        with (
            patch.object(previews, "resolve_public", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]) as dns,
            patch.object(previews, "PinnedHTTPConnection", return_value=connection) as factory,
            self.assertRaises(previews.UnsafeURL),
        ):
            previews.fetch_public("https://example.com", 512, {"text/html"})
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(dns.call_count, 1)
        connection.close.assert_called_once()

    def test_redirect_hostname_resolves_again_and_blocks_private_dns(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 307
        connection.getresponse.return_value.getheader.return_value = "https://other.com/image"
        with (
            patch.object(
                previews,
                "resolve_public",
                side_effect=[[(socket.AF_INET, ("8.8.8.8", 443))], previews.UnsafeURL("private DNS")],
            ) as dns,
            patch.object(previews, "PinnedHTTPConnection", return_value=connection) as factory,
            self.assertRaises(previews.UnsafeURL),
        ):
            previews.fetch_public("https://example.com", 512, {"text/html"})
        self.assertEqual(dns.call_args_list[1].args, ("other.com", 443))
        self.assertEqual(factory.call_count, 1)

    def test_connection_pins_address_and_tls_hostname(self):
        sock = MagicMock()
        context = MagicMock()
        address = (socket.AF_INET, ("8.8.8.8", 443))
        with (
            patch.object(socket, "socket", return_value=sock),
            patch.object(socket, "getaddrinfo") as dns,
            patch.object(previews.ssl, "create_default_context", return_value=context),
        ):
            connection = previews.PinnedHTTPConnection("example.com", 443, address, 2, tls=True)
            connection.connect()
        sock.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(sock, server_hostname="example.com")
        dns.assert_not_called()

    def test_closed_connection_transport_still_reads_response(self):
        transport = MagicMock()
        transport.settimeout.side_effect = OSError(9, "Bad file descriptor")
        connection = MagicMock()
        connection.sock = transport
        response = MagicMock()
        response.status = 200
        response.getheader.side_effect = lambda name, default=None: {"Content-Type": "application/json"}.get(
            name, default
        )
        response.read1.side_effect = [b"[1,2]", b""]

        def getresponse():
            connection.sock = None
            return response

        connection.getresponse.side_effect = getresponse
        with (
            patch.object(previews, "resolve_public", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]),
            patch.object(previews, "PinnedHTTPConnection", return_value=connection),
        ):
            data, _ = previews.fetch_public("https://example.com/feed", 512, {"application/json"})
        self.assertEqual(data, b"[1,2]")
        transport.settimeout.assert_not_called()

    def test_metadata_parse_relative_and_entities(self):
        html = b'<meta content="/cover.png?a=1&amp;b=2" PROPERTY="og:image"><meta property="og:image" content="/later">'
        self.assertEqual(
            previews.parse_og_image(html, "https://example.com/article"), "https://example.com/cover.png?a=1&b=2"
        )
        self.assertIsNone(previews.parse_og_image("<p>No image</p>", "https://example.com"))
        with self.assertRaises(previews.UnsafeURL):
            previews.parse_og_image('<meta property="og:image" content="http://[::1]/">', "https://example.com")

    def test_response_type_size_and_encoding_limits(self):
        for headers, chunks in [
            ({"Content-Type": "image/svg+xml"}, []),
            ({"Content-Type": "text/html", "Content-Length": "513"}, []),
            ({"Content-Type": "text/html", "Content-Encoding": "gzip"}, []),
            ({"Content-Type": "text/html"}, [b"a" * 513]),
        ]:
            response = MagicMock()
            response.status = 200
            response.getheader.side_effect = lambda name, default=None, headers=headers: headers.get(name, default)
            response.read1.side_effect = chunks
            connection = MagicMock()
            connection.sock = None
            connection.getresponse.return_value = response
            with (
                self.subTest(headers=headers),
                patch.object(previews, "resolve_public", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]),
                patch.object(previews, "PinnedHTTPConnection", return_value=connection),
                self.assertRaises(ValueError),
            ):
                previews.fetch_public("https://example.com", 512, {"text/html"})

    def test_preview_raster_decode_and_dimension_bound(self):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(buffer, format="PNG")
        html = b'<meta property="og:image" content="/image.png">'
        responses = [(html, "https://example.com/article"), (buffer.getvalue(), "https://example.com/image.png")]
        with patch.object(previews, "fetch_public", side_effect=responses):
            self.assertEqual(previews.article_preview("https://example.com").size, previews.SIZE)
        with (
            patch.object(previews, "fetch_public", side_effect=responses),
            patch.object(previews, "PIXEL_LIMIT", 100),
            self.assertRaises(ValueError),
        ):
            previews.article_preview("https://example.com")
        with (
            patch.object(previews, "fetch_public", side_effect=[responses[0], (b"<svg></svg>", responses[1][1])]),
            self.assertRaises(OSError),
        ):
            previews.article_preview("https://example.com")
        with (
            patch.object(previews, "fetch_public", side_effect=responses),
            patch.object(Image, "open", side_effect=Image.DecompressionBombError),
            self.assertRaises(ValueError),
        ):
            previews.article_preview("https://example.com")

    def test_editorial_fallback_is_deterministic_webp(self):
        story = worker.story_from_item(item(1), 1)
        image = previews.editorial_thumbnail(story)
        self.assertEqual(image.tobytes(), previews.editorial_thumbnail(story).tobytes())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media" / "1.webp"
            previews.save_thumbnail(path, image)
            with Image.open(path) as saved:
                self.assertEqual(saved.format, "WEBP")
                self.assertEqual(saved.size, previews.SIZE)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_atomic_write_failure_retains_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            path.write_bytes(b"previous snapshot")
            with patch.object(previews.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                previews.atomic_write(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"previous snapshot")
            self.assertEqual(list(path.parent.iterdir()), [path])


class WorkerTests(unittest.TestCase):
    def test_malformed_image_syntax_error_is_a_failed_attempt(self):
        def malformed(_):
            raise SyntaxError("Malformed PNG")

        results = worker.bounded_results([1], malformed, 1, time.monotonic() + 2)
        self.assertEqual(results, {1: (False, None)})

    def source(self, url, deadline):
        if "/item/" in url:
            story_id = int(url.rsplit("/", 1)[1].split(".")[0])
            return item(story_id, type="job" if story_id == 41 else "story")
        return list(range(1, 51)) if "jobstories" not in url else [41]

    def test_feed_limit_dedup_original_metadata_and_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(worker, "fetch_json", side_effect=self.source) as source,
                patch.object(worker, "article_preview", side_effect=ValueError("No image")),
            ):
                snapshot = worker.betterhacker_news_worker(directory)
            self.assertEqual(set(snapshot["feeds"]), set(worker.FEEDS))
            self.assertEqual(len(snapshot["feeds"]["top"]), 40)
            self.assertEqual(len([call for call in source.call_args_list if "/item/" in call.args[0]]), 41)
            story = snapshot["feeds"]["top"][0]
            self.assertEqual(
                story,
                {
                    "id": 1,
                    "title": item(1)["title"],
                    "url": item(1)["url"],
                    "domain": "example.com",
                    "score": 11,
                    "descendants": 4,
                    "by": "author",
                    "time": 123,
                    "topic": "Technology",
                    "rank": 1,
                    "thumbnail": "/media/1.webp",
                    "image_kind": "generated",
                },
            )
            self.assertTrue(snapshot["generated_at"].endswith("+00:00"))
            self.assertTrue(all(value.endswith("+00:00") for value in snapshot["feed_generated_at"].values()))
            self.assertEqual(snapshot["thumbnail_stats"]["generated"], 41)
            self.assertEqual(json.loads((Path(directory) / "current.json").read_text()), snapshot)
            self.assertEqual(len(list((Path(directory) / "media").glob("*.webp"))), 41)

    def test_failed_feed_preserved_and_dead_deleted_dropped(self):
        old = {
            "generated_at": "previous",
            "feeds": {name: [worker.story_from_item(item(1), 1)] for name in worker.FEEDS},
        }

        def source(url, deadline):
            if "topstories" in url:
                raise OSError("feed down")
            if "/item/" in url:
                story_id = int(url.rsplit("/", 1)[1].split(".")[0])
                return item(story_id, dead=story_id == 2, deleted=story_id == 3)
            return [2, 3]

        with tempfile.TemporaryDirectory() as directory:
            worker.write_json(Path(directory) / "current.json", old)
            with (
                patch.object(worker, "fetch_json", side_effect=source),
                patch.object(worker, "article_preview", side_effect=ValueError("No image")),
            ):
                snapshot = worker.betterhacker_news_worker(directory)
            self.assertEqual([story["id"] for story in snapshot["feeds"]["top"]], [1])
            self.assertEqual(snapshot["feed_generated_at"]["top"], "previous")
            self.assertNotEqual(snapshot["feed_generated_at"]["new"], "previous")
            self.assertEqual(snapshot["feeds"]["new"], [])

    def test_all_feed_failures_leave_snapshot_bytes_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            previous = {"generated_at": "old", "feeds": {"top": []}}
            worker.write_json(path, previous)
            before = path.read_bytes()
            with patch.object(worker, "fetch_json", side_effect=TimeoutError):
                self.assertEqual(worker.betterhacker_news_worker(directory), previous)
            self.assertEqual(path.read_bytes(), before)

    def test_item_failure_keeps_previous_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            old = {"feeds": {name: [worker.story_from_item(item(1), 1)] for name in worker.FEEDS}}
            worker.write_json(Path(directory) / "current.json", old)

            def source(url, deadline):
                if "/item/" in url:
                    raise OSError("item unavailable")
                return [1]

            with (
                patch.object(worker, "fetch_json", side_effect=source),
                patch.object(worker, "article_preview", side_effect=ValueError),
            ):
                snapshot = worker.betterhacker_news_worker(directory)
            self.assertEqual(snapshot["feeds"]["top"][0]["score"], 11)

    def test_failed_snapshot_publication_retains_last_good_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            worker.write_json(path, {"generated_at": "old", "feeds": {}})
            before = path.read_bytes()
            original_replace = previews.os.replace

            def replace(source, destination):
                if Path(destination) == path:
                    raise OSError("snapshot publication failed")
                return original_replace(source, destination)

            def source(url, deadline):
                return item(1) if "/item/" in url else [1]

            with (
                patch.object(worker, "fetch_json", side_effect=source),
                patch.object(worker, "article_preview", side_effect=ValueError),
                patch.object(previews.os, "replace", side_effect=replace),
                self.assertRaises(OSError),
            ):
                worker.betterhacker_news_worker(directory)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(path.parent.glob(".current.json.*")))

    def test_unsafe_story_url_falls_back_to_discussion(self):
        story = worker.story_from_item(item(17, url="http://169.254.169.254"), 2)
        self.assertEqual(story["url"], "https://news.ycombinator.com/item?id=17")
        self.assertEqual(story["domain"], "news.ycombinator.com")
        self.assertIsNone(worker.story_from_item(item(17, deleted=True), 2))

    def test_retry_rotation_and_preview_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            stories = [worker.story_from_item(item(story_id), 1) for story_id in range(1, 5)]
            with (
                patch.object(worker, "PREVIEW_RETRIES", 2),
                patch.object(worker, "article_preview", side_effect=ValueError) as fetch,
            ):
                worker.refresh_thumbnails(stories, Path(directory), time.monotonic() + 10, 100)
                first = {call.args[0] for call in fetch.call_args_list}
                fetch.reset_mock()
                worker.refresh_thumbnails(stories, Path(directory), time.monotonic() + 10, 200)
                self.assertEqual(fetch.call_count, 2)
                cache = worker.read_json(Path(directory) / "thumbnail-cache.json", {})
                self.assertEqual([cache[str(i)]["attempted_at"] for i in range(1, 5)], [100, 100, 200, 200])
                self.assertTrue(first)
            with patch.object(worker, "article_preview", return_value=Image.new("RGB", previews.SIZE)) as fetch:
                worker.refresh_thumbnails(stories, Path(directory), time.monotonic() + 10, 300)
                self.assertEqual(fetch.call_count, 4)
                fetch.reset_mock()
                worker.refresh_thumbnails(stories, Path(directory), time.monotonic() + 10, 600)
                fetch.assert_not_called()
                worker.refresh_thumbnails(stories, Path(directory), time.monotonic() + 10, 300 + worker.PREVIEW_TTL)
                self.assertEqual(fetch.call_count, 4)

    def test_item_concurrency_bound(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def fetch(key):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.005)
            with lock:
                active -= 1
            return key

        results = worker.bounded_results(range(60), fetch, worker.ITEM_CONCURRENCY, time.monotonic() + 10)
        self.assertEqual(len(results), 60)
        self.assertLessEqual(peak, 12)
        self.assertGreater(peak, 1)


if __name__ == "__main__":
    unittest.main()
