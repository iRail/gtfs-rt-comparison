"""Build a trip-ID protobuf index and audit realtime coverage against static GTFS."""

import csv
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

if __package__:
    from .static_trips_pb2 import TripIndex
else:
    from static_trips_pb2 import TripIndex

STATIC_URL = "https://gtfs.flatturtle.cloud/sncb-nmbs/_latest/sncb-nmbs-gtfs.zip"
SNCB_STATIC_URL = "https://sncb-opendata.hafas.de/gtfs/static/c21ac6758dd25af84cca5b707f3cb3de"
TRIP_FIELDS = tuple(field.name for field in TripIndex.DESCRIPTOR.fields_by_name["trips"].message_type.fields_by_name["value"].message_type.fields)


def canonical_id(trip_id):
    return trip_id.removeprefix("gt:nmbssncb:")


def fetch_archive(fixture=None, source_url=None):
    url = source_url or os.environ.get("STATIC_GTFS_URL") or STATIC_URL
    path = None
    temporary = False
    try:
        if fixture:
            path = Path(fixture)
            metadata = {"last_modified": None, "etag": None}
        else:
            headers = {"User-Agent": "iRail-gtfs-rt-comparison/1.0", "Accept": "application/zip"}
            for attempt in range(3):
                try:
                    with urlopen(Request(url, headers=headers), timeout=60) as response:
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
                            path = Path(handle.name)
                            temporary = True
                            size = 0
                            while chunk := response.read(1024 * 1024):
                                size += len(chunk)
                                if size > 128 * 1024 * 1024:
                                    raise ValueError("Static archive exceeds the 128 MiB download limit.")
                                handle.write(chunk)
                        metadata = {"last_modified": response.headers.get("Last-Modified"), "etag": response.headers.get("ETag")}
                    break
                except (HTTPError, URLError, TimeoutError, OSError):
                    if path and temporary:
                        path.unlink(missing_ok=True)
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        return {"status": "ok", "path": path, "temporary": temporary,
                "source_url": url,
                "fetched_at": datetime.now(timezone.utc).isoformat(), **metadata}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        if path and temporary:
            path.unlink(missing_ok=True)
        return {"status": "error", "source_url": url,
                "error": f"Static GTFS download failed (HTTP {error.code})." if isinstance(error, HTTPError)
                         else "Static GTFS could not be downloaded or opened.", "audit": None}


def csv_rows(archive, name, required=True):
    candidates = [item for item in archive.namelist() if Path(item).name == name]
    if not candidates and not required:
        return
    if len(candidates) != 1:
        raise ValueError(f"Static archive must contain exactly one {name}.")
    with archive.open(candidates[0]) as stream:
        with io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
            yield from csv.DictReader(text)


def service_active(calendar, exceptions, service_id, date):
    try:
        day = datetime.strptime(date, "%Y%m%d")
    except (TypeError, ValueError):
        return None
    override = exceptions.get((service_id, date))
    if override is not None:
        return override == "1"
    if calendar is None:
        # A service present only in calendar_dates runs only on its added dates.
        return False
    weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[day.weekday()]
    return calendar["start_date"].strip() <= date <= calendar["end_date"].strip() and calendar[weekday] == "1"


def build_index(archive):
    index = TripIndex(format_version=1)
    for row in csv_rows(archive, "trips.txt"):
        trip_id = row.get("trip_id")
        if not trip_id or not row.get("route_id") or not row.get("service_id"):
            raise ValueError("Static trips.txt has missing required trip identifiers.")
        if trip_id in index.trips:
            raise ValueError("Static trips.txt contains duplicate trip IDs.")
        record = index.trips[trip_id]
        for name in TRIP_FIELDS:
            setattr(record, name, row.get(name) or "")
    if not index.trips:
        raise ValueError("Static trips.txt contains no trips.")
    for row in csv_rows(archive, "routes.txt"):
        index.routes[row["route_id"]].fields.update(row)
    return index


def lookup(index, trip_id):
    # Only the known BMC namespace may be removed or added. No suffix heuristics.
    if trip_id in index.trips:
        return trip_id
    candidate = canonical_id(trip_id)
    if candidate in index.trips:
        return candidate
    candidate = "gt:nmbssncb:" + candidate
    return candidate if candidate in index.trips else None


def audit_groups(feeds):
    instances = {name: {(canonical_id(trip["trip_id"]), trip["start_date"], trip["start_time"]): trip
                        for trip in feed["trips"]} for name, feed in feeds.items()}
    a, b = instances["sncb"], instances["bmc"]
    groups = {"sncb_all": list(a.values()), "bmc_all": list(b.values())}
    if all(feed["status"] == "ok" for feed in feeds.values()):
        groups["missing_from_bmc"] = [a[key] for key in sorted(a.keys() - b.keys())]
        groups["retained_by_bmc"] = [a[key] for key in sorted(a.keys() & b.keys())]
    return groups


def enrich_and_audit(feeds, downloaded, output, match_field="static_match", prefix=""):
    if downloaded["status"] != "ok":
        for name in (prefix + "static-trips.pb", prefix + "static_trips.proto"):
            (output / name).unlink(missing_ok=True)
        return downloaded
    try:
        path = downloaded["path"]
        with path.open("rb") as handle:
            sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > 1024 * 1024 * 1024:
                raise ValueError("Static archive exceeds the 1 GiB uncompressed limit.")
            index = build_index(archive)
            index.source_url = downloaded["source_url"]
            index.sha256 = sha256
            index.generated_at = datetime.now(timezone.utc).isoformat()
            groups = audit_groups(feeds)
            relevant = {}
            for feed in feeds.values():
                for trip in feed["trips"]:
                    trip_id = lookup(index, trip["trip_id"])
                    trip[match_field] = {"id_found": trip_id is not None, "static_trip_id": trip_id,
                                            "service_active": None, "start_time_matches": None,
                                            "service_date": trip["start_date"] or None}
                    if trip_id:
                        relevant[trip_id] = None
            services = {index.trips[trip_id].service_id for trip_id in relevant}
            dates = {trip["start_date"] for feed in feeds.values() for trip in feed["trips"] if trip["start_date"]}
            calendars = {row["service_id"]: row for row in csv_rows(archive, "calendar.txt", required=False)
                         if row["service_id"] in services}
            exceptions = {}
            service_ids = set(calendars)
            for row in csv_rows(archive, "calendar_dates.txt", required=False):
                if row["service_id"] in services:
                    service_ids.add(row["service_id"])
                    if row["date"] in dates:
                        if row["exception_type"] not in ("1", "2"):
                            raise ValueError("Static calendar_dates.txt has an invalid exception type.")
                        exceptions[row["service_id"], row["date"]] = row["exception_type"]
            stops = {row["stop_id"]: row for row in csv_rows(archive, "stops.txt")}
            schedules = {trip_id: [] for trip_id in relevant}
            for row in csv_rows(archive, "stop_times.txt"):
                if row["trip_id"] in schedules:
                    schedules[row["trip_id"]].append({**row, "stop_name": stops.get(row["stop_id"], {}).get("stop_name", "")})
            for trip_id in relevant:
                trip = index.trips[trip_id]
                schedule = sorted(schedules[trip_id], key=lambda stop: int(stop["stop_sequence"]))
                relevant[trip_id] = {
                    "trip": {"trip_id": trip_id, **{name: getattr(trip, name) for name in TRIP_FIELDS}},
                    "route": dict(index.routes[trip.route_id].fields) if trip.route_id in index.routes else None,
                    "calendar": calendars.get(trip.service_id),
                    "calendar_exceptions": [{"date": date, "exception_type": exception}
                                            for (service, date), exception in sorted(exceptions.items()) if service == trip.service_id],
                    "scheduled_stops": schedule,
                }
            for feed in feeds.values():
                for trip in feed["trips"]:
                    match = trip[match_field]
                    if match["id_found"]:
                        trip_id = match["static_trip_id"]
                        service = index.trips[trip_id].service_id
                        match["service_active"] = service_active(calendars.get(service), exceptions, service, trip["start_date"]) if service in service_ids else None
                        schedule = relevant[trip_id]["scheduled_stops"]
                        if trip["start_time"] and schedule and schedule[0]["departure_time"]:
                            match["start_time_matches"] = trip["start_time"] == schedule[0]["departure_time"]
            audit = {}
            for name, trips in groups.items():
                audit[name] = {
                    "total": len(trips),
                    "id_found": sum(trip[match_field]["id_found"] for trip in trips),
                    "id_missing": sum(not trip[match_field]["id_found"] for trip in trips),
                    "active_service": sum(trip[match_field]["service_active"] is True for trip in trips),
                    "inactive_service": sum(trip[match_field]["service_active"] is False for trip in trips),
                    "start_time_mismatch": sum(trip[match_field]["start_time_matches"] is False for trip in trips),
                    "scheduled_id_missing": sum(trip["relationship"] == "SCHEDULED" and not trip[match_field]["id_found"] for trip in trips),
                }
            evidence = [{"trip_id": trip["trip_id"], "start_date": trip["start_date"], "start_time": trip["start_time"],
                         "relationship": trip["relationship"], "delayed": trip["delayed"], **trip[match_field]}
                        for trip in groups.get("missing_from_bmc", [])]
            metadata = {key: value for key, value in downloaded.items() if key not in ("path", "temporary")}
            metadata.update({"sha256": sha256, "trip_count": len(index.trips), "route_count": len(index.routes),
                             "feed_info": list(csv_rows(archive, "feed_info.txt", required=False)),
                             "audit": audit, "matched_records": relevant, "missing_trip_evidence": evidence,
                             "index_file": prefix + "static-trips.pb", "schema_file": prefix + "static_trips.proto"})
            output.mkdir(parents=True, exist_ok=True)
            (output / (prefix + "static-trips.pb")).write_bytes(index.SerializeToString(deterministic=True))
            (output / (prefix + "static_trips.proto")).write_text((Path(__file__).resolve().parents[1] / "schemas/static_trips.proto").read_text())
            return metadata
    except (OSError, zipfile.BadZipFile, ValueError, KeyError, UnicodeError, csv.Error) as error:
        for name in (prefix + "static-trips.pb", prefix + "static_trips.proto"):
            (output / name).unlink(missing_ok=True)
        for feed in feeds.values():
            for trip in feed["trips"]:
                trip.pop(match_field, None)
        return {"status": "error", "source_url": downloaded["source_url"],
                "error": "Static GTFS could not be indexed; archive data is missing, malformed or unreadable.", "audit": None}
    finally:
        if downloaded.get("temporary"):
            downloaded["path"].unlink(missing_ok=True)
