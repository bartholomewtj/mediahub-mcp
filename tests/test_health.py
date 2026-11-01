"""Pure-function tests for release health / ranking. No running stack required."""

from __future__ import annotations

import unittest

from app.health import (
    DONE_STATES,
    is_junk,
    pick_release,
    pick_scored,
    rank_releases,
    reject_reason,
    score,
    swarm_dead,
    track_record,
)


GB = 1_000_000_000
MB = 1_000_000

INDEXER_C = "IndexerC (Prowlarr)"
INDEXER_A = "IndexerA (Prowlarr)"
INDEXER_F = "FlareIndexer (Prowlarr)"

# Shaped like real Sonarr history: (imported, failed).
HISTORY = {INDEXER_C: (6, 19), INDEXER_A: (135, 7), INDEXER_F: (12, 0)}


def rel(title, seeders=20, resolution=1080, *, leechers=0, indexer=INDEXER_A, size=None,
        protocol="torrent", rejected=False, rejections=None, languages=None,
        full_season=None):
    out = {
        "protocol": protocol,
        "title": title,
        "seeders": seeders,
        "leechers": leechers,
        "rejected": rejected,
        "rejections": rejections or [],
        "guid": title,
        "indexerId": 1,
        "indexer": indexer,
        "quality": {"quality": {"name": f"WEBDL-{resolution}p", "resolution": resolution}},
    }
    if size is not None:
        out["size"] = size
    if languages is not None:
        out["languages"] = [{"name": n} for n in languages]
    if full_season is not None:
        out["fullSeason"] = full_season
    return out


def reason(r, **kw):
    kw.setdefault("runtime_minutes", None)
    return reject_reason(r, **kw)


class RejectBasics(unittest.TestCase):
    def test_clean_release_passes(self):
        self.assertIsNone(reason(rel("Show.S01E01.1080p.WEB.H264-GRP", languages=["English"])))

    def test_usenet_rejected(self):
        self.assertEqual(reason(rel("Show.S01E01.1080p.WEB", protocol="usenet")), "not a torrent")
        self.assertEqual(reason(rel("Show.S01E01.1080p.WEB", protocol=None)), "not a torrent")

    def test_junk(self):
        self.assertEqual(reason(rel("Movie.2024.CAM.x264")), "cam/telesync/screener")
        self.assertEqual(reason(rel("Movie.2024.HDTS.TS.x264")), "cam/telesync/screener")


class RejectDangerousFiles(unittest.TestCase):
    def test_extensions_rejected(self):
        for title in (
            "Movie.2024.1080p.WEB.exe",
            "Movie 2024 1080p WEBRip x264.mkv.EXE",
            "Movie.2024.1080p.WEB.rar",
            "Movie.2024.1080p.WEB.part1.RAR",
            "[GRP] Movie (2024) 1080p.zip",
            "Movie.2024.1080p.iso",
            "Movie.2024.1080p.msi",
            "Movie.2024.1080p.WEB.lnk",
            "Movie.2024.1080p.bat",
            "Movie.2024.1080p.cmd",
        ):
            with self.subTest(title=title):
                self.assertEqual(reason(rel(title)), "archive or executable")

    def test_scr_extension_caught_earlier_as_screener(self):
        # SCR is also a junk tag, which comes first in the rule order.
        self.assertEqual(reason(rel("Movie.2024.1080p.scr")), "cam/telesync/screener")

    def test_words_containing_extensions_pass(self):
        for title in (
            "My.Little.Pony.Rarity.Special.1080p.WEB",
            "Disclosure.2020.1080p.WEB",
            "Execution.Day.2024.1080p.WEB",
            "Zipper.2015.1080p.BluRay",
            "Batman.Begins.2005.1080p.BluRay",
            "Isolation.2024.1080p.WEB",
            "The.Commander.2024.1080p.WEB",
        ):
            with self.subTest(title=title):
                self.assertIsNone(reason(rel(title)))
        self.assertFalse(is_junk("Disclosure.2020.1080p.WEB"))


class RejectForeign(unittest.TestCase):
    def test_foreign_tokens(self):
        for title in (
            "Movie.2024.MULTi.1080p.WEB",
            "Movie.2024.MULTI.1080p.WEB",
            "Movie 2024 ITA 1080p",
            "Movie.2024.FRENCH.1080p.WEB",
            "Movie.2024.VFF.1080p",
            "Movie.2024.VOSTFR.1080p",
            "Movie.2024.GERMAN.DL.1080p",
            "Movie.2024.SPANISH.1080p",
            "Movie.2024.LATINO.1080p",
            "Movie.2024.RUS.1080p",
            "Movie.2024.HINDI.1080p",
            "Movie.2024.DUBBED.1080p",
            "Movie.2024.DUAL.1080p",
        ):
            with self.subTest(title=title):
                self.assertEqual(reason(rel(title)), "foreign language")

    def test_english_track_makes_multi_audio_ok(self):
        # "ITA ENG" / "MULTi English" carry an English track; Northern Line's
        # only healthy releases look like this (2026-10-31).
        for title in ("Movie.2024.iTA.ENG.1080p.WEB", "Show S01E01 ITA ENG 1080p WEB-DLMux",
                      "Movie.2024.MULTi.English.1080p"):
            with self.subTest(title=title):
                self.assertIsNone(reason(rel(title)))

    def test_title_words_are_not_language_tags(self):
        for title in (
            "The.Italian.Job.2003.1080p.BluRay",
            "The.Italian.Job.2003.1080p.BluRay.ITALIAN-free",  # ITALIAN is not ITA
            "The.French.Dispatch.2021.1080p.WEB",
            "The.Spanish.Prisoner.1997.1080p",
            "The.Good.German.2006.1080p",
            "Russian.Doll.S01E01.1080p",
            "Multiverse.2024.1080p",
            "Dual.Spires.2024.1080p",
        ):
            with self.subTest(title=title):
                self.assertIsNone(reason(rel(title)))

    def test_languages_list(self):
        self.assertEqual(reason(rel("Movie.2024.1080p", languages=["French"])), "foreign language")
        self.assertEqual(
            reason(rel("Movie.2024.1080p", languages=["Unknown", "German"])), "foreign language",
        )
        self.assertIsNone(reason(rel("Movie.2024.1080p", languages=["English"])))
        self.assertIsNone(reason(rel("Movie.2024.1080p", languages=["French", "English"])))
        self.assertIsNone(reason(rel("Movie.2024.1080p", languages=["Unknown"])))
        self.assertIsNone(reason(rel("Movie.2024.1080p", languages=[])))
        self.assertIsNone(reason(rel("Movie.2024.1080p")))

    def test_allow_foreign(self):
        self.assertIsNone(reason(rel("Movie.2024.FRENCH.1080p"), allow_foreign=True))
        self.assertIsNone(reason(rel("Movie.2024.1080p", languages=["French"]), allow_foreign=True))


class RejectHardsubs(unittest.TestCase):
    def test_hardsub_tokens(self):
        for title in (
            "Movie.2024.1080p.HC.WEB",
            "Movie.2024.1080p.HDRip.HC",
            "Movie.2024.HARDSUB.1080p",
            "Movie.2024.HardSubs.1080p",
            "Movie.2024.KORSUB.1080p",
            "Movie.2024.korsub.1080p",
            "Movie.2024.HCSUB.1080p",
        ):
            with self.subTest(title=title):
                self.assertEqual(reason(rel(title)), "hardcoded subtitles")

    def test_hdr_and_friends_pass(self):
        for title in (
            "Movie.2024.2160p.HDR.WEB",
            "Movie.2024.1080p.HDRip",
            "Movie.2024.1080p.HEVC",
            "Movie.2024.1080p.WEB.H264-HCG",
            "Movie.2024.1080p.Subs",
        ):
            with self.subTest(title=title):
                self.assertIsNone(reason(rel(title, resolution=2160 if "2160" in title else 1080)))

    def test_allow_foreign_allows_hardsubs(self):
        self.assertIsNone(reason(rel("Movie.2024.HC.1080p"), allow_foreign=True))


class RejectSize(unittest.TestCase):
    def test_1080_bounds(self):
        # 45 min episode: 4..150 MB/min → 180 MB .. 6.75 GB.
        self.assertIsNone(reason(rel("Show.S01E01.1080p", size=int(1.5 * GB)), runtime_minutes=45))
        self.assertIsNone(reason(rel("Show.S01E01.1080p", size=180 * MB), runtime_minutes=45))
        self.assertEqual(
            reason(rel("Show.S01E01.1080p", size=100 * MB), runtime_minutes=45),
            "size implausible for runtime",
        )
        self.assertEqual(
            reason(rel("Show.S01E01.1080p", size=10 * GB), runtime_minutes=45),
            "size implausible for runtime",
        )

    def test_2160_bounds(self):
        self.assertIsNone(reason(rel("Movie.2160p", resolution=2160, size=20 * GB), runtime_minutes=120))
        self.assertEqual(
            reason(rel("Movie.2160p", resolution=2160, size=1 * GB), runtime_minutes=120),
            "size implausible for runtime",  # 8.3 MB/min < 15
        )
        self.assertEqual(
            reason(rel("Movie.2160p", resolution=2160, size=60 * GB), runtime_minutes=120),
            "size implausible for runtime",  # 500 MB/min > 400
        )

    def test_720_and_480_bounds(self):
        self.assertIsNone(reason(rel("Show.720p", resolution=720, size=int(1.5 * GB)), runtime_minutes=45))
        self.assertEqual(
            reason(rel("Show.720p", resolution=720, size=3 * GB), runtime_minutes=45),
            "size implausible for runtime",  # 66 MB/min > 60
        )
        self.assertIsNone(reason(rel("Show.480p", resolution=480, size=300 * MB), runtime_minutes=30))
        self.assertEqual(
            reason(rel("Show.480p", resolution=480, size=1 * GB), runtime_minutes=30),
            "size implausible for runtime",  # 33 MB/min > 30
        )
        self.assertEqual(
            reason(rel("Show.480p", resolution=480, size=20 * MB), runtime_minutes=30),
            "size implausible for runtime",  # 0.67 MB/min < 0.8
        )

    def test_unknown_resolution_uses_1080_bounds(self):
        r = rel("Show.S01E01", size=100 * MB)
        r["quality"] = {"quality": {"name": "Unknown"}}
        self.assertEqual(reason(r, runtime_minutes=45), "size implausible for runtime")
        r["size"] = int(1.5 * GB)
        self.assertIsNone(reason(r, runtime_minutes=45))

    def test_season_pack_divided_by_episode_count(self):
        pack = rel("Show.S01.1080p.WEB", size=15 * GB, full_season=True)
        self.assertIsNone(reason(pack, runtime_minutes=45, episode_count=10))
        self.assertEqual(
            reason(pack, runtime_minutes=45, episode_count=1),
            "size implausible for runtime",  # 333 MB/min as a single episode
        )
        tiny_pack = rel("Show.S01.1080p.WEB", size=500 * MB, full_season=True)
        self.assertEqual(
            reason(tiny_pack, runtime_minutes=45, episode_count=10),
            "size implausible for runtime",  # 1.1 MB/min per episode
        )

    def test_unknown_size_or_runtime_skips_check(self):
        self.assertIsNone(reason(rel("Show.1080p", size=10 * GB), runtime_minutes=None))
        self.assertIsNone(reason(rel("Show.1080p"), runtime_minutes=45))
        self.assertIsNone(reason(rel("Show.1080p", size=0), runtime_minutes=45))


class TrackRecord(unittest.TestCase):
    def test_smoothed_rate(self):
        # (6 + 0.8*5) / (25 + 5) = 10/30
        self.assertAlmostEqual(track_record(HISTORY, INDEXER_C), 10 / 30)
        # (135 + 4) / (142 + 5)
        self.assertAlmostEqual(track_record(HISTORY, INDEXER_A), 139 / 147)
        # (12 + 4) / (12 + 5)
        self.assertAlmostEqual(track_record(HISTORY, INDEXER_F), 16 / 17)

    def test_min_grabs_falls_back_to_prior(self):
        h = {"Small": (3, 1)}
        self.assertEqual(track_record(h, "Small"), 0.8)
        self.assertEqual(track_record(h, "Small", prior=0.5), 0.5)
        self.assertAlmostEqual(track_record(h, "Small", min_grabs=4), (3 + 4) / (4 + 5))

    def test_unknown_indexer_gets_prior(self):
        self.assertEqual(track_record(HISTORY, None), 0.8)
        self.assertEqual(track_record(HISTORY, ""), 0.8)
        self.assertEqual(track_record(HISTORY, "IndexerZ"), 0.8)
        self.assertEqual(track_record({}, INDEXER_A), 0.8)

    def test_prowlarr_suffix_and_case(self):
        want = 139 / 147
        self.assertAlmostEqual(track_record(HISTORY, "IndexerA"), want)
        self.assertAlmostEqual(track_record(HISTORY, "indexera"), want)
        self.assertAlmostEqual(track_record(HISTORY, "INDEXERA (prowlarr)"), want)
        self.assertAlmostEqual(track_record({"indexera": (135, 7)}, "IndexerA (Prowlarr)"), want)

    def test_same_indexer_under_two_spellings_is_summed(self):
        h = {"IndexerA (Prowlarr)": (3, 0), "IndexerA": (3, 0)}
        self.assertAlmostEqual(track_record(h, "IndexerA"), (6 + 4) / (6 + 5))


class Ranking(unittest.TestCase):
    def kw(self, **over):
        base = dict(
            history_counts=HISTORY, current_resolution=1080,
            current_seeders=0, current_dead=True,
        )
        base.update(over)
        return base

    def test_unreliable_indexer_ranks_below_trusted_one(self):
        slow = rel("Movie.2024.1080p.WEB-A", 200, indexer=INDEXER_C)
        good = rel("Movie.2024.1080p.WEB-B", 40, indexer="IndexerA")
        ranked = rank_releases([slow, good], **self.kw())
        self.assertEqual([r["release"]["title"] for r in ranked],
                         ["Movie.2024.1080p.WEB-B", "Movie.2024.1080p.WEB-A"])
        self.assertIs(pick_scored([slow, good], **self.kw()), good)
        # The old seeders-only picker would have taken the IndexerC one.
        self.assertIs(pick_release([slow, good], current_resolution=1080,
                                   current_seeders=0, current_dead=True), slow)

    def test_score_scales_with_track(self):
        r = rel("X", 10, leechers=5)
        self.assertEqual(score(r, 1.0), 105.0)
        self.assertLess(score(r, 0.5), score(r, 0.9))
        self.assertEqual(score(r, 0.0), 0.0)

    def test_result_shape_is_new_dict(self):
        r = rel("Show.S01E01.1080p.WEB", 20, size=int(1.5 * GB))
        out = rank_releases([r], **self.kw())
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertIs(item["release"], r)
        self.assertEqual(set(item), {"release", "score", "track", "resolution", "size_gb", "full_season"})
        self.assertIsInstance(item["score"], float)
        self.assertAlmostEqual(item["track"], 139 / 147)
        self.assertEqual(item["resolution"], 1080)
        self.assertEqual(item["size_gb"], 1.5)
        self.assertFalse(item["full_season"])
        self.assertNotIn("score", r)  # original untouched
        self.assertIsNone(rank_releases([rel("NoSize.1080p", 20)], **self.kw())[0]["size_gb"])

    def test_rules_from_pick_release_still_apply(self):
        releases = [
            rel("Show.S01E01.CAM", 99),
            rel("Show.S01E01.1080p.WEB", 50),                 # current title
            rel("Show.S01E01.720p.WEB", 50, 720),            # one step down: ok
            rel("Show.S01E01.480p.WEB", 90, 480),            # two steps: no
            rel("Show.S01E01.1080p.Zero", 0),
            rel("Show.S01E01.1080p.Two", 2),                 # < MIN_SEEDERS
            rel("Show.S01E01.1080p.Rej", 70, rejected=True, rejections=["Must contain: English"]),
            rel("Show.S01E01.1080p.Cut", 30, rejected=True, rejections=["Cutoff not met"]),
            rel("Show.S01E01.1080p.FRENCH", 80),
            rel("Show.S01E01.1080p.NZB", 80, protocol="usenet"),
        ]
        ranked = rank_releases(releases, **self.kw(current_title="show.s01e01.1080p.web"))
        self.assertEqual(
            [r["release"]["title"] for r in ranked],
            ["Show.S01E01.720p.WEB", "Show.S01E01.1080p.Cut"],
        )

    def test_healthier_than_current_required(self):
        releases = [rel("A.1080p", 9), rel("B.1080p", 10)]
        ranked = rank_releases(releases, **self.kw(current_seeders=5, current_dead=False))
        self.assertEqual([r["release"]["title"] for r in ranked], ["B.1080p"])

    def test_size_and_language_rules_applied(self):
        releases = [
            rel("Movie.1080p.Big", 90, size=40 * GB),
            rel("Movie.1080p.Fr", 80, languages=["French"]),
            rel("Movie.1080p.Ok", 10, size=4 * GB),
        ]
        ranked = rank_releases(releases, **self.kw(runtime_minutes=110))
        self.assertEqual([r["release"]["title"] for r in ranked], ["Movie.1080p.Ok"])
        ranked = rank_releases(releases, **self.kw(runtime_minutes=110, allow_foreign=True))
        self.assertEqual([r["release"]["title"] for r in ranked], ["Movie.1080p.Fr", "Movie.1080p.Ok"])

    def test_prefer_full_season(self):
        releases = [
            rel("Show.S01E01.1080p.A", 100, full_season=False),
            rel("Show.S01.1080p.Pack.Weak", 10, full_season=True),
            rel("Show.S01E01.1080p.B", 50),
            rel("Show.S01.1080p.Pack.Strong", 30, full_season=True),
        ]
        ranked = rank_releases(releases, **self.kw(prefer_full_season=True))
        self.assertEqual([r["release"]["title"] for r in ranked], [
            "Show.S01.1080p.Pack.Strong",
            "Show.S01.1080p.Pack.Weak",
            "Show.S01E01.1080p.A",
            "Show.S01E01.1080p.B",
        ])
        self.assertEqual([r["full_season"] for r in ranked], [True, True, False, False])
        ranked = rank_releases(releases, **self.kw())
        self.assertEqual(ranked[0]["release"]["title"], "Show.S01E01.1080p.A")

    def test_limit_and_empty(self):
        releases = [rel(f"R{i}.1080p", 10 + i) for i in range(5)]
        self.assertEqual(len(rank_releases(releases, **self.kw(limit=2))), 2)
        self.assertEqual(rank_releases(releases, **self.kw(limit=2))[0]["release"]["title"], "R4.1080p")
        self.assertEqual(rank_releases([], **self.kw()), [])
        self.assertIsNone(pick_scored([], **self.kw()))

    def test_ties_keep_search_order(self):
        releases = [rel("First.1080p", 20), rel("Second.1080p", 20)]
        self.assertEqual(pick_scored(releases, **self.kw())["title"], "First.1080p")


class PickReleaseUnchanged(unittest.TestCase):
    def test_drops_one_step_when_healthier(self):
        releases = [
            rel("Movie.2160p.BluRay", 1, 2160),
            rel("Movie.1080p.WEB", 40, 1080),
            rel("Movie.720p.WEB", 80, 720),
        ]
        winner = pick_release(releases, current_resolution=2160, current_seeders=1, current_dead=True)
        self.assertEqual(winner["title"], "Movie.1080p.WEB")

    def test_skips_junk_and_same_title(self):
        releases = [
            rel("Show.S01E01.CAM", 99),
            rel("Show.S01E01.1080p.WEB", 12, 1080),
            rel("Show.S01E01.2160p.BluRay", 1, 2160),
        ]
        winner = pick_release(
            releases, current_resolution=1080, current_seeders=0, current_dead=True,
            current_title="Show.S01E01.1080p.WEB",
        )
        self.assertIsNone(winner)

    def test_allows_cutoff_reject(self):
        releases = [rel("Movie.1080p.WEB", 40, rejected=True, rejections=["Cutoff not met: WEBDL-1080p"])]
        winner = pick_release(releases, current_resolution=2160, current_seeders=0, current_dead=True)
        self.assertEqual(winner["title"], "Movie.1080p.WEB")

    def test_old_picker_ignores_new_rules(self):
        # pick_release keeps its old behaviour: no language/indexer logic.
        releases = [rel("Movie.FRENCH.1080p", 40, indexer=INDEXER_C)]
        winner = pick_release(releases, current_resolution=1080, current_seeders=0, current_dead=True)
        self.assertEqual(winner["title"], "Movie.FRENCH.1080p")


T0 = 1_800_000_000.0


def tor(state="stalledDL", availability=0.4, num_seeds=0, progress=0.3, added_on=T0):
    return {
        "state": state, "availability": availability, "num_seeds": num_seeds,
        "progress": progress, "added_on": added_on,
    }


class SwarmDead(unittest.TestCase):
    def call(self, t, now, dead_since=None, first_seen=T0):
        return swarm_dead(t, now_ts=now, first_seen_ts=first_seen, dead_since_ts=dead_since)

    def test_grace_period(self):
        self.assertEqual(self.call(tor(), T0 + 599), (False, None))
        self.assertEqual(self.call(tor(), T0 + 600), (False, T0 + 600))

    def test_grace_counts_from_later_of_added_and_first_seen(self):
        # Old torrent, but the watcher only just saw it.
        t = tor(added_on=T0 - 86_400)
        self.assertEqual(self.call(t, T0 + 100), (False, None))
        # Watcher saw it long ago, but it was re-added just now.
        t = tor(added_on=T0 + 1000)
        self.assertEqual(self.call(t, T0 + 1100, first_seen=T0 - 86_400), (False, None))

    def test_done_paused_importing_not_judged(self):
        for state in ("uploading", "stalledUP", "pausedDL", "stoppedDL", "importing", "queuedDL"):
            with self.subTest(state=state):
                self.assertEqual(self.call(tor(state=state), T0 + 5000, dead_since=T0 + 1000), (False, None))
        self.assertIn("stalledUP", DONE_STATES)
        self.assertEqual(self.call(tor(progress=1.0), T0 + 5000, dead_since=T0 + 1000), (False, None))

    def test_unknown_availability_is_not_dead(self):
        self.assertEqual(self.call(tor(availability=-1), T0 + 5000, dead_since=T0 + 1000), (False, None))
        t = tor()
        del t["availability"]
        self.assertEqual(self.call(t, T0 + 5000), (False, None))
        t = tor()
        del t["num_seeds"]
        self.assertEqual(self.call(t, T0 + 5000), (False, None))

    def test_dead_after_window(self):
        dead, since = self.call(tor(), T0 + 1000)
        self.assertEqual((dead, since), (False, T0 + 1000))
        dead, since = self.call(tor(), T0 + 1599, dead_since=since)
        self.assertEqual((dead, since), (False, T0 + 1000))
        dead, since = self.call(tor(), T0 + 1600, dead_since=since)
        self.assertEqual((dead, since), (True, T0 + 1000))

    def test_metadata_stuck_can_die(self):
        self.assertEqual(self.call(tor(state="metaDL", availability=0), T0 + 2000, dead_since=T0 + 1000),
                         (True, T0 + 1000))

    def test_any_life_resets_clock(self):
        # A connected seed.
        self.assertEqual(self.call(tor(num_seeds=1), T0 + 5000, dead_since=T0 + 1000), (False, None))
        # Full copy reachable through peers.
        self.assertEqual(self.call(tor(availability=1.0), T0 + 5000, dead_since=T0 + 1000), (False, None))
        self.assertEqual(self.call(tor(availability=2.5), T0 + 5000, dead_since=T0 + 1000), (False, None))
        # Next dead-looking poll starts a fresh window.
        self.assertEqual(self.call(tor(), T0 + 5100, dead_since=None), (False, T0 + 5100))

    def test_custom_window(self):
        self.assertEqual(
            swarm_dead(tor(), now_ts=T0 + 400, first_seen_ts=T0, dead_since_ts=T0 + 100,
                       grace_s=60, window_s=300),
            (True, T0 + 100),
        )


if __name__ == "__main__":
    unittest.main()
