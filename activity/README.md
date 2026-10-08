# WWM Discord Activity

Pi Git-pull setup: from the bot folder run `bash "Setup Raspberry Pi.sh"`, then `bash "Start WWM Bot and Activity.sh"`. See **Raspberry Pi Setup.md** in the repository root for the first-run prompts and Discord settings. The root `.env` remains your bot configuration; setup writes only the local production Activity profile. The prepared public runtime/artwork is now included in Git, excluding private snapshots.

## Run on this Windows computer

Double-click **Start WWM Bot and Activity.cmd** in the bot folder. It starts **tester**, the authenticated Activity on loopback port **8767**, and Tailscale Funnel. This is the main launcher. Leave its window open; Ctrl+C stops processes it started. It refuses a second launcher or an already-running guildbot.py rather than starting a duplicate bot.

The saved tester root URL Mapping is **smol-computer.tail265e5f.ts.net**. Normal restarts keep this address. You do not need to copy a new hostname every time. Tailscale must remain signed in, the computer must be online, and Funnel must remain approved. Renaming/removing the Tailscale device or changing the tailnet DNS name can change the address.

**activity/Open WWM Discord Test.cmd** starts only the Activity and Funnel, without the bot, using the same test profile. It now uses Tailscale too. Do not run both launchers together. An existing matching web service or Funnel can be reused; reused services remain running after exit. Only owned processes are stopped.

**activity/Open WWM Activity Preview.cmd** is an independent offline snapshot preview on port 8766. It does not start the bot or use Funnel. The preview is clearly marked as saved data.

## Test and production

| Setting | This computer / test | Raspberry Pi / production |
|---|---|---|
| Discord app | tester | Goose Overlord |
| Activity profile | activity/.env.test | activity/.env.production |
| Bot token selected | DISCORD_API_TOKEN_DEV | DISCORD_API_TOKEN |
| Branch | dev | main |
| Web port | 8767 | 8768 |
| Logs | activity/logs/test | activity/logs/production |
| Public hostname | smol-computer.tail265e5f.ts.net | Pi's own Tailscale hostname |

The launcher validates that each profile matches the correct bot token. It never silently substitutes the production bot for tester. Production credentials have not been copied or configured, and the Pi has not been deployed. The original activity/.env is retained for compatibility; the current launchers use explicit profiles. Never copy test credentials over production.

For production, see deploy/PI-SETUP.md. The Pi service runs the Activity separately from your existing bot service. Its own Discord URL Mapping is configured once against the Pi's hostname. Tailscale Funnel uses HTTPS port 443 on each device; use separate devices for test and production. Keep Tailscale itself running independently of the bot.

## Credentials and access

Copy the matching .env.test.example or .env.production.example if creating a new profile. Enter the application's OAuth client secret locally. Secrets and the bot's root .env are never served to the browser. Player guild searches check each viewer's current Discord character binding in data/guild_verification.db. Rankings require Discord sign-in. Saved snapshots are blocked in Discord mode.

TUNNEL_PROVIDER=tailscale selects the stable tunnel. cloudflare is retained as an explicit fallback for temporary URLs, not the default. First-time Tailscale Funnel use requires account/HTTPS approval. No RAM reads are used.

## Data and UI

HR/ST and Abyss rankings use original artwork and mapped skills. Team profiles are resolved by each member's recorded host, and selected guild match rosters load participant names. Missing/deleted profiles show Name unavailable. Current names may differ from names at the time of the run. Missing statistics show a dash, not an invented zero. Personal Arena history and historical dungeon damage remain unavailable.

The bot startup adapter in utility/activity_commands.py preserves the Portal-managed Launch entry point during command sync. Deploy that helper and the changed guildbot.py together when updating the Pi bot. The Activity-only ZIP does not replace the bot entry point.

## Build and checks

Run npm ci --ignore-scripts and npm run build inside activity to build the frontend. From the bot folder, run python activity/deploy/package_runtime.py to create the Pi runtime ZIP. It excludes credentials, snapshots, captures, Windows environments and node_modules. Rebuilding snapshot data with activity/build_preview_data.py requires the existing local extraction/research files; running the built runtime does not.

Check configuration without launching any bot:

```sh
python activity/run_stack.py --environment test --check
```

Offline tests: python -m unittest discover -s activity/tests -p 'test_*.py'. Browser checks are in activity/tests/browser.cjs. The launcher has been checked without running either bot during verification.

Sessions/caches are process-local; use one worker. A restart asks Activity users to sign in again. Shared selection synchronization is not implemented. Review production rate limits before a broad public rollout.

Official references: https://docs.discord.com/developers/activities/building-an-activity and https://tailscale.com/docs/features/tailscale-funnel
