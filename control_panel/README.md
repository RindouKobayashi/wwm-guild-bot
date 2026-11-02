# WWM Control Room

## API Tools and daily operations

**API Tools** provides guided player, guild, combat-plan and ranking lookups, with local presets, sanitized results, comparisons and traffic correlation. **Advanced client** uses host-approved HTTPS profiles; it cannot call arbitrary destinations. **Bot Activity** can preview and explicitly confirm edits to existing leaderboard, schedule and market-report messages through the running managed bot. Manual edits suppress mentions and cannot assign roles or create replacement messages.

Read [API-TOOLS-REVIEW.md](API-TOOLS-REVIEW.md) for usage, profile setup, limits, schema 4 migration, tests and rollout. Custom profiles belong in ignored `profiles.local.json`; the committed example contains no credentials. Restart the managed bot after upgrading to enable message actions. API Tools does not automatically log in when cached authentication is missing.

## Time-window performance update

Live health, history tables and analytics now refresh independently. Health polls every five seconds; analytics refreshes at most every ten seconds. Window/filter changes cancel superseded requests and query work. A slow history request displays a history-specific message while live health remains responsive. Previous results retain their original selection label until replacement results arrive.

SQLite schema 3 adds compact metadata and WAL mode. Existing history is indexed in resumable background batches; incomplete totals are explicitly labelled. New events are indexed immediately, and full payloads remain available in the request inspector. Exact medians/p95 and the existing retention limits are preserved.

Analytics accepts `range_seconds=900|3600|86400|259200` or the existing `since`/`until` parameters. Responses include `since`, `until`, `as_of` and `indexing`. Identical range queries share work and a ten-second cache. `summary=true` on the existing request listing returns lightweight table records; omitting it preserves the previous payload-bearing response shape. Overload/timeouts return HTTP 503 with `code: history_busy`, `retryable: true` and `Retry-After: 2`.

History has two read slots, read-only snapshots and a three-second execution budget. Writes remain on their own connection. Maintenance uses bounded batches and page reclamation instead of repeated full-file VACUUM. Legacy non-incremental databases reuse freed pages; they are not force-compacted during traffic. The size budget accounts for the DB, its indexes/metadata, WAL and SHM.

History performance counters are visible in Diagnostics and `/api/status.performance`: active readers, cache hits, shared/cancelled/timed-out queries, capacity rejections, queue wait, query duration and health-sample delay. `/api/status.indexing` reports backfill progress, and `/api/status.storage` reports physical files versus the configured budget.

Review [PERFORMANCE-REVIEW.md](PERFORMANCE-REVIEW.md) before deploying. After pulling this release, stop the old combined launcher and run `bash run.sh` again. Do not delete the history database or increase the heartbeat stale timeout to work around loading delays.

Additional verification:

```bash
python -m unittest discover -s control_panel/tests -p test_history_performance.py -v
node --test control_panel/tests/refresh.test.mjs
```

## Expansion release

The expanded panel adds complete-window API analytics (15m/1h/24h/72h), outcome/status/duration/retry filters, request investigations with correlated logs, command metrics, explicitly instrumented background jobs, dashboard-only incidents, runtime version reporting and startup progress. Filters and the selected page are bookmarkable; mobile navigation uses a drawer.

Summary statistics are calculated from all retained matching requests, independently of the paginated table. Activity traffic is counted separately from WWM requests. Empty latency buckets are gaps, not zero-duration requests. Names/outcomes/timings are recorded for commands; arguments and user identities are not.

Registered jobs report iteration start, completion, propagated exceptions, logged errors, consecutive failures and the next scheduled iteration. A completed iteration may have skipped work based on its own conditions; silent internally handled failures cannot be inferred. Job cancellation is not reported as success. This release deliberately provides read-only job monitoring; it does not introduce run-once or arbitrary cog controls.

Incidents group unexpected managed process exits, Discord unhealthiness lasting 60 seconds, three consecutive WWM authorization failures, three failed job iterations, storage errors, dropped telemetry and sustained resource pressure. Acknowledgement and notes do not resolve an incident. Recovery is evaluated separately. A failed history disk cannot persist its own incident until writable again; the status error/stale indicator remains the immediate signal. No Discord messages or other external notifications are sent.

Defaults for resource incidents are CPU ≥90%, RAM ≥90% or free disk <1024 MiB, sustained for 300 seconds. Optional ignored `local.json` configuration:

```json
"incident_thresholds": {"cpu_percent": 90, "memory_percent": 90, "disk_free_mb": 1024, "sustained_seconds": 300}
```

History migration is additive to SQLite schema version 2 and preserves existing requests, logs and audits. Telemetry versions 1 and 2 are accepted, so upgrade the panel before restarting bot/Activity processes. New fields are not reconstructed for old records. Incidents are kept for 30 days after recovery; job snapshots/command events use the existing 72-hour history policy and storage budget.

After reviewing and deploying this release, stop the old combined launcher and restart using `bash run.sh`. Do not run a second launcher alongside it. The new command/job telemetry requires a bot restart. No production deploy, Git pull or bot restart is performed by the dashboard itself.

New authenticated read interfaces: `/api/analytics`, `/api/requests/{id}/investigation`, `/api/jobs`, `/api/commands`, `/api/commands/summary`, `/api/incidents`. Incident acknowledgement uses `POST /api/incidents/{id}/acknowledge` with the existing same-origin/CSRF checks. Additional allowlisted diagnostics check storage and local auth-context readiness without network login.

A manually launched private control panel for the test bot on Windows and the production bot on Raspberry Pi. The React bundle is committed under `public/`; the Pi does not need Node.js.

## Windows

From the bot directory, install the optional panel dependencies once:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-panel.txt
& '.\Start WWM Control Panel.cmd' --setup --owner 'RindouKobayashi@github'
& '.\Start WWM Control Panel.cmd'
```

Open the private URL printed by the launcher while signed in to Tailscale. The panel starts with the bot stopped or externally managed; choose **Start stack** when ready. Stop an existing manual bot/Activity launcher yourself before taking ownership in the panel. The panel cannot take over or terminate external services.

The owner string must exactly match the login Tailscale forwards, including `@github` for a GitHub identity. A rejected login never falls back to permitting any tailnet user. Correct the owner with `--setup --owner ...` from the host, then relaunch.

## Raspberry Pi

Use your existing checkout, Linux `.venv`, root `.env`, Activity production profile and saved-login setup. After reviewing, committing and pushing the staged changes on your computer, pull the same reviewed commit on the Pi. Stop the old bot/Activity launcher before switching to the panel-owned stack.

One-time setup on the Pi:

```bash
cd ~/Desktop/wwm-guild-bot
git pull
.venv/bin/python -m pip install -r requirements.txt
bash 'Setup Raspberry Pi.sh'  # validates/preserves production configuration
bash 'Start WWM Control Panel.sh' --setup --owner 'RindouKobayashi@github'
```

Keep the Pi's production `.env`, `activity/.env.production` and private auth context on the Pi. Do not copy the computer's `.env`, `local.json`, database or Windows `.venv`. If setup needs Tailscale HTTPS consent, complete the consent link on your own device and rerun setup.

For normal startup, run:

```bash
bash run.sh
```

This starts the **private panel plus production bot, Activity and Activity tunnel** in the background, surviving an SSH disconnect. Find the panel URL in `activity/logs/production/launcher.log` and open it from your Tailscale-connected computer or phone: **https://PI-HOSTNAME.TAILNET.ts.net:8443**. A desktop browser on the Pi is not required. Startup is recorded in the panel's operation history; if a component fails, the panel remains available to inspect and retry it.

```bash
tail -n 25 activity/logs/production/launcher.log
bash new_kill.sh  # stops the recorded panel and the services it owns
```

For foreground operation, `bash 'Start WWM Bot and Activity.sh'` starts the same panel-owned stack. Keep that terminal open or use tmux; Ctrl+C stops it and its owned services. To run **only the panel** without starting the bot, use `bash 'Start WWM Control Panel.sh'`. Closing a browser tab stops nothing. To stop only the bot stack while keeping the panel available, use **Stop stack** in the dashboard.

No system service is installed and nothing starts automatically after reboot. Start `bash run.sh` again after a reboot. The private Serve route remains configured. The `--activity-only` option retains the older Activity-only launcher; such externally managed processes cannot be controlled by the panel until stopped and relaunched under panel ownership.

Optional live API validation requires the existing `wwm_auth` private context and dependencies; headless renewal additionally requires its documented .NET helper. These private files never travel in Git.

## Network and access

- Panel backend: **127.0.0.1:8770**, never LAN-bound.
- Private panel: **https://DEVICE.TAILNET.ts.net:8443**, through Tailscale Serve.
- Existing public Activity: HTTPS **443**, pointing at test **8767** or production **8768**.
- Setup inspects background and foreground routes and refuses public Funnel or conflicting routes on **8443**. It never runs a Tailscale reset command.
- All panel pages and browser APIs require the exact owner identity, expected host, and (for changes) same-origin CSRF validation. Use the HTTPS Serve address even when on the same computer.
- The ingestion endpoint requires a random per-panel-launch secret passed to owned children through their environment. It is not a browser API.
- Tailscale ACLs/grants can further restrict 8443 to your identity. This code does not edit tailnet policy or expose a Funnel route for the panel.

Host administrators and other programs running as your local user are trusted: they can inspect process environments and make localhost requests. Do not expose a second reverse proxy to the panel backend.

`control_panel/local.json` is ignored and holds owner, hostname and optional `other_url`. Set `other_url` to the other machine's private HTTPS panel URL to enable the machine switcher. Only same-tailnet HTTPS URLs on port 8443 are accepted. Each panel controls its own host only.

## Service behavior

The panel and existing combined CLI share the same supervisor and environment checks. Stack startup is Activity → public Activity tunnel → bot. Stack shutdown reverses that order. Matching external services are displayed as external; destructive individual controls refuse them. A stack stop skips external services. A restart with external services fails preflight before stopping anything.

The panel uses OS file locks, owned subprocess handles and a direct-bot process lock. It never kills a process by a stored PID. Start failures roll back newly started dependencies; process crashes remain visible and are not automatically restarted. Graceful shutdown has a 10-second deadline, then records forced termination if needed. Owned bot/Activity processes accept a per-instance shutdown message over their private stdin pipe, so graceful shutdown also works without a Windows console. Other processes use process-group Ctrl+Break on Windows or SIGTERM on Linux.

Production stop/restart requires typing the machine hostname and `production`. Diagnostics are limited to configuration, runtime assets and a 90-second saved-login validation. The live validation may refresh the configured game-account session and displays an explicit confirmation. There is no shell, arbitrary HTTP request runner, secret editor, deployment command or reboot button.

## Telemetry and privacy

The shared WWM helper emits one logical request with individual retry attempts and safe decoded previews. Existing return values and authentication/retry behavior are preserved. Activity requests have their own request ID; downstream WWM calls retain it as their parent. HTTP 200 with no recognized application success code is **unknown**, not a validated success.

Only exact configured endpoints capture allowlisted payload fields. Chat and unknown endpoints retain metadata only. Unknown fields and personal text are omitted, so dynamic result-map keys are anonymized and unrecognized fields may appear empty; this is intentional. Previews are capped at 16 KiB, lists at 40 items, and nesting at seven levels. Login-provider bodies, headers and credentials are never captured. Secrets are not included in exports.

Structured log records retain severity, module, safe literal templates, exception type and sanitized stack frame locations. Raw f-string messages and legacy subprocess output are summarized, since they can contain arbitrary player/chat/credential data. Full raw logs remain local for trusted terminal inspection. Legacy file tails handle truncation and rotation; subprocess logs rotate at 5 MiB with two backups. The existing bot info log now rotates at 5 MiB with three backups instead of overwriting on every startup.

Bounded queues isolate the bot from panel/network failures and report dropped event counts. Heartbeats arrive every five seconds; fifteen seconds without a heartbeat marks health unknown. Background tasks expose running/failed state, not a claim that every iteration succeeded. Tunnel configuration is distinct from externally verified public reachability.

SQLite history is separate for test and production under ignored `control_panel/data/`: logs/traffic 72h, minute metrics 14d and operation audit 30d, with a 250 MiB database budget and space reserved for journaling. Old telemetry is removed first. A storage error is visible and controls cannot falsely report a persisted success. SSE sends invalidations; five-second polling is the fallback. Closing the panel during an operation records interruption rather than success.

## Development and verification

```powershell
cd control_panel
npm ci
npm run build
cd ..
.\.venv\Scripts\python.exe -m unittest discover -s control_panel/tests -v
.\.venv\Scripts\python.exe -m unittest discover -s activity/tests -p 'test_*.py' -q
```

For a read-only local fixture preview, run `python -m control_panel.tests.preview` and open `http://127.0.0.1:8771`. This separate test app reads no credentials and starts no services. It is not the authenticated panel and must never be exposed with Serve/Funnel.

React Bits SpotlightCard, MagicBento and AnimatedContent are bundled locally. MagicBento's demo artwork/icon dependencies were removed; runtime data replaces the demo content. Client-only directives were removed for this Vite app. See `REACT-BITS-LICENSE.md` and `THIRD_PARTY.md`. Reduced motion and keyboard focus are supported.

Actual Raspberry Pi hardware, its Tailscale route and live game login must be validated on that device; local tests do not establish remote production readiness.
