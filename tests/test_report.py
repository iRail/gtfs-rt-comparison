import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from google.transit import gtfs_realtime_pb2 as gtfs
from scripts.build_report import build_report, compare, fetch_feed, parse_feed


def feed():
    message = gtfs.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = 1_700_000_000
    return message


def add_trip(message, trip_id="trip-1", date="20261006", start="07:00:00", delays=(0, 120)):
    entity = message.entity.add()
    entity.id = f"entity-{len(message.entity)}"
    update = entity.trip_update
    update.trip.trip_id = trip_id
    update.trip.start_date = date
    update.trip.start_time = start
    for index, delay in enumerate(delays):
        stop = update.stop_time_update.add()
        stop.stop_id = f"stop-{index}"
        if delay is not None:
            stop.arrival.delay = delay
        stop.departure.time = 1_700_000_100 + index * 60
    return update


def parsed(message, name="sncb"):
    return parse_feed(message.SerializeToString(), name, 1_700_000_010)


class FeedTests(unittest.TestCase):
    def test_zero_negative_and_missing_are_distinct(self):
        message = feed()
        add_trip(message, "zero", delays=(0,))
        add_trip(message, "early", delays=(-30,))
        add_trip(message, "unknown", delays=(None,))
        result = parsed(message)
        self.assertEqual([trip["max_delay_seconds"] for trip in result["trips"]], [0, -30, None])
        self.assertEqual(result["stats"]["delayed_trips"], 0)
        self.assertEqual(result["stats"]["trips_with_delay_values"], 2)
        self.assertEqual(result["stats"]["usable_stop_updates"], 3)

    def test_arrival_departure_and_trip_delay_maximum(self):
        message = feed()
        update = add_trip(message, delays=(120,))
        update.stop_time_update[0].departure.delay = 180
        update.delay = 240
        result = parsed(message)
        self.assertEqual(result["trips"][0]["max_delay_seconds"], 240)
        self.assertEqual(result["stats"]["delay_values"], 3)
        self.assertEqual(result["stats"]["delayed_trips"], 1)

    def test_cancelled_trips_and_deleted_entities_are_not_delays(self):
        message = feed()
        add_trip(message).trip.schedule_relationship = gtfs.TripDescriptor.CANCELED
        add_trip(message, "deleted-entity")
        message.entity[-1].is_deleted = True
        result = parsed(message)
        self.assertEqual(result["stats"]["cancelled_trips"], 1)
        self.assertEqual(result["stats"]["delayed_trips"], 0)
        self.assertEqual(result["deleted_entities"], 1)

    def test_skipped_and_no_data_stops_excluded(self):
        message = feed()
        update = add_trip(message, delays=(300, 900))
        update.stop_time_update[0].schedule_relationship = gtfs.TripUpdate.StopTimeUpdate.SKIPPED
        update.stop_time_update[1].schedule_relationship = gtfs.TripUpdate.StopTimeUpdate.NO_DATA
        result = parsed(message)
        self.assertIsNone(result["trips"][0]["max_delay_seconds"])
        self.assertEqual(result["stats"]["usable_stop_updates"], 0)

    def test_duplicate_retains_newest(self):
        message = feed()
        add_trip(message, delays=(600,)).timestamp = 200
        add_trip(message, delays=(120,)).timestamp = 100
        result = parsed(message)
        self.assertEqual(result["stats"]["trips"], 1)
        self.assertEqual(result["duplicate_trips"], 1)
        self.assertEqual(result["trips"][0]["max_delay_seconds"], 600)

    def test_differential_rejected(self):
        message = feed()
        message.header.incrementality = gtfs.FeedHeader.DIFFERENTIAL
        with self.assertRaisesRegex(ValueError, "Differential"):
            parsed(message)

    def test_stale_and_missing_timestamps(self):
        message = feed()
        self.assertTrue(parse_feed(message.SerializeToString(), "sncb", 1_700_001_000)["warnings"])
        message.header.ClearField("timestamp")
        self.assertIsNone(parsed(message)["age_seconds"])
        self.assertTrue(parsed(message)["warnings"])

    def test_invalid_trip_id_excluded(self):
        message = feed()
        add_trip(message, "")
        self.assertEqual(parsed(message)["stats"]["trips"], 0)


class ComparisonTests(unittest.TestCase):
    def test_bmc_namespace_matches_and_preserves_original_ids(self):
        a, b = feed(), feed()
        add_trip(a, delays=(120,))
        add_trip(b, "gt:nmbssncb:trip-1", delays=(180,))
        result = compare({"sncb": parsed(a), "bmc": parsed(b, "bmc")})
        self.assertEqual(result["matched_trips"], 1)
        self.assertEqual(result["rows"][0]["difference_seconds"], 60)
        self.assertEqual(result["rows"][0]["bmc"]["trip_id"], "gt:nmbssncb:trip-1")

    def test_service_instances_do_not_merge(self):
        a, b = feed(), feed()
        add_trip(a)
        add_trip(b, date="20261007")
        add_trip(b, start="08:00:00")
        add_trip(b, "gt:other:trip-1")
        result = compare({"sncb": parsed(a), "bmc": parsed(b, "bmc")})
        self.assertEqual(result["matched_trips"], 0)
        self.assertEqual(len(result["rows"]), 4)

    def test_missing_delay_is_not_zero(self):
        a, b = feed(), feed()
        add_trip(a)
        add_trip(b, delays=(None,))
        result = compare({"sncb": parsed(a), "bmc": parsed(b, "bmc")})
        self.assertEqual(result["comparable_delays"], 0)
        self.assertIsNone(result["rows"][0]["difference_seconds"])

    def test_cancelled_counterpart_is_not_comparable(self):
        a, b = feed(), feed()
        add_trip(a)
        add_trip(b).trip.schedule_relationship = gtfs.TripDescriptor.CANCELED
        result = compare({"sncb": parsed(a), "bmc": parsed(b, "bmc")})
        self.assertEqual(result["comparable_delays"], 0)
        self.assertIsNone(result["rows"][0]["difference_seconds"])

    def test_unavailable_feed_does_not_mean_missing_trips(self):
        a = feed()
        add_trip(a)
        result = compare({"sncb": parsed(a), "bmc": {"status": "error", "trips": []}})
        self.assertFalse(result["available"])
        self.assertIsNone(result["sncb_only"])
        self.assertIsNone(result["matched_trips"])
        self.assertEqual(len(result["rows"]), 1)


class BuildTests(unittest.TestCase):
    def test_invalid_request_does_not_publish_sensitive_values(self):
        with patch("scripts.build_report.urlopen", side_effect=ValueError("Invalid header value: secret-token")):
            result = fetch_feed("bmc")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_invalid_response_and_empty_protobuf_report_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.pbf"
            for content in (b"<html>Bad gateway</html>", b""):
                path.write_bytes(content)
                result = fetch_feed("bmc", path)
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["stats"])

    def test_build_with_one_feed_failure_still_creates_pages_artifact(self):
        message = feed()
        add_trip(message)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "feed.pbf"
            fixture.write_bytes(message.SerializeToString())
            output = Path(directory) / "site"
            with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": ""}):
                report = build_report(output, {"sncb": fixture, "bmc": Path(directory) / "missing.pbf"})
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / ".nojekyll").is_file())
            self.assertFalse(report["comparison"]["available"])
            self.assertEqual(json.loads((output / "report.json").read_text())["feeds"]["bmc"]["status"], "error")
            self.assertNotIn("trips", report["feeds"]["sncb"])
            self.assertEqual(len(report["comparison"]["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
