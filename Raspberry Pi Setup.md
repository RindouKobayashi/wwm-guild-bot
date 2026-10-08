# Raspberry Pi: same repository, production configuration

After committing/pushing these changes, SSH into the Pi and run:

```sh
cd ~/Desktop/wwm-guild-bot
git pull
bash "Setup Raspberry Pi.sh"
```

Run as your usual `smolsus` user, not with sudo. Setup uses your existing Linux `.venv`; if missing it creates one and installs requirements.txt. Python 3.10+ and the matching Python venv support are required. It does not copy the Windows environment. Keep the Pi's existing root `.env` with `GITHUB_BRANCH=main` and Goose Overlord's `DISCORD_API_TOKEN`. Setup validates the bot identity without printing its token and never overwrites that file.

On first setup:

1. Enter **Goose Overlord's OAuth2 client secret** at the hidden terminal prompt. This is not its bot token and is different from tester's OAuth secret. If the root .env already contains DISCORD_CLIENT_SECRET, setup can use it. The Activity profile is saved locally as `activity/.env.production`, with owner-only permissions. Subsequent setup runs preserve it.
2. If Tailscale is missing, setup offers to install it using Tailscale's official Linux installer. It uses sudo only for system installation/device management. Sign in through the URL printed in the SSH terminal. Setup grants your normal Linux user permission to manage this device's Tailscale serving configuration.
3. Setup prints the **Pi's stable ts.net hostname** and the launch command. It does not start the bot.

Stop your old manually running guildbot.py using Ctrl+C, then run:

```sh
bash "Start WWM Bot and Activity.sh"
```

This starts Goose Overlord, the Activity on `127.0.0.1:8768`, and the Pi's HTTPS Funnel. First use may print a one-time Funnel/HTTPS approval URL; open and approve it in your browser. The hostname remains stable across normal restarts.

In **Goose Overlord's** Discord Developer Portal, set OAuth2 redirect `https://127.0.0.1`, set Activity root `/` to the Pi hostname, and enable Activities. Its Launch entry point is preserved by the updated bot command sync. Do not change tester's Windows mapping.

Keep the SSH session open while running. Ctrl+C stops owned processes. For a run that survives disconnecting SSH, use tmux if available:

```sh
tmux new -s wwm
bash "Start WWM Bot and Activity.sh"
```

Detach with Ctrl+B then D. Reconnect with `tmux attach -t wwm`. To stop, attach and press Ctrl+C. This setup does not enable automatic reboot startup; systemd can be configured separately when desired. Do not also run the standalone wwm-activity system service on the same port; use one launching approach.

## Later updates

Stop the stack, `git pull`, rerun setup if configuration/dependencies changed, then start the same `.sh` launcher. Existing credentials, binding database and bot data stay local. All prepared Activity runtime files, extracted catalogue and original art are included in Git, so the Pi does not need the game folder, Toolkit research output, Node.js or an extractor. Generated snapshot files and secrets remain excluded.

Logs: `activity/logs/production/bot.log`, `discord.log`, `tunnel.log`.

For a preflight without installation, login or secret changes:

```sh
bash "Setup Raspberry Pi.sh" --check
```

The preflight validates the existing bot identity and runtime assets; the full setup validates Activity credentials/Tailscale configuration. Neither starts the bot. Test uses `.env.test`, tester and port 8767; production uses `.env.production`, Goose Overlord and port 8768. Linux launcher refuses an already-running guildbot.py owned by the same user.

Tailscale reference: https://tailscale.com/docs/install/linux
