"""Bazarr snapshot is defensive: no key or a dead service must not raise."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import bazarr_snapshot


class FakeClient:
    def __init__(self, status=200, payload=None, error=None):
        self.status = status
        self.payload = payload or {}
        self.error = error

    async def get(self, url, headers=None):
        if self.error:
            raise self.error
        class R:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")
            def json(self):
                return self._payload
        return R(self.status, self.payload)


class BazarrSnapshotTests(unittest.TestCase):
    def test_missing_key_is_down_not_an_exception(self):
        async def run():
            with patch("app.services.config.BAZARR_API_KEY", ""):
                with patch("app.services.config.BAZARR_URL", "http://localhost:6767"):
                    return await bazarr_snapshot(FakeClient())
        import asyncio
        out = asyncio.run(run())
        self.assertFalse(out["service"]["up"])
        self.assertIn("API key", out["service"]["error"])

    def test_up_from_ping(self):
        async def run():
            with patch("app.services.config.BAZARR_API_KEY", "abc"):
                with patch("app.services.config.BAZARR_URL", "http://localhost:6767"):
                    return await bazarr_snapshot(
                        FakeClient(payload={"status": "OK"})
                    )
        import asyncio
        out = asyncio.run(run())
        self.assertTrue(out["service"]["up"])
        # Liveness comes from the anonymous ping, which carries no version.
        self.assertIsNone(out["service"]["version"])
        self.assertEqual(out["service"]["name"], "Bazarr")


if __name__ == "__main__":
    unittest.main()
