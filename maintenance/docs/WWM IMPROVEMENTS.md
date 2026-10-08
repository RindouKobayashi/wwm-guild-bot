# WWM bot improvements

The changes are in this Discord bot project. The LLM code, settings, existing databases and unrelated user edits were left alone. No bot startup, Discord login, command sync, message sending or live WWM API tests were performed.

## Extracted ranking names and artwork audit

`/ranking view`, its autocomplete and the dungeon picker use the **98 extracted leaderboard records**. Names come directly from `trial_rank_ids.csv`'s localized leaderboard titles, keyed by literal `rank_name` and HR/ST namespace. Only the trailing `Top N` display suffix is removed for the short name. Challenge, phase, season, punctuation and other title distinctions are preserved. The full extracted title is also searchable.

Examples: `hr:1` / `Dream Jinming Pool` with type HR; `st:1` / `Formless Pass` with type ST. A bare ID shared by both types prompts a choice. Unknown numeric IDs remain usable with an explicit type. Community names and aliases are excluded from view lookup, labels and autocomplete. The legacy registry and management commands are preserved for existing data, but editing them does not change `/ranking view`.

**Artwork mapping corrected after the reused-banner error:** the earlier 92-board claim established only that a trial UI row referenced an image. Those rows contain copied/template images and are not sufficient evidence of correct dungeon artwork. The importer no longer displays `guild_image`, `info_pic`, `danlihui`, or the similarly copied boss-profile picture fields.

The current bundle displays **18 original boss introduction images across 38 boards** as dungeon-level galleries. It follows the trial's explicit `fuben_id`, checks `fight_entity_id` against boss-profile `boss_no`, follows `next_id` only within the same `instance_group`, and looks up original replay introduction artwork by the exact extracted boss name. All matching difficulty rows must produce the same complete gallery, and archive variants must agree on the relevant structural fields. Localization hashes may differ when their resolved English boss name agrees. `all_boss_id` alone is not trusted because some rows copy an unrelated profile ID.

The reported incorrect grouping is now separated:

| Extracted dungeon | Verified instance chain | Named original artwork |
| --- | --- | --- |
| Blazing Gale Dance (`hr:3`) | 42 → 43 | Murong Yuan; Twin Lions |
| Abyssal Lament (`st:11`, `st:12`, `st:60004`) | 57 → 56, with agreeing level variants | Coffin Master; River Master |
| Serpent's Fang (`st:13`, `st:14`, `st:15`) | 87 → 86, with agreeing level variants | Wolf Maiden; Snake Doctor |

These are original **boss introduction/background pictures**, not a claim to have recovered the exact composite dungeon-selection banner or a phase-specific portrait. The view identifies the bosses and shows the complete gallery. Some source images are backgrounds intended to sit behind an in-game model; no replacement character, border or animation is generated.

**60 boards remain unresolved and text-only.** Reasons include missing fight-entity profiles, stages without fight entities, missing named introduction artwork, disagreements between difficulty variants, and the six previously identified name/ordinal mismatches. The updated `artwork_audit.html` has one card per board, its original images and names, a resolution reason, and expandable mapping evidence. Older bundles and audits are preserved but superseded by the latest pointer.

PNG signatures/hashes and confined paths are verified before copying; each complete upload gallery must fit within an 8 MiB budget and Discord's 10-image limit. Team details/back navigation preserve every attachment, while returning to the picker clears them. No Discord bot or game API was started for verification.

### Refreshing and deployment

Run **Refresh WWM Ranking Assets.cmd**, or:

```bat
python maintenance/assets/import_dungeon_assets.py
python maintenance/assets/import_dungeon_assets.py --game-data-run "D:\exports\game_data_run"
```

By default this reads the sibling toolkit's latest completed game-data export. It copies the necessary PNGs and mapping/provenance into **data/ranking_assets/**. Bot runtime uses only this local directory; it does not import the toolkit, start the game, read game RAM or open a browser. New exports produce new bundles; old bundles are preserved. The bot notices the updated `latest.json` when the next ranking view is constructed.

**For deployment, copy `data/ranking_assets/` along with the code.** The project's existing `.gitignore` ignores `data/`; that policy was preserved, so a code-only Git deployment will fall back to text unless you include the asset bundle separately. The importer itself has no third-party package dependencies. Launchers use the bot's Python if present, otherwise the normal Python 3.11 launcher.

## Ranking fixes

- Navigating back to page 1 no longer jumps to the supplied player's page again. The initial lookup can still jump to their rank.
- Out-of-range page requests refetch the clamped page instead of displaying empty/wrong results under a corrected page number.
- `59.9999` seconds rounds to `1:00.000`, not `0:60.000`. Missing/invalid/non-finite scores display `N/A`.
- An unlinked caller is invited to link an account or specify a player; the view no longer falsely says they are absent from the board without querying a PID.
- Null attempt metadata is tolerated. Rank-line names are escaped and bounded; timestamps are compact. The ranking message edit disables mentions.
- Components V2 error views clear obsolete attachments and avoid exposing raw exception text. A failed usage-counter update no longer replaces an otherwise successful leaderboard.

## WWM API utility fixes

- An explicitly supplied request token now overrides the generated token; it was previously ignored.
- Generated authentication tokens are no longer printed in debug logs. Guild verification and market lookups use the fresh-token default instead of forwarding the legacy static settings token, preserving their previous generated-token behavior.
- Number-ID lookup uses the server number returned by that lookup instead of always routing through 10595. The existing default remains only when the response omits the server.
- Both Number-ID and PID helpers select the exact requested PID from a successful response. An unrelated first record or error payload cannot be returned as that player's successful full profile.

These paths were verified with mocked MessagePack/API responses, not live authentication or server traffic.

## Equipment/affix fixes

The profile equipment display now prefers the reference range for the exact affix ID. A fallback range combined across IDs with the same name is explicitly labelled. Values outside the reference interval are not falsely marked `MAX`, and cross-ID reference ranges do not claim an upgrade delta.

The affix mapper accepts exact integral IDs and rejects fractional floats, booleans and non-finite values without crashing or truncating them into a different affix ID. It continues to map only known affix positions; equipment/item/currency namespaces remain separate. Existing category-aware item-name handling was preserved.

## Offline checks

Run **maintenance/checks/Check WWM Offline.cmd**, or:

```bat
python tools/check_wwm_offline.py
```

**41 offline regression tests pass.** All 11 changed/new Python files also pass syntax parsing, and `git diff --check` is clean.

This selects only `tests_offline/test_wwm_offline_improvements.py`. It never discovers the existing `test/` live API probe scripts. Settings are synthetic, credentials are not loaded, API calls are mocked and unexpected aiohttp requests are blocked. Real cog/view constructors and component serialization are used; no `Bot` is created or started. The checks cover attachments, pagination, team navigation, profile construction, text budgets, token override, server routing, exact PID selection, reference ranges, affix namespace handling and asset confinement.

Discord rendering/upload acceptance has not been live-tested, as requested. Components V2 media galleries and attachment edits follow the [official discord.py interaction API](https://discordpy.readthedocs.io/en/stable/interactions/api.html).

## Further opportunities

Additional per-boss or per-phase art should be added only after confirming the underlying instance/boss relationships; shared table artwork is not sufficient evidence. Other useful improvements would be cached public leaderboard responses, a consistent timeout-disable/edit helper across the WWM views, and exact table-driven labels for more collection categories. Those broader changes are not included in this patch.


## Profile and API review additions

The profile selector has an **Activity** page with account age, recorded playtime in hours/days, average playtime per calendar day since account creation, and status at profile fetch. This uses existing profile fields and makes no additional API calls. Missing, future or non-finite timing values do not create invalid Discord timestamps or fabricated averages. It is a snapshot, not continuous tracking.

`get_player_info` now preserves the PID and host number established by the exact Redis result key. Previously a successful full-data fetch could discard the ID needed by Number ID resolution. The WWM cog delegates to the same utility resolver as ranking, validates successful responses and positive host numbers, and does not reinterpret a failed 10-digit Number ID as a nickname.

The existing utility also exposes sect election/history, homestead, combat-plan and fashion endpoints. The cog already displays many of these domains. Adding unobserved match history or per-match damage from aggregate combat counters would be unsupported, so no such data was invented. Further additions should use returned field evidence and the existing viewer permissions.


## Dungeon selection card correction (2026-10-08)

Ranking artwork now uses one original dungeon activity card, sourced from explicit game rank_id/rank_id_replace bindings. The previous boss introduction galleries are excluded. Current coverage: 62/98 boards, 18 distinct original cards; 36 unresolved boards remain text-only. Refresh WWM Ranking Assets.cmd refreshes game card bindings before importing. Old bundles are preserved. See DUNGEON ARTWORK RESEARCH.md for evidence and limits. The updated offline suite has 43 passing tests.


## Upcoming dungeon card follow-up (2026-10-08)

The deeper check found all six upcoming combined cards. Current coverage is 98/98 extracted boards, 24 distinct original images. Evidence distinguishes 62 activity bindings, 18 direct guild-card references and 18 hash-pinned visual reviews. The refresh command includes these resources; changed reviewed artwork requires a new review. See DUNGEON ARTWORK RESEARCH.md. Offline suite: 48 tests.


Readable artwork naming: active snapshot data/ranking_assets/ranking-artwork-2026-10-08-revision-02/; PNGs under artwork/hero-realm/ and artwork/sword-trial/. Historical snapshots are preserved; hashes remain in manifest.json.
