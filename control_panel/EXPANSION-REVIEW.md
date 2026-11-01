# Expansion release review

## Included

- Full-window API aggregates and shared filters; logical requests remain distinct from retry attempts and Activity ingress.
- Correlated request, command, job and log investigation with fixed failure explanations.
- Public discord.py command hooks and explicit coroutine decorators on selected jobs; no command arguments or private library monkey-patches.
- Grouped dashboard incidents, acknowledgement/notes and automatic observed recovery. No external notifications or automated restarts.
- Runtime/build/asset identity, restart indicators, live service progress and bounded local diagnostics.
- Bookmarkable views, complete-window volume/error charts, mobile navigation and the prepared Pi frontend bundle.
- Additive schema migration and backward-compatible telemetry ingestion.

## Preserve existing work

The schedule and leaderboard cogs already contain unrelated edits. Only the added job-observation import and decorators belong to this release. Existing Activity UI, live-chat and research changes remain outside the expansion. Credentials, local panel configuration, history and node_modules remain ignored.

## Deployment

Review the selected diff, commit/push through your normal workflow, then pull that reviewed revision on the Pi. Stop the existing combined launcher and run `bash run.sh` again. The same private panel URL and owner login are retained. Restarting is required to obtain command/job telemetry from the new bot code. The schema change adds tables/indexes; it does not delete old history.

The Windows panel can be verified independently with no bot started. This release does not silently update or restart your running Pi production bot.
