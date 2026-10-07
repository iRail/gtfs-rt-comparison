import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.static_gtfs import build_index, enrich_and_audit, fetch_archive, lookup, service_active
from scripts.static_trips_pb2 import TripIndex
from scripts.build_report import build_report
from test_report import add_trip, feed, parsed


def fixture(path, trips=None, calendar_dates=None):
    tables = {
        "trips.txt": trips or [{"trip_id": "gt:nmbssncb:trip-1", "route_id": "route-1", "service_id": "service-1", "trip_short_name": "IC42", "trip_headsign": "Brussels"}],
        "routes.txt": [{"route_id": "route-1", "route_short_name": "IC", "route_long_name": "Ghent — Brussels"}],
        "calendar.txt": [{"service_id": "service-1", "start_date": "20260101", "end_date": "20261231", **{day: "1" for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]}}],
        "calendar_dates.txt": calendar_dates or [{"service_id": "service-1", "date": "20261006", "exception_type": "2"}],
        "stops.txt": [{"stop_id": "stop-1", "stop_name": "Ghent"}, {"stop_id": "stop-2", "stop_name": "Brussels"}],
        # Deliberately unsorted: earliest static departure must follow sequence.
        "stop_times.txt": [{"trip_id": "gt:nmbssncb:trip-1", "stop_id": "stop-2", "stop_sequence": "2", "arrival_time": "08:00:00", "departure_time": "08:01:00"}, {"trip_id": "gt:nmbssncb:trip-1", "stop_id": "stop-1", "stop_sequence": "1", "arrival_time": "06:59:00", "departure_time": "07:00:00"}],
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, rows in tables.items():
            text = io.StringIO(newline="")
            writer = csv.DictWriter(text, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, "\ufeff" + text.getvalue())


class StaticGTFSChecks(unittest.TestCase):
    def test_two_static_sources_keep_independent_matches_and_indexes(self):
        a, b = feed(), feed()
        add_trip(a, "trip-1")
        add_trip(a, "trip-2")
        add_trip(b, "gt:nmbssncb:trip-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, message in (("sncb", a), ("bmc", b)):
                (root / (name + ".pbf")).write_bytes(message.SerializeToString())
            fixture(root / "bmc.zip")
            fixture(root / "sncb.zip", trips=[{"trip_id": "trip-2", "route_id": "route-1", "service_id": "service-1", "trip_short_name": "42"}])
            result = build_report(root / "out", {name: root / (name + ".pbf") for name in ("sncb", "bmc")}, root / "bmc.zip", root / "sncb.zip")
            rows = {row["trip_id"]: row for row in result["comparison"]["rows"]}
            self.assertTrue(rows["trip-1"]["sncb"]["static_match"]["id_found"])
            self.assertFalse(rows["trip-1"]["sncb"]["sncb_static_match"]["id_found"])
            self.assertFalse(rows["trip-2"]["sncb"]["static_match"]["id_found"])
            self.assertTrue(rows["trip-2"]["sncb"]["sncb_static_match"]["id_found"])
            sncb = TripIndex.FromString((root / "out/sncb-static-trips.pb").read_bytes())
            bmc = TripIndex.FromString((root / "out/static-trips.pb").read_bytes())
            self.assertEqual(set(sncb.trips), {"trip-2"})
            self.assertEqual(set(bmc.trips), {"gt:nmbssncb:trip-1"})
            self.assertTrue((root / "out/sncb-static-audit.json").is_file())
            self.assertTrue((root / "out/static-audit.json").is_file())

    def test_second_source_failure_preserves_first_source_matches_and_index(self):
        a = feed()
        add_trip(a)
        feeds = {"sncb": parsed(a), "bmc": {"status": "ok", "trips": []}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "bmc.zip")
            enrich_and_audit(feeds, fetch_archive(root / "bmc.zip"), root / "out")
            # A stale failed-source artifact must be removed without touching BMC.
            (root / "out/sncb-static-trips.pb").write_bytes(b"stale")
            result = enrich_and_audit(feeds, fetch_archive(root / "missing.zip"), root / "out", match_field="sncb_static_match", prefix="sncb-")
            self.assertEqual(result["status"], "error")
            self.assertTrue(feeds["sncb"]["trips"][0]["static_match"]["id_found"])
            self.assertNotIn("sncb_static_match", feeds["sncb"]["trips"][0])
            self.assertTrue((root / "out/static-trips.pb").is_file())
            self.assertFalse((root / "out/sncb-static-trips.pb").exists())

    def test_calendar_exceptions_override_weekdays_and_ranges(self):
        calendar = {"start_date": "20261001", "end_date": "20261010", **{day: "1" for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]}}
        self.assertFalse(service_active(calendar, {("s", "20261007"): "2"}, "s", "20261007"))
        self.assertTrue(service_active(calendar, {("s", "20261011"): "1"}, "s", "20261011"))
        self.assertFalse(service_active(calendar, {}, "s", "20261011"))
        self.assertTrue(service_active(None, {("s", "20261007"): "1"}, "s", "20261007"))
        self.assertIsNone(service_active(calendar, {}, "s", ""))

    def test_namespace_lookup_does_not_guess_variant_suffixes(self):
        index = TripIndex()
        index.trips["gt:nmbssncb:trip-1:2"].service_id = "s"
        self.assertIsNone(lookup(index, "trip-1"))
        self.assertEqual(lookup(index, "trip-1:2"), "gt:nmbssncb:trip-1:2")
        self.assertIsNone(lookup(index, "gt:other:trip-1:2"))

    def test_all_missing_trips_are_audited_not_just_delayed_trips(self):
        a, b = feed(), feed()
        add_trip(a, "trip-1", date="20261007")
        add_trip(b, "gt:nmbssncb:trip-1", date="20261007")
        add_trip(a, "absent-static", date="20261007", delays=(0,))
        feeds = {"sncb": parsed(a), "bmc": parsed(b, "bmc")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.zip"
            fixture(path)
            result = enrich_and_audit(feeds, fetch_archive(path), Path(directory) / "out")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["audit"]["missing_from_bmc"]["id_missing"], 1)
            self.assertEqual(result["missing_trip_evidence"][0]["delayed"], False)
            match = feeds["sncb"]["trips"][0]["static_match"]
            self.assertTrue(match["id_found"])
            self.assertTrue(match["service_active"])
            self.assertTrue(match["start_time_matches"])
            record = result["matched_records"][match["static_trip_id"]]
            self.assertEqual(record["scheduled_stops"][0]["stop_name"], "Ghent")
            restored = TripIndex.FromString((Path(directory) / "out/static-trips.pb").read_bytes())
            self.assertEqual(restored.trips[match["static_trip_id"]].trip_short_name, "IC42")
            self.assertEqual(dict(restored.routes["route-1"].fields)["route_short_name"], "IC")

    def test_id_match_is_separate_from_active_service_and_start_time(self):
        a = feed()
        add_trip(a, "trip-1", start="09:00:00")
        feeds = {"sncb": parsed(a), "bmc": {"status": "ok", "trips": []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.zip"
            fixture(path)
            result = enrich_and_audit(feeds, fetch_archive(path), Path(directory) / "out")
            audit = result["audit"]["missing_from_bmc"]
            self.assertEqual(audit["id_found"], 1)
            self.assertEqual(audit["inactive_service"], 1)
            self.assertEqual(audit["start_time_mismatch"], 1)

    def test_one_realtime_feed_failure_leaves_missing_comparison_unknown(self):
        a = feed()
        add_trip(a)
        feeds = {"sncb": parsed(a), "bmc": {"status": "error", "trips": []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.zip"
            fixture(path)
            result = enrich_and_audit(feeds, fetch_archive(path), Path(directory) / "out")
            self.assertNotIn("missing_from_bmc", result["audit"])
            self.assertEqual(result["audit"]["sncb_all"]["id_found"], 1)

    def test_invalid_static_archive_cannot_turn_unknown_into_no_match(self):
        a = feed()
        add_trip(a)
        feeds = {"sncb": parsed(a), "bmc": {"status": "ok", "trips": []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.zip"
            path.write_bytes(b"not a ZIP")
            result = enrich_and_audit(feeds, fetch_archive(path), Path(directory) / "out")
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["audit"])
            self.assertNotIn("static_match", feeds["sncb"]["trips"][0])

    def test_duplicate_static_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.zip"
            row = {"trip_id": "trip-1", "route_id": "route-1", "service_id": "service-1"}
            fixture(path, trips=[row, row])
            with zipfile.ZipFile(path) as archive:
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    build_index(archive)


if __name__ == "__main__":
    unittest.main()
