# Control panel commit review

This is the review guide for the staged private-panel changes. Nothing has been committed or pushed.

## What this commit adds

- Private Python/aiohttp panel with owner-only Tailscale access, CSRF protection, SQLite history and host metrics.
- React/TypeScript interface and its prebuilt static bundle, including the React Bits source and license notices. The Pi needs no Node.js installation.
- Safe WWM API request/response previews, retry grouping, Activity request correlation, structured logs and heartbeats.
- A shared supervisor with duplicate-launch locks, per-process ownership, cooperative shutdown and bounded log rotation.
- Manual panel launchers for Windows/test and Raspberry Pi/production.
- Combined Pi startup: `run.sh` → `Start WWM Bot and Activity.sh` → panel with `--start-stack`. `new_kill.sh` recognizes the panel launcher as well as the old launcher.
- Tests for authorization, isolation, storage, payload privacy, process shutdown and combined startup.

## Behavior to review carefully

Normal Pi startup now requires one-time private panel setup. `run.sh` starts the panel and its bot stack in the background; it prints where to find the panel URL. The panel stays available if a managed component fails. It does not open a desktop browser on the Pi. Open the HTTPS URL from a device on Tailscale.

`Start WWM Control Panel.sh` still starts only the panel. The Activity-only option still uses the old CLI. Existing external processes cannot be silently adopted, restarted or terminated by the panel. Stop the old launcher before switching.

The public Activity stays on HTTPS 443; the panel uses private HTTPS 8443 and loopback 8770. The panel owner is configured separately on each host. There is no public dashboard route, shell console, secret editor or automatic startup after reboot.

Logging changes from overwrite-on-start to rotation. The browser displays safe templates and summaries rather than arbitrary raw chat/player/credential text. Full raw logs remain local. Payload previews deliberately omit unknown/private fields.

## What is deliberately excluded

Existing edits to cogs, Activity player/ranking UI, asset research, authentication research and generated game data are not part of this staged commit. The existing authentication-provider introduction in the main Pi setup document remains unstaged; only the new control-panel startup note is staged there.

No `.env`, `control_panel/local.json`, history database, private auth context, log, virtual environment or `node_modules` is staged. Prepared public frontend assets and the npm lockfile are staged so the Pi can run without a build step.

## Review before committing

```bash
git diff --cached --stat
git diff --cached -- control_panel/README.md control_panel/__main__.py control_panel/server.py
git diff --cached -- run.sh 'Start WWM Bot and Activity.sh' new_kill.sh
git diff --cached -- activity/run_stack.py activity/server.py guildbot.py utility/wwm.py settings.py
git diff --cached --check
```

The Git staged/unstaged views let you read the selected changes separately from your other work. Do not use `git add .` for this review; it would include unrelated work.

## Pi deployment after your review

Commit and push the selected changes through your usual branch workflow, then follow [the Raspberry Pi instructions](README.md#raspberry-pi). Pull that reviewed commit on the Pi, install requirements, preserve the Pi's production credentials, configure the owner/private route once, stop the old launcher, and run `bash run.sh`.

SSH authentication to the Pi was unavailable in this session. Actual production startup and hardware verification remain to be performed on the Pi.
