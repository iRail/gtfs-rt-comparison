"""Fetch two GTFS-RT snapshots and build a self-contained GitHub Pages artifact."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as gtfs

ROOT = Path(__file__).resolve().parents[1]
FEEDS = {
    "sncb": "https://sncb-opendata.hafas.de/gtfs/realtime/d22ad6759ee25bg84ddb6c818g4dc4de_TC",
    "bmc": "https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/rt/trip-update?format=protobuf",
}
MAX_BYTES = 25 * 1024 * 1024
STALE_AFTER_SECONDS = 300


class FeedValidationError(ValueError):
    """A validation message that is safe to publish without request details."""


def optional(message, field):
    return getattr(message, field) if message.HasField(field) else None


def relationship(message):
    field = message.DESCRIPTOR.fields_by_name["schedule_relationship"]
    return field.enum_type.values_by_number[message.schedule_relationship].name


def normalize_trip(entity):
    update = entity.trip_update
    trip = update.trip
    stops = []
    delays = []
    trip_delay = optional(update, "delay")
    if trip_delay is not None:
        delays.append(trip_delay)
    for stop in update.stop_time_update:
        events = {}
        for name in ("arrival", "departure"):
            event = getattr(stop, name)
            events[name] = {
                "delay_seconds": optional(event, "delay"),
                "time": optional(event, "time"),
                "uncertainty": optional(event, "uncertainty"),
            } if stop.HasField(name) else None
            # NO_DATA/SKIPPED events do not announce a usable delay.
            if stop.schedule_relationship not in (1, 2) and events[name] is not None:
                delay = events[name]["delay_seconds"]
                if delay is not None:
                    delays.append(delay)
        stops.append({
            "stop_id": stop.stop_id,
            "stop_sequence": optional(stop, "stop_sequence"),
            "relationship": relationship(stop),
            **events,
        })
    cancelled = trip.schedule_relationship == gtfs.TripDescriptor.CANCELED
    return {
        "entity_id": entity.id,
        "trip_id": trip.trip_id,
        "start_date": trip.start_date,
        "start_time": trip.start_time,
        "route_id": trip.route_id,
        "relationship": relationship(trip),
        "cancelled": cancelled,
        "updated_at": optional(update, "timestamp"),
        "trip_delay_seconds": trip_delay,
        "max_delay_seconds": max(delays) if delays else None,
        "delay_values": len(delays),
        "delayed": not cancelled and any(delay > 0 for delay in delays),
        "stops": stops,
    }


def trip_key(trip):
    # Keep all descriptor fields: do not guess matches across service instances.
    return trip["trip_id"].removeprefix("gt:nmbssncb:"), trip["start_date"], trip["start_time"]


def summarize(trips):
    delayed = [trip["max_delay_seconds"] for trip in trips if trip["delayed"]]
    return {
        "trips": len(trips),
        "stop_updates": sum(len(trip["stops"]) for trip in trips),
        "usable_stop_updates": sum(stop["relationship"] not in ("NO_DATA", "SKIPPED")
                                   and any(stop[event] is not None and
                                           (stop[event]["delay_seconds"] is not None or stop[event]["time"] is not None)
                                           for event in ("arrival", "departure"))
                                   for trip in trips for stop in trip["stops"]),
        "delay_values": sum(trip["delay_values"] for trip in trips),
        "trips_with_delay_values": sum(trip["max_delay_seconds"] is not None for trip in trips),
        "delayed_trips": len(delayed),
        "cancelled_trips": sum(trip["cancelled"] for trip in trips),
        "mean_max_delay_seconds": round(sum(delayed) / len(delayed), 1) if delayed else None,
        "largest_delay_seconds": max(delayed) if delayed else None,
    }


def parse_feed(data, name, fetched_at, elapsed_seconds=0):
    feed = gtfs.FeedMessage()
    feed.ParseFromString(data)
    if not feed.IsInitialized() or not feed.HasField("header"):
        raise FeedValidationError("Feed is missing required protobuf fields.")
    if feed.header.incrementality == gtfs.FeedHeader.DIFFERENTIAL:
        raise FeedValidationError("Differential feed cannot be compared as a full snapshot.")
    trips_by_key = {}
    duplicates = invalid = deleted = trip_entities = 0
    for entity in feed.entity:
        if entity.is_deleted:
            deleted += 1
            continue
        if not entity.HasField("trip_update"):
            continue
        trip_entities += 1
        if not entity.trip_update.trip.trip_id:
            invalid += 1
            continue
        trip = normalize_trip(entity)
        key = trip_key(trip)
        if key in trips_by_key:
            duplicates += 1
            if (trip["updated_at"] or 0) <= (trips_by_key[key]["updated_at"] or 0):
                continue
        trips_by_key[key] = trip
    trips = list(trips_by_key.values())
    timestamp = optional(feed.header, "timestamp")
    age = round(fetched_at - timestamp) if timestamp is not None else None
    warnings = []
    if age is not None and age > STALE_AFTER_SECONDS:
        warnings.append(f"Feed timestamp is {age} seconds old (threshold: {STALE_AFTER_SECONDS}s).")
    if age is not None and age < -60:
        warnings.append("Feed timestamp is in the future; freshness cannot be trusted.")
    if timestamp is None:
        warnings.append("No feed timestamp; freshness is unknown.")
    if duplicates:
        warnings.append(f"{duplicates} duplicate trip instances; newest trip update retained.")
    if invalid:
        warnings.append(f"{invalid} trip updates without trip_id excluded.")
    return {
        "name": name.upper(), "status": "ok", "error": None,
        "fetched_at": fetched_at, "feed_timestamp": timestamp, "age_seconds": age,
        "elapsed_seconds": round(elapsed_seconds, 2), "bytes": len(data),
        "version": feed.header.gtfs_realtime_version,
        "entities": len(feed.entity), "trip_update_entities": trip_entities,
        "deleted_entities": deleted, "duplicate_trips": duplicates,
        "warnings": warnings, "stats": summarize(trips), "trips": trips,
    }


def fetch_feed(name, fixture=None):
    started = time.monotonic()
    try:
        if fixture:
            data = Path(fixture).read_bytes()
        else:
            url = os.environ.get(f"{name.upper()}_FEED_URL") or FEEDS[name]
            headers = {"User-Agent": "iRail-gtfs-rt-comparison/1.0", "Accept": "application/x-protobuf"}
            auth_name = os.environ.get(f"{name.upper()}_AUTH_HEADER")
            auth_value = os.environ.get(f"{name.upper()}_AUTH_VALUE")
            if auth_name and auth_value:
                headers[auth_name] = auth_value
            for attempt in range(3):
                try:
                    with urlopen(Request(url, headers=headers), timeout=30) as response:
                        data = response.read(MAX_BYTES + 1)
                    break
                except HTTPError as error:
                    if error.code < 500 or attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
                except (URLError, TimeoutError, OSError):
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        if len(data) > MAX_BYTES:
            raise FeedValidationError("Feed exceeds the 25 MiB download limit.")
        return parse_feed(data, name, time.time(), time.monotonic() - started)
    except (HTTPError, URLError, TimeoutError, OSError, DecodeError, ValueError) as error:
        # Do not publish request URLs, authentication values, or server bodies.
        if isinstance(error, HTTPError):
            reason = f"HTTP {error.code}: feed could not be downloaded."
        elif isinstance(error, DecodeError):
            reason = "Response is not a valid GTFS-RT protobuf feed."
        elif isinstance(error, FeedValidationError):
            reason = str(error)
        elif isinstance(error, ValueError):
            reason = "Feed request configuration is invalid."
        else:
            reason = "Feed could not be downloaded (connection, timeout, or file error)."
        return {
            "name": name.upper(), "status": "error", "error": reason,
            "fetched_at": time.time(), "feed_timestamp": None, "age_seconds": None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "warnings": [], "stats": None, "trips": [],
        }


def compare(feeds):
    indices = {name: {trip_key(trip): trip for trip in feed["trips"]} for name, feed in feeds.items()}
    sncb, bmc = indices["sncb"], indices["bmc"]
    available = all(feed["status"] == "ok" for feed in feeds.values())
    common = sncb.keys() & bmc.keys()
    comparable = [key for key in common if all(indices[name][key]["max_delay_seconds"] is not None
                                               and not indices[name][key]["cancelled"] for name in feeds)]
    rows = []
    for key in sncb.keys() | bmc.keys():
        a, b = sncb.get(key), bmc.get(key)
        if not any(trip and trip["delayed"] for trip in (a, b)):
            continue
        difference = None
        if a and b and key in comparable:
            difference = b["max_delay_seconds"] - a["max_delay_seconds"]
        rows.append({
            "trip_id": key[0], "start_date": key[1], "start_time": key[2],
            "sncb": a, "bmc": b, "difference_seconds": difference,
        })
    rows.sort(key=lambda row: (-max((row[name]["max_delay_seconds"] or 0) if row[name] else 0
                                   for name in feeds), row["trip_id"], row["start_date"], row["start_time"]))
    return {
        "available": available,
        "matched_trips": len(common) if available else None,
        "sncb_only": len(sncb.keys() - bmc.keys()) if available else None,
        "bmc_only": len(bmc.keys() - sncb.keys()) if available else None,
        "comparable_delays": len(comparable) if available else None,
        "same_max_delay": sum(sncb[key]["max_delay_seconds"] == bmc[key]["max_delay_seconds"]
                              for key in comparable) if available else None,
        "different_max_delay": sum(sncb[key]["max_delay_seconds"] != bmc[key]["max_delay_seconds"]
                                   for key in comparable) if available else None,
        "rows": rows,
    }


def build_report(output, fixtures=None):
    fixtures = fixtures or {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {name: pool.submit(fetch_feed, name, fixtures.get(name)) for name in FEEDS}
        feeds = {name: job.result() for name, job in jobs.items()}
    comparison = compare(feeds)
    # Full trip details are included once, in the delayed-trip rows.
    for feed in feeds.values():
        feed.pop("trips")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": "Europe/Brussels", "feeds": feeds, "comparison": comparison,
        "fixture_mode": bool(fixtures),
    }
    output.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "web").iterdir():
        if path.is_file():
            shutil.copy2(path, output / path.name)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    (output / ".nojekyll").touch()
    summary = ["## GTFS-RT comparison", "", f"Built: {report['generated_at']}", ""]
    for name, feed in feeds.items():
        if feed["status"] == "ok":
            stats = feed["stats"]
            summary.append(f"- {name.upper()}: {stats['trips']} trips, {stats['delayed_trips']} delayed, {stats['stop_updates']} stop updates")
        else:
            summary.append(f"- {name.upper()}: {feed['error']}")
            print(f"::warning title={name.upper()} feed unavailable::{feed['error']}")
    summary.append(f"\nDelayed trip rows: {len(comparison['rows'])}")
    text = "\n".join(summary) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument("--sncb-file", type=Path, help="Use a saved protobuf feed for local verification")
    parser.add_argument("--bmc-file", type=Path, help="Use a saved protobuf feed for local verification")
    args = parser.parse_args()
    build_report(args.output, {name: path for name, path in {"sncb": args.sncb_file, "bmc": args.bmc_file}.items() if path})


if __name__ == "__main__":
    main()
