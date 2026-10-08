# Raspberry Pi deployment

Target supplied by the owner: `smolsus@smolsus`, `~/Desktop/wwm-guild-bot`.
Windows could not resolve this hostname. Obtain the Pi's LAN IP or try its configured mDNS name. No remote files have been changed yet.

Copy only the Activity runtime bundle into the existing bot's `activity` folder. Preserve its existing `.env`, Linux `.venv`, data and bot process. Never copy the Windows virtual environment onto Linux. The runtime bundle excludes snapshots, node_modules, captures, tools, credentials and tests. Its ZIP contents extract under `activity/` in the bot directory.

1. On the Pi, copy `activity/.env.production.example` to `activity/.env.production`. Fill in the same Discord application's OAuth client ID and secret locally. Keep the bot's own `.env` intact. The Activity loads its production profile; existing utility code loads the bot configuration when needed.
2. Run `bash activity/deploy/install-service.sh` as `smolsus`. This checks the existing Linux Python environment and installs only `wwm-activity.service`. It uses port 8768 on loopback and automatically restarts after failure/reboot. It does not launch or restart the Discord bot.
3. Verify locally on the Pi: `curl --fail http://127.0.0.1:8768/api/config`. It must report `mode: discord`.
4. Install and sign in to Tailscale on the Pi, and approve Funnel for that device. For an initial test, run `tailscale funnel --https=443 http://127.0.0.1:8768`. Configure the Pi's own ts.net hostname as Goose Overlord's `/` URL Mapping. Keep tester mapped to the Windows device's hostname.
5. For unattended production, configure Tailscale Funnel in background mode on the Pi after verification (`tailscale funnel --bg --https=443 http://127.0.0.1:8768`). This persists public serving until disabled. Tailscale and wwm-activity must both start after reboot. No domain purchase or router port forwarding is required.

Discord configuration: add the OAuth redirect URI `https://127.0.0.1` according to Discord's Activity guide; configure the `/` URL Mapping, enable Activities, and use the default Launch Entry Point. Test as an application owner/tester before broad distribution. The server must use the same Application ID as the Portal mapping and the bot whose bindings it reads.

Useful commands:

```sh
sudo systemctl restart wwm-activity
sudo systemctl stop wwm-activity
journalctl -u wwm-activity -n 80 --no-pager
```

One worker only: current sessions/caches are in memory. A restart asks viewers to sign in again. Unbound users cannot query guild reports; bindings are checked against the Pi's existing `data/guild_verification.db`.

References: https://docs.discord.com/developers/activities/building-an-activity and https://developers.cloudflare.com/tunnel/
