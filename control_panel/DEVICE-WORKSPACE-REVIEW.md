# Device workspace — review and rollout

The Device page adds a local host overview, process list, full log/file viewer, energy-alert inspector, verified database backups and an operations timeline. A configured SSH target offers a terminal, logs and approved files in the same panel. Service controls elsewhere still operate this panel's host.

## Use

Choose **Device**, check the target, environment and username, then open the terminal or a workspace tab. Production connections and shells require the displayed machine name. The terminal runs as the normal host user; elevation follows that account's existing permissions. Windows elevation may require the desktop's UAC prompt.

Terminal sessions are temporary: two maximum, 30-minute input-idle limit, four-hour lifetime and 60 seconds to reconnect after detaching. Reconnect replays bounded output, never commands. Closing a session stops its attached processes. Input/output is not stored in panel audits. Commands can still write host files or normal shell history. The terminal deliberately provides the account's full shell access.

Full logs default to redacted text. Select a registered file, page backward/forward, follow the tail or search. Search is bounded and cancellable. Raw access requires deliberate confirmation, expires after five minutes and is bound to the browser, target and file. Downloads preserve the original bytes for raw access; redacted downloads mask sensitive values. Redaction is best effort, so inspect material before sharing it. Arbitrary filesystem paths, uploads and file editing are not exposed by the graphical viewer.

Energy alerts shows cached readings and, when the managed bot supports the new bridge, its most recent evaluation reason. Cached energy is labelled separately from current observations. This release does not change alert decisions or query the game for this inspector.

Backups use SQLite's backup API, integrity checks and SHA-256 manifests. They include only explicitly registered databases, not credentials or saved login files. They can contain member data; downloading requires confirmation. The limit is seven archives and 1 GiB; old backups are never silently deleted. There is no browser restore/upload feature. Separate databases are captured separately, not as a simultaneous cross-database transaction.

## Install after reviewing the staged changes

1. Commit/push the reviewed release and pull it on the Pi using your normal workflow.
2. Install `requirements-panel.txt` into each host's own virtual environment. It adds AsyncSSH and Windows-only pywinpty. The Pi needs neither Node.js nor the Windows virtual environment; the built interface is included.
3. On the connecting host, copy `devices.example.json` to ignored `devices.local.json`. Check the exact Tailscale hostname, Linux username, repository path and trusted SSH host-key alias. The example describes this Pi; change it when using another host.
4. Verify the target key through your established SSH setup. Missing or changed keys are rejected; the panel does not auto-accept them. Connections use existing Tailscale SSH authorization without importing private keys, forwarding agents or storing passwords. A Tailscale reauthentication link may appear when required.
5. Restart the panel manually after updating dependencies. Restart the bot normally when ready to enable its new energy-observation bridge. No reboot startup is installed.

Remote graphical access invokes the matching `control_panel.file_reader` helper from the target repository and virtual environment. Deploy that release on the target first; its panel need not be running. Missing helpers produce a setup message. SSH target administration remains host-side. Only configured targets on the same tailnet are accepted, with resolved addresses restricted to Tailscale ranges.

## Security and storage

Existing owner-only authorization, same-origin checks and CSRF remain. Device sessions additionally use an HttpOnly/Secure/SameSite browser cookie. Terminal attachment tickets are single-use, short-lived and browser/session-bound; WebSocket origins are checked. Shell output is bounded and backpressured. Terminal work, SSH and file reads are separated from health/history work.

The additive schema-5 migration adds metadata-only device audits, retained for 30 days. Raw grants, terminal buffers and SSH sessions stay in memory. Existing history/privacy retention remains. Reader access rejects symlinks/reparse-point escapes and accepts only opaque registered file IDs. Audit-write failures surface as storage errors without preventing terminal cleanup.

## Verification and deployment state

Windows: 103 panel tests (one Linux-specific skip), six refresh tests, six existing energy-alert tests and nine Activity server tests passed; TypeScript and the production build passed. Browser verification exercised a real PowerShell command, interruption, closure, the device overview and approved file viewing. Isolated Pi checks exercise actual SSH, terminal resize, fixture redaction/raw reads and Linux acceptance tests without restarting the production bot.

The production Pi repository, configuration and database have not been deployed or restarted by this work. Review this document and the staged diff before your normal commit/push/pull rollout. Local test servers are stopped after verification.
