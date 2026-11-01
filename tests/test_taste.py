"""taste.md ledger: exact round-trip, record() rules, atomic save, serialised updates."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import taste

SAMPLE = """\
# plex-recs taste
Last poll: 2026-10-11

## Rated
- 2026-08-26 | tv | tmdb:910005 | The Quiet Divide (2025) | liked | finished 2026-08-15
- 2026-08-26 | tv | tmdb:910003 | The Long Harbour (2001) | loved | why: Great finale
- 2026-08-26 | tv | tmdb:910006 | Paper Lanterns (2022) | loved | watched 2026-08-11
- 2026-10-11 | movie | tmdb:910007 | Harbour Lights (1986) | meh

## Unseen
- 2026-08-26 | tv | tmdb:910004 | Night Shift Nine | asked

## Skipped
- 2026-10-11 | tv | tmdb:910008 | The Salt Road (2002) | later

## Requested
- 2026-08-26 | tv | tmdb:910009 | Iron Meridian | ext:43 | all
- 2026-09-16 | movie | tmdb:910010 | Tin Lantern | ext:none | radarr:17
- 2026-10-11 | tv | tmdb:910011 | Ember Coast | ext:60 | S1-S5
"""

TODAY = "2026-10-31"


def _ids(ledger, section):
    return [r.tmdb_id for r in ledger.sections.get(section, [])]


def _assert_single_home(tc, ledger):
    for tid, names in taste.duplicates(ledger).items():
        in_four = [n for n in names if n in taste.SECTIONS]
        tc.assertLessEqual(len(in_four), 1, f"tmdb:{tid} in {in_four}")
    for name in taste.SECTIONS:
        ids = _ids(ledger, name)
        tc.assertEqual(len(ids), len(set(ids)), f"duplicate ids within {name}")


class ParseRenderTests(unittest.TestCase):
    def test_sample_round_trips_exactly(self):
        self.assertEqual(taste.render(taste.parse(SAMPLE)), SAMPLE)

    def test_sample_parses_fields(self):
        led = taste.parse(SAMPLE)
        self.assertEqual(led.header, "# plex-recs taste")
        self.assertEqual(led.last_poll, "2026-10-11")
        self.assertEqual(list(led.sections), ["Rated", "Unseen", "Skipped", "Requested"])
        quiet, harbour, lanterns, lights = led.sections["Rated"]
        self.assertEqual((quiet.title, quiet.year, quiet.verdict, quiet.finished),
                         ("The Quiet Divide", 2025, "liked", "2026-08-15"))
        self.assertEqual((harbour.verdict, harbour.why), ("loved", "Great finale"))
        self.assertEqual(lanterns.watched, "2026-08-11")
        self.assertEqual((lights.type, lights.tmdb_id, lights.verdict), ("movie", 910007, "meh"))
        shift = led.sections["Unseen"][0]
        self.assertEqual((shift.title, shift.year, shift.verdict), ("Night Shift Nine", None, "asked"))
        gk, tin, ember = led.sections["Requested"]
        self.assertEqual(gk.extra, ["ext:43", "all"])
        self.assertEqual(tin.extra, ["ext:none", "radarr:17"])
        self.assertEqual(ember.extra, ["ext:60", "S1-S5"])

    def test_crlf_and_bom_normalised(self):
        dirty = "﻿" + SAMPLE.replace("\n", "\r\n")
        self.assertEqual(taste.render(taste.parse(dirty)), SAMPLE)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "taste.md"
            p.write_bytes(dirty.encode("utf-8"))
            self.assertEqual(taste.render(taste.load(p)), SAMPLE)

    def test_modified_row_is_rerendered_unmodified_rows_verbatim(self):
        text = SAMPLE.replace("| meh", "| meh | odd-field")
        led = taste.parse(text)
        self.assertEqual(taste.render(led), text)
        led.sections["Rated"][3].why = "too loud"
        out = taste.render(led)
        self.assertIn("- 2026-10-11 | movie | tmdb:910007 | Harbour Lights (1986) | meh | odd-field | why: too loud\n", out)
        # untouched rows still byte-identical
        self.assertIn("| The Long Harbour (2001) | loved | why: Great finale\n", out)

    def test_why_may_contain_separator(self):
        line = "- 2026-01-01 | movie | tmdb:1 | X (2000) | loved | why: a | b | c\n"
        led = taste.parse("# t\n\n## Rated\n" + line)
        self.assertEqual(led.sections["Rated"][0].why, "a | b | c")
        self.assertTrue(taste.render(led).endswith(line))

    def test_garbage_line_raises_instead_of_dropping(self):
        with self.assertRaises(taste.TasteFormatError):
            taste.parse("# t\n\n## Rated\nsome note\n")
        with self.assertRaises(taste.TasteFormatError):
            taste.parse("# t\n\n## Rated\n- 2026-01-01 | tv | imdb:tt1 | X\n")


class FileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "taste.md"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_empty_ledger(self):
        led = taste.load(self.path)
        self.assertEqual(led.header, "# plex-recs taste")
        self.assertIsNone(led.last_poll)
        self.assertEqual(list(led.sections), list(taste.SECTIONS))
        self.assertTrue(all(rows == [] for rows in led.sections.values()))

    def test_empty_ledger_saves_all_headings(self):
        taste.save(self.path, taste.load(self.path))
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "# plex-recs taste\n\n## Rated\n\n## Unseen\n\n## Skipped\n\n## Not mine\n",
        )

    def test_non_ascii_round_trip_through_save_load(self):
        led = taste.load(self.path)
        taste.record(led, tmdb_id=910012, type="tv", title="Kōbe Nights", year=2024,
                     verdict="loved", why="Hana — wow", today=TODAY)
        taste.save(self.path, led)
        raw = self.path.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        again = taste.load(self.path)
        row = again.sections["Rated"][0]
        self.assertEqual((row.title, row.year, row.why), ("Kōbe Nights", 2024, "Hana — wow"))
        self.assertEqual(taste.render(again), raw.decode("utf-8"))

    def test_save_inserts_missing_sections_in_order(self):
        self.path.write_text(SAMPLE, encoding="utf-8")
        led = taste.load(self.path)
        taste.save(self.path, led)
        led2 = taste.load(self.path)
        self.assertEqual(list(led2.sections), ["Rated", "Unseen", "Skipped", "Not mine", "Requested"])
        self.assertIn("## Not mine\n\n## Requested\n", self.path.read_text(encoding="utf-8"))

    def test_save_is_atomic_and_leaves_no_tmp(self):
        self.path.write_text(SAMPLE, encoding="utf-8")
        taste.save(self.path, taste.parse(SAMPLE))
        self.assertFalse((self.dir / "taste.md.tmp").exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["taste.md"])

    def test_failed_replace_keeps_original_and_cleans_tmp(self):
        self.path.write_text(SAMPLE, encoding="utf-8")
        led = taste.parse(SAMPLE)
        led.last_poll = "1999-01-01"
        with patch("app.taste.os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                taste.save(self.path, led)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)
        self.assertFalse((self.dir / "taste.md.tmp").exists())


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.led = taste.parse(SAMPLE)

    def tearDown(self):
        _assert_single_home(self, self.led)

    def test_add_new_to_unseen(self):
        res = taste.record(self.led, tmdb_id=1399, type="tv", title="Game of Thrones",
                           year=2011, verdict="unseen", today=TODAY)
        self.assertEqual((res.status, res.section, res.changed), ("added", "Unseen", True))
        self.assertIsNone(res.previous)
        row = self.led.sections["Unseen"][-1]
        self.assertEqual((row.date, row.verdict, row.tmdb_id), (TODAY, "asked", 1399))
        self.assertEqual(self.led.last_poll, TODAY)
        self.assertIn("- 2026-10-31 | tv | tmdb:1399 | Game of Thrones (2011) | asked\n",
                      taste.render(self.led))

    def test_move_unseen_to_rated_keeps_date(self):
        res = taste.record(self.led, tmdb_id=910004, type="tv", title="Night Shift Nine",
                           year=2016, verdict="liked", finished="2026-10-30", today=TODAY)
        self.assertEqual((res.status, res.section), ("moved", "Rated"))
        self.assertEqual(res.previous[0], "Unseen")
        self.assertNotIn(910004, _ids(self.led, "Unseen"))
        row = self.led.sections["Rated"][-1]
        self.assertEqual((row.tmdb_id, row.date, row.verdict, row.finished),
                         (910004, "2026-08-26", "liked", "2026-10-30"))
        self.assertIn("- 2026-08-26 | tv | tmdb:910004 | Night Shift Nine (2016) | liked | finished 2026-10-30\n",
                      taste.render(self.led))

    def test_rated_verdict_change_needs_confirm(self):
        before = taste.render(self.led)
        res = taste.record(self.led, tmdb_id=910007, type="movie", title="Harbour Lights",
                           year=1986, verdict="loved", today=TODAY)
        self.assertEqual((res.status, res.changed), ("needs_confirm", False))
        self.assertEqual(res.previous[0], "Rated")
        self.assertEqual(res.previous[1].verdict, "meh")
        self.assertEqual(res.row.verdict, "loved")
        self.assertEqual(taste.render(self.led), before)
        self.assertEqual(self.led.last_poll, "2026-10-11")

    def test_rated_to_status_needs_confirm(self):
        before = taste.render(self.led)
        for verdict in ("unseen", "skip", "not_mine"):
            res = taste.record(self.led, tmdb_id=910003, type="tv", title="The Long Harbour",
                               year=2001, verdict=verdict, today=TODAY)
            self.assertEqual(res.status, "needs_confirm", verdict)
        self.assertEqual(taste.render(self.led), before)

    def test_force_applies_verdict_change_in_place(self):
        res = taste.record(self.led, tmdb_id=910007, type="movie", title="Harbour Lights",
                           year=1986, verdict="loved", today=TODAY, force=True)
        self.assertTrue(res.changed)
        self.assertEqual(res.status, "updated")
        self.assertEqual(_ids(self.led, "Rated"), [910005, 910003, 910006, 910007])
        row = self.led.sections["Rated"][3]
        self.assertEqual((row.verdict, row.date), ("loved", "2026-10-11"))
        self.assertEqual(self.led.last_poll, TODAY)

    def test_force_moves_rated_to_skipped(self):
        res = taste.record(self.led, tmdb_id=910003, type="tv", title="The Long Harbour",
                           year=2001, verdict="skip", today=TODAY, force=True)
        self.assertEqual((res.status, res.section), ("moved", "Skipped"))
        self.assertNotIn(910003, _ids(self.led, "Rated"))
        self.assertEqual(self.led.sections["Skipped"][-1].verdict, "later")

    def test_same_verdict_merges_why(self):
        res = taste.record(self.led, tmdb_id=910007, type="movie", title="Harbour Lights",
                           year=1986, verdict="meh", why="Dated\nbut fine", today=TODAY)
        self.assertEqual((res.status, res.changed), ("unchanged", True))
        row = self.led.sections["Rated"][3]
        self.assertEqual((row.why, row.date), ("Dated but fine", "2026-10-11"))
        self.assertIn("- 2026-10-11 | movie | tmdb:910007 | Harbour Lights (1986) | meh | why: Dated but fine\n",
                      taste.render(self.led))
        self.assertEqual(self.led.last_poll, TODAY)

    def test_same_verdict_keeps_existing_dates_and_why(self):
        res = taste.record(self.led, tmdb_id=910005, type="tv", title="The Quiet Divide",
                           year=2025, verdict="liked", today=TODAY)
        self.assertEqual((res.status, res.changed), ("unchanged", False))
        row = self.led.sections["Rated"][0]
        self.assertEqual(row.finished, "2026-08-15")
        self.assertEqual(self.led.last_poll, "2026-10-11")
        self.assertEqual(taste.render(self.led), SAMPLE)

    def test_not_mine_section_created(self):
        self.assertNotIn("Not mine", self.led.sections)
        res = taste.record(self.led, tmdb_id=910008, type="tv", title="The Salt Road",
                           year=2002, verdict="not_mine", today=TODAY)
        self.assertEqual((res.status, res.section), ("moved", "Not mine"))
        self.assertEqual(_ids(self.led, "Not mine"), [910008])
        self.assertEqual(_ids(self.led, "Skipped"), [])
        self.assertEqual(self.led.sections["Not mine"][0].verdict, "not mine")
        self.assertEqual(list(self.led.sections), ["Rated", "Unseen", "Skipped", "Not mine", "Requested"])
        self.assertIn("- 2026-10-11 | tv | tmdb:910008 | The Salt Road (2002) | not mine\n",
                      taste.render(self.led))

    def test_no_title_in_two_sections_after_any_record(self):
        # Seed a pre-existing cross-section duplicate, then hit it from every angle.
        self.led.sections["Skipped"].append(taste.Row("2026-01-01", "tv", 910004, "Night Shift Nine",
                                                      verdict="later"))
        seq = ["skip", "unseen", "not_mine", "liked", "loved", "unseen", "meh", "skip"]
        for v in seq:
            taste.record(self.led, tmdb_id=910004, type="tv", title="Night Shift Nine", year=None,
                         verdict=v, today=TODAY, force=True)
            _assert_single_home(self, self.led)
            self.assertEqual(sum(_ids(self.led, s).count(910004) for s in taste.SECTIONS), 1)

    def test_legacy_requested_untouched(self):
        before = list(self.led.sections["Requested"])
        res = taste.record(self.led, tmdb_id=910009, type="tv", title="Iron Meridian",
                           year=2008, verdict="loved", today=TODAY)
        self.assertEqual(res.status, "added")
        self.assertEqual(self.led.sections["Requested"], before)
        self.assertIn("- 2026-08-26 | tv | tmdb:910009 | Iron Meridian | ext:43 | all\n",
                      taste.render(self.led))
        self.assertEqual(taste.find(self.led, 910009)[0], "Rated")
        self.assertEqual(taste.duplicates(self.led), {910009: ["Rated", "Requested"]})

    def test_bad_input_rejected(self):
        with self.assertRaises(ValueError):
            taste.record(self.led, tmdb_id=1, type="film", title="X", year=None,
                         verdict="liked", today=TODAY)
        with self.assertRaises(ValueError):
            taste.record(self.led, tmdb_id=1, type="tv", title="X", year=None,
                         verdict="amazing", today=TODAY)


class QueryTests(unittest.TestCase):
    def test_duplicates_finds_cross_section_id(self):
        led = taste.parse(SAMPLE)
        self.assertEqual(taste.duplicates(led), {})
        led.sections["Requested"].append(taste.Row("2026-10-01", "tv", 910008, "The Salt Road", 2002))
        self.assertEqual(taste.duplicates(led), {910008: ["Skipped", "Requested"]})

    def test_find_order_and_miss(self):
        led = taste.parse(SAMPLE)
        self.assertEqual(taste.find(led, 910004)[0], "Unseen")
        self.assertEqual(taste.find(led, 910011)[0], "Requested")
        self.assertIsNone(taste.find(led, 999999))

    def test_drop_section(self):
        led = taste.parse(SAMPLE)
        rows = taste.drop_section(led, "Requested")
        self.assertEqual([r.tmdb_id for r in rows], [910009, 910010, 910011])
        self.assertNotIn("Requested", led.sections)
        self.assertEqual(taste.drop_section(led, "Nope"), [])

    def test_to_dict_is_json_safe(self):
        import json
        row = taste.parse(SAMPLE).sections["Rated"][1]
        d = taste.to_dict(row)
        self.assertNotIn("raw", d)
        self.assertEqual(d["why"], "Great finale")
        self.assertEqual(json.loads(json.dumps(d)), d)


class UpdateTests(unittest.TestCase):
    def test_concurrent_updates_do_not_lose_writes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "taste.md"
            path.write_text(SAMPLE, encoding="utf-8")

            def make(i):
                def fn(led):
                    res = taste.record(led, tmdb_id=900000 + i, type="movie", title=f"Film {i}",
                                       year=2000 + i, verdict="unseen", today=TODAY)
                    return res, res.changed
                return fn

            async def main():
                return await asyncio.gather(*(taste.update(path, make(i)) for i in range(10)))

            results = asyncio.run(main())
            self.assertTrue(all(r.status == "added" for r in results))
            led = taste.load(path)
            ids = set(_ids(led, "Unseen"))
            self.assertTrue({900000 + i for i in range(10)} <= ids)
            self.assertIn(910004, ids)
            self.assertEqual(led.sections["Requested"], taste.parse(SAMPLE).sections["Requested"])
            self.assertFalse((Path(d) / "taste.md.tmp").exists())

    def test_update_skips_save_when_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "taste.md"
            path.write_text(SAMPLE, encoding="utf-8")
            with patch("app.taste.save") as fake_save:
                out = asyncio.run(taste.update(path, lambda led: ("x", False)))
            self.assertEqual(out, "x")
            fake_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
