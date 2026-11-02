# API Tools and approved message updates

## How to use it

Open **API Tools → WWM Tools**, choose Find player, Find guild, Combat plan or Rankings, enter inputs and press **Run lookup**. Typing never sends requests. Combat plans detect the server from the player lookup by default; explicit server IDs are available when needed. There is no authoritative server-name registry in the current runtime assets, so the interface does not invent geographic names for those IDs.

Results show approved fields in a table first. Sanitized JSON, attempts and timing are available separately. You can copy/download the sanitized result, open its traffic records, save inputs as a local preset, reopen retained results, and compare two results from the same tool. Omitted/truncated fields are marked unavailable during comparisons. Presets are machine-local, excluded from Git/support exports, and limited to 50. Results retain their original timestamp and environment.

**Bot Activity → Approved message updates** lists configured existing leaderboard, schedule and market-report messages when the managed bot is connected. Select a target and choose **Preview update**. Review the machine, guild, channel, message ID and generated content. Confirm the preview, then choose **Publish to Discord**. Production additionally requires typing the panel hostname. Previews expire after two minutes; target edits invalidate them. Manual actions edit the exact preview, suppress mentions, do not assign roles, and never create a replacement for a missing message. Normal scheduled jobs retain their current behavior and schedules.

If the panel loses contact during publishing, the operation is **unknown**, not successful. **Check target without resending** verifies target accessibility and gives its Discord link; it does not infer that the uncertain update succeeded. Repeated clicks use the same preview's operation identity, not a new publication.

## Approved custom API profiles

The Advanced client starts empty. Copy `profiles.example.json` to ignored `control_panel/profiles.local.json` on the machine that should call the API. Replace the example origin, paths and fields with the service you approve. Set credential environment variables before launching the panel. The sample Authorization variable must contain the complete header value, such as a Bearer prefix plus its token. Never put a credential value in the profile or browser.

Each profile defines an ID/name/description, exact HTTPS origin on port 443, fixed paths, methods, scalar query/body/header fields, credential-header environment references, response format (`json`, `msgpack` or `text`) and response field allowlist. Allowlisting is recursive: nested fields must also be approved. Do not approve personal text fields. Text responses currently show byte counts with text omitted; use a structured response profile to inspect actual approved values.

GET is the default. POST must explicitly declare `"read_only": true` and be an approved lookup operation. Arbitrary destinations, credential entry, auth headers from the browser, redirects, proxy inheritance and private/reserved network addresses are rejected. DNS answers are checked at connection time, and the validated addresses are used by the connector. Custom calls appear separately in traffic and are excluded from WWM health/latency totals.

## Runtime and data

- One manual API run per machine; no waiting queue. Each run has a disposable child worker, a 30-second outer deadline, at most one retry per request, a 64 KiB body cap, a 2 MiB decoded response cap, and 16 KiB sanitized previews. Cancellation stops the worker and future retries; it cannot recall a sent request.
- WWM helpers use the host's static token or recent saved-login token cache. API Tools never invokes login/renewal. An unavailable cache produces a readiness explanation. Existing explicit live authentication diagnostics remain separate.
- SQLite schema 4 adds presets, tool results and preview metadata without resetting events. Results use 72-hour retention, operation audits 30 days, and expired preview metadata is pruned. Preview message bodies remain in bounded memory only and never enter operation audit, telemetry, presets or support exports. The existing overall database budget includes the new tables.
- Bot actions use an authenticated ephemeral loopback listener advertised by the exact supervised bot's heartbeat. There is no second Discord session or public route. Preparation and message construction are shared with scheduled jobs, and execution locks prevent overlap. Only the three registered message-edit actions are available.
- Health sampling and bounded history readers remain independent. Operation polling stops at a terminal state. Slow history, missing bot heartbeat and lost panel connection remain distinct.

## Troubleshooting and export

Request investigations explain auth rejection, rate limiting, timeouts, decoding errors and general failures, with retained success context and appropriate local diagnostic buttons. Incidents show deterministic next steps. **Run health checklist** combines configuration, assets, storage and heartbeat checks without login. Diagnostics includes a support-bundle inclusion preview; bundles exclude owner identity, secrets, presets, raw tool inputs and posting preview bodies.

## Review and rollout

The frontend bundle is included; Pi needs Python dependencies already used by the project, not Node.js. No new package dependency is introduced. Review/commit/push through the normal workflow. On the Pi, stop the old launcher, pull the reviewed commit and start `bash run.sh`. A bot restart is required to advertise the action bridge. Do not copy Windows credentials, virtual environments, profiles or databases onto the Pi.

The local test panel can serve the new interface without starting the bot. Production has not been deployed/restarted by this work. Live Discord publishing requires a separately confirmed preview on a test-server target; automated action tests use mocks and send no messages.

Validation commands:

```text
python -m unittest discover -s control_panel/tests -q
python -m unittest discover -s activity/tests -p test_*.py -q
npm test --prefix control_panel
npm run build --prefix control_panel
python control_panel/tests/pi_tools_check.py
```

The isolated Pi check uploads only application/test code to an automatically removed temporary directory, runs mocked API/action checks with temporary logs, and leaves production configuration, history and running services alone.

Verified: 89 panel tests, 31 Activity tests and 6 refresh-scheduler tests on Windows; 26 focused tests on the Pi's ARM64/Python 3.11 runtime. The frontend TypeScript/production build passes. Browser checks cover inline errors, empty custom profiles, unavailable bot actions, mobile overflow and keyboard-labelled inputs. No live game lookup or Discord publish was performed as part of this verification.
