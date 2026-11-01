# Time-window responsiveness fix

## Cause

History scans, telemetry writes and status-related database work shared one executor. Every live event and five-second browser poll launched another complete refresh, including analytics for hidden pages. Changing windows ignored obsolete results but did not cancel their requests or SQLite work. Cleanup repeatedly vacuumed the whole database near its size threshold.

On the Pi, a status response took 0.44 seconds but contained a 137-second-old sample. A 15-minute analytics request timed out after ten seconds. Read-only inspection found a roughly 208 MB database and about 291 GB of cumulative disk writes attributed to the panel process, consistent with repeated full-file compaction.

## Changes

- Health sampling reads live process/heartbeat state and memory caches; it never waits for history queries or incidents. Tailscale checks run separately, with two-second subprocess limits and their own freshness.
- Two bounded read workers use independent read-only WAL snapshots. SQLite progress handlers enforce three-second query budgets and cancellation; disconnected final waiters interrupt the underlying query. Saturated readers return retryable `history_busy` responses instead of growing a work queue.
- Schema 3 adds compact request/command metadata, indexed by time, endpoint and outcome. New events and metadata commit atomically. Old records are indexed in resumable batches; payload inspector data remains intact.
- Analytics reads metadata rather than reparsing payloads. Median/p95 remain exact. Identical range requests share in-progress work and a ten-second bounded cache, including requests crossing a wall-clock bucket boundary.
- Request tables use `summary=true` to fetch lightweight metadata. Full previews remain on the existing detail endpoint; older callers retain the previous listing shape by default.
- Retention performs bounded deletion/page reclamation and opportunistic WAL truncation. It never loops over full database VACUUM. Physical accounting includes metadata/index pages, WAL and SHM; reusable free pages do not cause repeated unnecessary deletion.
- Browser resources load independently, cancel on selection changes, coalesce live invalidations and prevent overlapping polls. Hidden pages do not request analytics. Previous-window results keep a visible label until replacement data arrives; history errors no longer mark the connection stale.

## Review and deployment

No unrelated cogs, Activity UI, credentials or history files belong to this change. Built frontend assets and the migration/tests are included. Read the staged diff before committing/pushing through the normal workflow.

After pulling the reviewed commit on the Pi, stop the previous launcher and restart with `bash run.sh`. Do not start a second stack alongside it. Existing history is not reset. The first run will display indexing progress while the old records are indexed in the background; new records are immediately indexed. Existing retention and disk-budget limits still apply.

Tests cover exact aggregates on large history, resumable indexing, real SQL interruption, client-disconnect cancellation, concurrent writers, a real five-second health tick while both readers are busy, cache sharing, and deterministic browser polling/cancellation behaviour.

The Pi benchmark script opens the original production database read-only and migrates only a temporary backup. Its temporary copy uses NORMAL sync solely to reduce disposable indexing I/O; production keeps its configured synchronous mode. The script does not restart services or modify production history.

## Measured on the Raspberry Pi

An isolated backup of the real 209,629,184-byte production history contained 195,311 request events. The source connection reported zero changes.

| Query | Uncached | Cached |
| --- | ---: | ---: |
| 15 minutes | 0.101 s | <0.001 s |
| 1 hour | 0.285 s | <0.001 s |
| 24 hours | 2.250 s | <0.001 s |
| 72 hours | 2.252 s | <0.001 s |

The 100-row metadata-only request page took 0.0037 seconds and returned 39,864 bytes. The largest analytics response was 11,424 bytes. Exact median/p95 calculations were retained. Initial indexing in the disposable NORMAL-sync copy took 17.9 seconds; this is not a promise for production backfill, which retains its normal durability settings and runs in smaller background batches.
