import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.app import app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patcher = patch("app.app.DATA", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = TestClient(app)

    def write_snapshot(self, age=0):
        stamp = datetime.now(UTC) - timedelta(seconds=age)
        (self.root / "current.json").write_text(json.dumps({"generated_at": stamp.isoformat(), "feeds": {"top": []}}))

    def test_freshness_and_failure_health(self):
        self.assertEqual(self.client.get("/healthz").status_code, 503)
        self.write_snapshot()
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/status").json()["refresh_interval_seconds"], 300)
        self.write_snapshot(1000)
        self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_trust_boundaries(self):
        response = self.client.get("/api/status")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/api/status", headers={"host": "attacker.example"}).status_code, 400)
        self.assertEqual(self.client.get("/media/-1.webp").status_code, 404)
        self.assertEqual(self.client.get("/.env").status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_failed_feed_does_not_claim_fresh(self):
        self.write_snapshot()
        data = json.loads((self.root / "current.json").read_text())
        data["feed_generated_at"] = {
            feed: data["generated_at"] for feed in ["top", "new", "best", "show", "ask", "jobs"]
        }
        data["feed_generated_at"]["top"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        (self.root / "current.json").write_text(json.dumps(data))
        state = self.client.get("/api/status").json()
        self.assertEqual(state["stale_feeds"], ["top"])
        self.assertEqual(self.client.get("/healthz").status_code, 503)


if __name__ == "__main__":
    unittest.main()
