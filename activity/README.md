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


## Player profiles and shared deployments

The Players page supports a 10-digit Number ID (including leading zeroes) or exact nickname. My character uses the authenticated viewer's local bot binding. The server checks that binding on every player/guild request. It resolves the player and their host through the existing WWM utility, then returns only the displayed profile fields: name, Number ID, signature, level, status, region, mapped sect, account creation, online hours, achievement counts and current guild name. It does not return the raw player response, internal PID, credentials or other users' Discord bindings.

Guild name lookup can fail independently without hiding the player profile. Missing values remain unavailable; no guild is distinct from unreported guild membership. Invisible status takes precedence over online status. A profile's guild battle button opens the same player in the existing reports page; personal Arena match history is not provided.

The same committed server, player_profiles.py, public frontend, catalogue and art run on both hosts. No game folder, extraction tools or Node build is required after pulling the prepared runtime. Windows uses Start WWM Bot and Activity.cmd with activity/.env.test and port 8767; the Pi uses run.sh with activity/.env.production and port 8768. Credentials and data/guild_verification.db stay local and ignored by Git. Do not copy one host's profile or database over the other. Each bot keeps its own bindings. Frontend HTML, JS, styles and catalogue request revalidation so reopening an Activity after deployment can pick up updated files.

Update Windows: stop the combined launcher with Ctrl+C, pull the commit, and restart the .cmd. Update Pi from the repository folder:

```bash
bash new_kill.sh
git pull
bash run.sh
```

Close and reopen the Activity after restarting the service. No new dependencies or Developer Portal mapping changes are required for player search. The original one-time OAuth and Funnel setup remains necessary on each host.

Offline checks: python -m unittest discover -s activity/tests -p 'test_*.py'. The optional player UI check requires Node 20+ and Playwright: node activity/tests/player-browser.cjs. WWM_TEST_BROWSER can select an installed Chromium browser; all identity and game responses in this check are fixtures, and no Discord bot is started.


## Expanded player details

Profiles now include always-visible Energy & activity, Masteries, Base attributes, Martial arts & sect, Arena summaries and Fashion & social panels. These use the same data fields and existing rank/sect mappings as the bot. Energy follows the bot's cap and regeneration interval: online readings are authoritative, offline totals are estimates, and missing base readings are labelled regeneration floors. Hidden birthdays are not displayed. Arena summary totals and ranks do not imply access to personal match history.

Equipment, Collections, Social and Homestead load automatically and remain visible. Equipment shows extracted item/affix names, game formatting, formatted reference ranges and percentages of the exact affix maximum. Collections have name/category filtering with all returned rows in a scrollable table; category scopes prevent currency/title/outfit ID collisions. Unmapped or ambiguous IDs are labelled explicitly. Social resolves partner names on their recorded hosts and reports topic likes; partial failures stay visible. Homestead displays the returned name, level, Bounty Gourd, prosperity, mate and description.

activity/player_mappings.json contains only extracted static names/constants, not player captures. It is tracked and included in runtime packaging, so the Pi does not need the mapping workbooks or a generated affix database. When those source files are updated, regenerate it from the bot folder with python activity/maintenance/build_player_mappings.py and commit the resulting JSON. The exporter reuses the existing bot's constants and original extracted CSV/XLSX names; do not edit guessed names into the export. Launchers and Pi setup check that the complete runtime was pulled.

Player search automatically loads equipment, collections, social and homestead records into separate visible panels. A failed section does not hide the others. Equipment cards show percentages of exact-ID affix maxima, tier, quality, set, relay status and readable base attributes; these percentages are not graduation scores. Collections show all returned rows in a scrollable, filterable table.

## Equipment metadata and the profile layout

The profile overview puts Martial arts & sect and Fashion & social inside the main profile, with Energy, Masteries, Base attributes and Arena alongside it. Equipment follows in explicit offensive (1, 2, 10, 11), defensive (3, 4, 8, 5), and ring/bow (9, 21) groups. Wide screens show 4/4/2 cards; smaller screens retain grouping in two or one columns. Tier, quality, set and relay state take priority over durability and retune counts.

Affix percentages are value / exact-ID maximum, not normalized distance between minimum and maximum. For example, 73.132 / 77.8 is 94%. Bound comparisons allow tiny floating-point differences, but genuine out-of-range values have no misleading bar. Both range endpoints use the affix formatter. The installed legacy table supplies tier-specific relay transfer limits (94% for tiers 91 and 96); these are transfer limits, not a separate graduation score. Relay state comes from the returned legacy origin field. Quality codes use the standard Common/Uncommon/Rare/Epic/Legendary labels (4 purple, 5 gold); set names and tier numbers are extracted, not inferred from equipment names.

To refresh metadata on the asset-extraction computer, run from the bot folder:

```sh
python activity/maintenance/extract_equipment_metadata.py
python activity/maintenance/build_player_mappings.py
```

The extractor reads the sibling Toolkit's schema catalogue, latest equipment details CSV and installed game archives without editing them. It validates each equipment ID and localization key before exporting metadata. Optional --game-root and --details arguments select the matching installed assets/export. Output data/equipment_metadata.json is an intermediate; commit activity/player_mappings.json and the maintenance scripts. The Pi and tester use that same prepared JSON without requiring the game installation. Refresh the Toolkit schema catalogue and equipment export after game updates before regenerating metadata. Unknown records remain explicitly unavailable.

Leaderboard rows and selected Abyss results show date, time and timezone when a run timestamp is supplied. No-hit score timestamps are decoded in that mode only. Fastest/Path samples currently provide no completion timestamp; they display Completion time not reported. A player creation date or a fetch/cache time is never used as the run date. Previously checked PvP and guild report readers do not return reports for these Abyss battle-log IDs.
