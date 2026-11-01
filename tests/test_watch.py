"""Pure-function tests for download watch. No running stack required."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.watch import (
    STALL, GRACE,
    classify_download,
    cutoff_only,
    first_missing_episode,
    is_healthier,
    is_junk,
    pick_release,
    quality_allowed,
    resolution_of,
    season_numbers,
)


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def rel(title, seeders, resolution=1080, rejected=False, rejections=None, leechers=0):
    return {
        "protocol": "torrent",
        "title": title,
        "seeders": seeders,
        "leechers": leechers,
        "rejected": rejected,
        "rejections": rejections or [],
        "guid": title,
        "indexerId": 1,
        "quality": {"quality": {"name": f"WEBDL-{resolution}p", "resolution": resolution}},
    }


class JunkAndQuality(unittest.TestCase):
    def test_junk_titles(self):
        self.assertTrue(is_junk("Movie.2024.CAM.mkv"))
        self.assertTrue(is_junk("Show.S01E01.TELESYNC.x264"))
        self.assertTrue(is_junk("Film.SCREENER.1080p"))
        self.assertFalse(is_junk("Movie.2024.1080p.WEB.H264"))
        self.assertFalse(is_junk("The.Script.2024.1080p"))  # SCR as a substring of Script? \bSCR\b no

    def test_resolution_from_arr_object(self):
        self.assertEqual(resolution_of({"quality": {"name": "HDTV-1080p", "resolution": 1080}}), 1080)
        self.assertEqual(resolution_of({"quality": {"name": "Bluray-2160p"}}), 2160)
        self.assertIsNone(resolution_of({}))

    def test_one_step_down_only(self):
        self.assertTrue(quality_allowed(2160, 2160))
        self.assertTrue(quality_allowed(2160, 1080))
        self.assertFalse(quality_allowed(2160, 720))
        self.assertTrue(quality_allowed(1080, 2160))
        self.assertTrue(quality_allowed(1080, 720))
        self.assertFalse(quality_allowed(1080, 480))
        self.assertTrue(quality_allowed(None, 720))
        self.assertFalse(quality_allowed(1080, None))

    def test_cutoff_only_rejections(self):
        self.assertTrue(cutoff_only(["Cutoff not met: WEBDL-1080p"]))
        self.assertFalse(cutoff_only(["Cutoff not met", "Wrong language"]))
        self.assertTrue(cutoff_only([]))


class Scoring(unittest.TestCase):
    def test_dead_needs_three_seeders(self):
        self.assertTrue(is_healthier(0, 3, True))
        self.assertFalse(is_healthier(0, 2, True))

    def test_stalled_needs_double_seeders(self):
        self.assertTrue(is_healthier(5, 10, False))
        self.assertFalse(is_healthier(5, 9, False))

    def test_pick_skips_junk_and_same_title(self):
        releases = [
            rel("Show.S01E01.CAM", 99),
            rel("Show.S01E01.1080p.WEB", 12, 1080),
            rel("Show.S01E01.2160p.BluRay", 1, 2160),
        ]
        winner = pick_release(
            releases, current_resolution=1080, current_seeders=0, current_dead=True,
            current_title="Show.S01E01.1080p.WEB",
        )
        self.assertIsNone(winner)  # only healthy non-junk is the current title

    def test_pick_drops_one_step_when_healthier(self):
        releases = [
            rel("Movie.2160p.BluRay", 1, 2160),
            rel("Movie.1080p.WEB", 40, 1080),
            rel("Movie.720p.WEB", 80, 720),
        ]
        winner = pick_release(
            releases, current_resolution=2160, current_seeders=1, current_dead=True,
        )
        self.assertEqual(winner["title"], "Movie.1080p.WEB")

    def test_pick_will_not_drop_two_steps(self):
        releases = [rel("Movie.720p.WEB", 80, 720)]
        winner = pick_release(
            releases, current_resolution=2160, current_seeders=0, current_dead=True,
        )
        self.assertIsNone(winner)

    def test_pick_allows_cutoff_reject_when_dropping(self):
        releases = [rel(
            "Movie.1080p.WEB", 40, 1080,
            rejected=True, rejections=["Cutoff not met: WEBDL-1080p"],
        )]
        winner = pick_release(
            releases, current_resolution=2160, current_seeders=0, current_dead=True,
        )
        self.assertEqual(winner["title"], "Movie.1080p.WEB")

    def test_pick_skips_other_rejects(self):
        releases = [rel(
            "Movie.1080p.WEB", 40, 1080,
            rejected=True, rejections=["Must contain: English"],
        )]
        winner = pick_release(
            releases, current_resolution=1080, current_seeders=0, current_dead=True,
        )
        self.assertIsNone(winner)


class Classify(unittest.TestCase):
    def test_paused_left_alone(self):
        self.assertEqual(classify_download(
            qbit_state="pausedDL", tracked_status="ok", tracked_state="downloadPaused",
            dlspeed=0, percent=10, added_at=NOW - timedelta(hours=3),
            last_progress_at=NOW - timedelta(hours=3), now=NOW,
        ), "paused")

    def test_qbit5_stopped_is_paused(self):
        self.assertEqual(classify_download(
            qbit_state="stoppedDL", tracked_status="ok", tracked_state="downloading",
            dlspeed=0, percent=10, added_at=NOW - timedelta(hours=3),
            last_progress_at=NOW - timedelta(hours=3), now=NOW,
        ), "paused")

    def test_queued_is_waiting_not_stalled(self):
        self.assertEqual(classify_download(
            qbit_state="queuedDL", tracked_status="ok", tracked_state="downloading",
            dlspeed=0, percent=0, added_at=NOW - timedelta(hours=3),
            last_progress_at=NOW - timedelta(hours=3), now=NOW,
        ), "waiting")

    def test_grace_on_new_torrent(self):
        self.assertEqual(classify_download(
            qbit_state="stalledDL", tracked_status="ok", tracked_state="downloading",
            dlspeed=0, percent=0, added_at=NOW - timedelta(minutes=10),
            last_progress_at=NOW - timedelta(minutes=10), now=NOW,
        ), "grace")
        self.assertEqual(GRACE, timedelta(minutes=30))

    def test_error_skips_grace(self):
        self.assertEqual(classify_download(
            qbit_state="error", tracked_status="error", tracked_state="downloadFailed",
            dlspeed=0, percent=0, added_at=NOW - timedelta(minutes=2),
            last_progress_at=NOW - timedelta(minutes=2), now=NOW,
        ), "error")

    def test_stall_after_forty_minutes(self):
        self.assertEqual(STALL, timedelta(minutes=40))
        self.assertEqual(classify_download(
            qbit_state="stalledDL", tracked_status="ok", tracked_state="downloading",
            dlspeed=0, percent=10, added_at=NOW - timedelta(hours=2),
            last_progress_at=NOW - timedelta(minutes=39), now=NOW,
        ), "progressing")
        self.assertEqual(classify_download(
            qbit_state="stalledDL", tracked_status="ok", tracked_state="downloading",
            dlspeed=0, percent=10, added_at=NOW - timedelta(hours=2),
            last_progress_at=NOW - timedelta(minutes=40), now=NOW,
        ), "stalled")

    def test_almost_done_stall(self):
        self.assertEqual(classify_download(
            qbit_state="stalledDL", tracked_status="ok", tracked_state="downloading",
            dlspeed=0, percent=85, added_at=NOW - timedelta(hours=2),
            last_progress_at=NOW - timedelta(minutes=40), now=NOW,
        ), "almost_done_stalled")

    def test_slow_but_moving_is_progressing(self):
        self.assertEqual(classify_download(
            qbit_state="downloading", tracked_status="ok", tracked_state="downloading",
            dlspeed=50_000, percent=10, added_at=NOW - timedelta(hours=2),
            last_progress_at=NOW - timedelta(hours=1), now=NOW,
        ), "progressing")


class Episodes(unittest.TestCase):
    def test_season_numbers(self):
        self.assertEqual(season_numbers([{"seasonNumber": 1}, {"seasonNumber": 2}]), [1, 2])
        self.assertEqual(season_numbers([1, 2]), [1, 2])
        self.assertIsNone(season_numbers([]))

    def test_first_missing_skips_future_and_owned(self):
        eps = [
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True,
             "airDateUtc": "2020-01-01T00:00:00Z"},
            {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False,
             "airDateUtc": "2020-01-08T00:00:00Z"},
            {"id": 3, "seasonNumber": 1, "episodeNumber": 3, "monitored": True, "hasFile": False,
             "airDateUtc": "2099-01-01T00:00:00Z"},
        ]
        got = first_missing_episode(eps, seasons=[1], queued_ids=set(), now=NOW)
        self.assertEqual(got["id"], 2)
        got = first_missing_episode(eps, seasons=[1], queued_ids={2}, now=NOW)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
