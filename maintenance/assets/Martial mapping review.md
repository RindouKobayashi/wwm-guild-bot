# Kungfu extraction and remaining mapping audit — 16 November 2026

## Delivered and how to review

The generated `assets/martial-review/` contains 270 localized combat kungfu entries, 11 shared path icons, source receipts and an illustrated Discord upload pack. It is a review copy, not a second runtime source. The normal portable release has also been generated under `.asset-builds/20261116T131747Z-da056f/release/`. Its report compares the 20 legacy names with extracted data, including new alternate/encounter kungfu. Twenty-four decoded entries without a martial path are excluded explicitly; extraction does not claim they are ordinary playable kungfu. Hidden/client definitions do not prove current regional availability.

The catalogue is now applied and ready to commit; the Pi does not need a separate Apply step. The staged bundle preserves the asset data that was already staged before this work. The working-tree bundle separately preserves your additional unstaged asset edits, so they are not silently included in the commit.

## Normal Pi workflow

1. Review and commit the staged changes here, then push.
2. On the Pi: `git pull`, then `bash run.sh` (or your existing bot/panel launcher).
3. Before the Discord bot starts, it validates the installed bundle and the exact icon approval manifest, verifies the authenticated application, uploads only missing approved icons, and reuses existing icons.
4. Assigned IDs and interrupted-upload receipts live in ignored `data/`, so the checkout stays clean. The bot then loads the local IDs automatically. No Node.js, game installation, extraction or manual emoji-ID editing is required on the Pi.

The reviewed `martial/startup-approval.json` enables automatic uploads for test application `1289424877916393547` and production application `1488802246081773669`. It pins the catalogue and upload manifest by checksum. The public `assets/discord/martial-applications.json` supplies these identities for future review candidates. Changing targets or artwork requires another reviewed commit. Startup does not delete emojis or post messages.

Sync has a 45-second overall deadline. On failure the bot continues starting with names and any matching receipts already saved. It records an **Icon sync incomplete** log and `data/martial-emoji-sync-status.json`; it tries again only at the next manual start. An uncertain POST is reconciled against Discord before any resend. Explicit rejections clear their pending markers; ambiguous outcomes are retained. Normal bot authentication/connectivity failures remain normal bot failures.

For a future game update, extract/review/apply on the main computer before committing. The updater generates the next checksum-bound startup approval together with the bundle. The Pi consumes the committed result only.

## Evidence and limits

The actual combat table has `kongfu_id`, `name`, `liupai_id`, `kongfu_icon` and weapon/skill definitions. Current schemas are index2:1494 and its index202:10951 overlay. Paths come from index3:787, keyed on `liupai_id`, with `name` and `liupai_small_icon`. Frames are discovered by schema, not hardcoded. Duplicate same-archive candidates, malformed rows, padding errors and unsupported archive namespaces fail closed. Explicit patch rows override base rows; current source field signatures and locators are included in the catalogue.

Recovered game code provides independent corroboration: `hexm/common/misc/wuxue_misc.lua`, function `get_wuxue_liupai_attr_by_id`, looks up `datam.kongfu[kongfu_id].liupai_id`, then `datam.neigong_liupai_data` for the localized name and small icon. The player-card equipment controller passes `curr_plan.kongfu.kongfu_main/sub` to its martial display. Appearance-item IDs were not used.

Original art is selected from the installed resource registry using the overseas regular UI variant (`<small-icon>_oversea_0_0_ui`), preserving the bot's existing overseas icon style. The 4k variants are not mixed into that choice. Exact resource names, registry fingerprints, physical lookup receipts and original/upload-image hashes are recorded. The game's semantic version is explicitly unknown; installed content is identified by hashes. The packed-row reader reuses the existing scalar/container readers and adds the zero-width false tag observed in `is_chiji`; strict identity/padding checks guard the complete rows. No game Lua is executed.

## Discord files and procedure

Open `assets/martial-review/upload-pack/contact-sheet.html` or its PNG preview. Upload only the 11 PNGs in `icons/`, one per path, not 270 separate emojis. The manifest gives exact names, hashes and associated kungfu. These are **application emojis**, not server emoji slots or a bot avatar.

The helper defaults to a read-only preview:

```powershell
.\.venv\Scripts\python.exe maintenance/assets/upload_martial_emojis.py --environment test --pack assets/martial-review/upload-pack
```

Add `--upload` for interactive approval. `--only path_1` limits a check to one icon. Production requires typing the authenticated application ID. `--import-existing` verifies manually uploaded image pixels before saving assigned IDs. All create operations have durable pending markers; ambiguous network outcomes require reconciliation rather than automatic resend. The helper never deletes emojis or opens a gateway session. Concurrent modifying helpers are rejected by a host-local lock. Rate-limit waits are bounded.

The user-approved single test upload succeeded for application `tester` (`1289424877916393547`), and its check message was posted/read back in the configured test log channel:
https://discord.com/channels/409959440616390668/414234388776353828/1571877951752896553

The committed `assets/discord/martial-emojis.json` is an optional public seed containing the earlier approved test icon. All new helper/startup receipts are saved to ignored `data/martial-emojis.json`; automatic operation never rewrites tracked registry files. No production upload or Pi deployment has been performed during implementation. Missing uploads omit decoration without blocking lookup. New extracted kungfu without an explicit combat-role policy is labelled Undetermined, not assumed DPS.

## Remaining mappings

`martial-mapping-inventory.json` is a reproducible inventory of uppercase literal containers in runtime Python, including symbol, definition and consumer files, without copying values. Run `maintenance/assets/audit_manual_mappings.py` to refresh it. It is a candidate inventory, not proof every constant should be extracted. The following grouping covers dynamic/database mappings that a constant scan cannot establish.

| Group / priority | Current consumers and source | Correct next source / limitation |
|---|---|---|
| Already extracted | Shared `api_constants`, player equipment/affixes, sect names/statuses/vote allowances, dungeon/Abyss catalogues | Validated `assets/wwm` bundle. Literal compatibility dictionaries are not separate unmigrated features. Independent caller lists still need review. |
| Kungfu — delivered | Player search, chat formatting, Abyss recorded builds, Activity previews | Verified combat-ID/path joins and original icons. Legacy fallback retained with provenance, not presented as newly extracted data. |
| Avatars — high | `avatar_paths`, live-chat mapping UI and local portrait data | Needs its own ID/resource manifest and deployment coverage. Animated composites cannot be inferred from static images. |
| Market goods — high | `market_cog` approved `good_names` database | Verify the market `main_good` namespace before using item catalogues. Preserve user approvals and custom overrides; generic item IDs may collide. |
| PvP labels / quality — medium | Player-profile rank and quality presentation | Verify grade/quality table schemas and localized fields. Keep threshold logic and presentation colors separate; candidate table names alone are not evidence. |
| Homestead — medium | Farm quality/serial branches and NPC notification formatting | Recover farm-quality and notice definitions; notification choices remain application policy. |
| Equipment presentation — medium | Remaining slot/type labels and consumer-specific exclusions | Reuse verified equipment metadata where identities agree. Slot layout, grouping and exclusions are UI policy. |
| Guild titles — medium | `event_log_cog` role/rank labels | Prefer returned guild custom-post data; guild-renamed titles are live data, not global localized assets. |
| Breaking Army — separate behavior work | Scheduler boss lists, leaderboard session selection | Reuse extracted boss IDs only after verifying eligibility. Random rolls, exclusions, year boundaries and session timing are behavior/policy changes, not part of this migration. |
| Sect/likes integration | Sect channel slugs, market topic usage, remaining-vote calculations | Names/enums already extracted. Discord channels and vote reset semantics require explicit configuration/behavior work. |
| Protocol/routing/policy — retain | Request fields, endpoints, host routing, timeouts, Discord IDs, healer/tank sets | Keep named API contracts, environment configuration and bot policy. Assets cannot supply Discord-assigned IDs or user preferences. |

No energy rules, schedule semantics, guild permissions or market approvals were changed.

## Final verification

28 asset/upload checks, 37 Abyss/offline checks and 31 Activity tests pass. The stale assertion of exactly 54 Abyss trials was replaced by nonempty/unique-ID checks while retaining every original artwork checksum check; the current bundle has 60 trials. The sealed candidate also validates with Python site packages disabled, without Toolkit, Pillow, Discord or settings imports. This verifies dependency-free portable loading on Windows; no new ARM/Pi runtime test or production restart was performed. The approved test upload was followed by a read-only rerun that reports REUSE, and the test message was read back.

## Automatic-startup verification

Seven startup-specific tests cover fresh creation and reuse, clean tracked files, wrong application, tampered approval, uncertain POST recovery, explicit rejection retry, receipt merging and the nonfatal timeout. All 103 panel tests (one platform-specific skip), 31 Activity tests, 28 asset/upload checks and 37 Abyss/offline checks pass against the applied working bundle. Automatic upload execution was mocked; no additional Discord icons or messages were sent for this follow-up. Pi production remains running unchanged until your commit/pull/manual launch.
