# SNCB × BMC GTFS-RT comparison

A GitHub Pages report with side-by-side feed coverage, trip matching and expandable arrival/departure delays at each reported stop. The report is built from two concurrently fetched snapshots, with no live browser requests to the upstream feeds.

## Feeds

- **SNCB:** <https://sncb-opendata.hafas.de/gtfs/realtime/d22ad6759ee25bg84ddb6c818g4dc4de_TC>
- **BMC:** <https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/rt/trip-update?format=protobuf>

The originally supplied BMC URL ending in `/trip-update.pbf` returns 404. The replacement `opendata-discovery-gtfs-realtime.api.production.belgianmobility.io` hostname did not resolve during verification. The working BMC endpoint uses `/trip-update?format=protobuf`; without the format option it returns JSON. Both feeds were successfully decoded during development.

Both static archives are downloaded on every workflow run, alongside the realtime feeds:

- **SNCB static GTFS:** <https://sncb-opendata.hafas.de/gtfs/static/c21ac6758dd25af84cca5b707f3cb3de>, overridden with `SNCB_STATIC_GTFS_URL`.
- **BMC static GTFS:** <https://gtfs.flatturtle.cloud/sncb-nmbs/_latest/sncb-nmbs-gtfs.zip>, overridden with `STATIC_GTFS_URL`.

Each archive has its own protobuf trip index, ID audit and expandable trip information.

## GitHub Pages setup

1. Push these files to the repository's `main` branch.
2. In **Settings → Pages → Build and deployment**, set **Source** to **GitHub Actions**.
3. Run **Build and deploy feed comparison** from the **Actions** tab, or let the push trigger it.

The workflow in [`.github/workflows/pages.yml`](.github/workflows/pages.yml) installs the pinned protobuf dependencies, tests the comparison logic, fetches both feeds, builds `_site/`, uploads the Pages artifact and deploys it through the `github-pages` environment. It does not commit generated data to the repository. The deployed URL appears in the workflow's deployment output; for this repository it is normally <https://irail.github.io/gtfs-rt-comparison/>.

### Refresh schedule

```yaml
schedule:
  - cron: '0 7-9,17-20 * * *'
    timezone: Europe/Brussels
```

Runs **every day** at **07:00, 08:00, 09:00, 17:00, 18:00, 19:00 and 20:00 Brussels time**, including daylight-saving changes. Pushes to `main` and manual dispatch also build and deploy immediately. To collect only on weekdays, replace the final `*` with `1-5`.

[GitHub supports timezone-aware schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Scheduled runs may be delayed or dropped during high load, and public-repository schedules are disabled after 60 days without repository activity. The page displays capture and publication timestamps, so it remains clear which snapshot is shown.

### Optional endpoint overrides / authentication

Defaults work without credentials at the time of verification. Set these in **Settings → Secrets and variables → Actions** if the providers change:

| Feed | Repository URL variable | Header-name variable | Header-value secret |
| --- | --- | --- | --- |
| SNCB | `SNCB_FEED_URL` | `SNCB_AUTH_HEADER` | `SNCB_AUTH_VALUE` |
| BMC | `BMC_FEED_URL` | `BMC_AUTH_HEADER` | `BMC_AUTH_VALUE` |

Both header fields must be set to send authentication. Credentials and overridden URLs are not included in the public report. Use the provider's actual header name, such as `Ocp-Apim-Subscription-Key`, if required.

## Comparison rules

- Match exact `(trip_id, start_date, start_time)` after removing the observed BMC `gt:nmbssncb:` trip namespace. Preserve original identifiers in the details. Missing descriptor fields remain empty; no heuristic matching is performed.
- Stop details remove only the observed `gs:nmbssncb:` namespace, retain platform suffixes, and align repeated stops by occurrence because SNCB does not supply stop sequences. SNCB's stop order takes precedence; extra BMC stops follow.
- A **delayed trip** is a non-cancelled trip with at least one **explicit positive** trip-level, arrival or departure delay. The displayed trip delay is the maximum of all usable explicit values in the snapshot, including past stops. It is not necessarily the current delay.
- Zero means an explicit zero delay; missing fields mean unknown. Absolute event times alone do not establish a delay without the static schedule. No inferred delay propagation is performed.
- Cancelled service instances and NO_DATA/SKIPPED stop updates are excluded from delay counts. Deleted feed entities are ignored. Duplicate trip instances retain the newest trip update (first wins if timestamps tie).
- Both raw stop-update counts and counts with usable event data are shown, because BMC includes NO_DATA stops absent from SNCB.
- The agreement summary compares maximum explicit delay values across all matched, non-cancelled trips with values in both feeds. Equal maxima do not establish equal stop-level data.
- The detailed table includes every trip delayed in either feed, including trips absent from the other feed. It is sorted by the larger delay, descending. Positive Δ means BMC reports a larger maximum.
- Feeds older than five minutes at collection are flagged. A missing timestamp leaves freshness unknown. Different publication times may explain differences. Each run replaces the previous snapshot; this is not a historical archive.
- Failed feeds have **unknown** statistics and comparisons, rather than zero counts. The report still deploys the healthy feed's data and a visible error, and emits a GitHub Actions warning. Differential feeds are rejected because they cannot be compared as independent full snapshots.

See the [GTFS realtime specification](https://gtfs.org/documentation/realtime/reference/) for field semantics.

## Static trip lookups

The report checks **all** realtime trips against both static `trips.txt` databases. Each source has its own summary of IDs found, IDs absent, active service dates and start-time differences. The delayed-trip table has **In SNCB static GTFS?** and **In BMC static GTFS?** columns, displaying Yes, No or Unknown independently. Missing IDs appear in red. Each Yes has a **Trip info** button for that source's train number, headsign, route, calendar and scheduled stops.

Only the known `gt:nmbssncb:` namespace is added or removed for lookup. All other parts of IDs remain intact. Service activity and start-time checks remain separate from ID presence. An unavailable static source leaves only its own matches unknown and does not prevent the other lookup or realtime report from publishing.

Train website links prefer the SNCB static record and fall back to the BMC record. Links use an advertised numeric train number and the realtime service date, and show the websites' latest information rather than captured delays. An ID match does not establish complete planner applicability: service dates, start times and stop compatibility also matter.

The [saved source comparison](docs/factcheck-hafas-2026-10-07.md) records why checking both databases matters.

The workflow publishes:

- `sncb-static-trips.pb` and `static-trips.pb`: independent SNCB and BMC protobuf maps of **every** original static trip ID to its trip metadata, plus route records. Its schema is [`schemas/static_trips.proto`](schemas/static_trips.proto). It is a trip/route index, not a complete routing database; schedules and service-date checks for current matching realtime trips are in `report.json`.
- `sncb-static-audit.json` and `static-audit.json`: each archive’s SHA-256, source, feed version, download metadata, all-trip summary counts and evidence for every SNCB trip missing from BMC.
- `report.json`: realtime hashes/timestamps, both static audits and matching trip/calendar/schedule information used by the page.

To query the protobuf index locally:

```python
from pathlib import Path
from scripts.static_trips_pb2 import TripIndex

index = TripIndex.FromString(Path('_site/sncb-static-trips.pb').read_bytes())
trip_id = 'YOUR_TRIP_ID'
if trip_id in index.trips:
    trip = index.trips[trip_id]
    print(trip.trip_short_name, trip.trip_headsign, trip.service_id)
```

## Local development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build_report.py
.venv/bin/python -m http.server 8000 --directory _site
```

Open <http://localhost:8000>. To verify with saved protobuf snapshots:

```bash
.venv/bin/python scripts/build_report.py --sncb-file /tmp/sncb.pbf --bmc-file /tmp/bmc.pbf --static-file /tmp/static.zip --sncb-static-file /tmp/sncb-static.zip
```

Reports built with saved files display a local verification banner. Production builds fetch live snapshots. `report.json` contains summary metadata and the original trip/stop details for delayed trips. The site also includes the static index, schema and audit, plus `.nojekyll`, HTML, CSS and JavaScript. All asset paths are relative so the site works under a GitHub Pages repository subpath.
