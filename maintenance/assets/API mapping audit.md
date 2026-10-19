# API constants and game-data sources

Audited against the installed October 19, 2026 assets, existing bot callers and current extracted Lua. No game process, API requests or Discord login were used.

## What each mapping means

| Mapping | Actual meaning and use | Update source |
| --- | --- | --- |
| `SCHOOL_NAMES` | Numeric `base.school` sect ID. Used by player search, Activity, ranking options and sect commands. A definition in the client is not proof that it is currently open. | `school_select_config` / `sect_name`, keyed by `no`. Current location `index4:8922`; 11 sects. Sectless comes from its own `100_1` status row: 12 names total. |
| `SCHOOL_RANKING` | Sect status title, **not** leaderboard position. API `school.status` is a string such as `1_3`; its parts equal the row's `school` and `identify`. Player search and Activity display it; sect vote monitoring uses it to describe members. | `school_status`, current `index4:8931`; 55 string-keyed rows. Use `name_school_identity`, matching current client promotion/share code. Preserve `name_school_identity_show` separately because some labels differ. |
| `VOTE_COUNTS` | Configured sect-chief election vote allowance for that status. It is not the number of votes received and not a live remaining-vote counter. Used by player search and sect monitoring against `chief_campaign.vote_num`. | The same status row's `vote_time`; 54 explicit allowances. Zero stays zero. Sectless has no allowance; no zero is invented for that missing field. |
| `LIKES` | Labels for server like/unlike topic IDs returned by `get_topics_likes`. Player search displays these categories. Topic IDs differ from the UI group's primary key. | Profile upvote table, current `index3:726`: 13 groups reference 16 server topics through `server_id`. Use `upvote_text`. Market `129`, Kitty `151` and Pet `172` are absent from this UI table; preserve and explicitly attribute their existing compatibility labels. |
| `BOSS_NAMES` | Guild boss-fight schedule IDs used by event logs and guild leaderboard sessions. These are a separate namespace from HR/ST dungeon IDs and Abyss recall IDs. | Guild boss-fight definition table, current `index1:9392`; 30 named rows. Existing non-placeholder IDs agree except updated names noted below. |
| `KONGFU_WEAPON_MAP` | Martial-art name, path icon, Discord emoji and app-assigned combat role. Used by guild chat, player search and Abyss loadouts. | Existing verified `martial_names` override is retained. A complete current martial-name join is still unresolved; do not use appearance-item `kongfu_id` tables as though they were combat IDs. |
| `SCHOOL_EMOTES`, `SCHOOL_ROLES` | Discord server emoji and role IDs, with test/production differences. | Discord configuration. These cannot come from game assets. New game sects do not automatically create Discord roles or emojis. |
| `HEALER_WEAPONS`, `TANK_WEAPONS`, `classify_kongfu_role` | Bot classification policy, including hybrid roles. | Keep explicit app policy; a role heuristic is not a game display-name mapping. |
| `BULK_FIELDS`, `BULK_PRESETS`, `GUILD_FIELDS`, `CLUB_CHAT_*`, `API_ENDPOINTS` | Request contracts and which fields the bot chooses to request. | Maintain against API/Lua contracts, not a localized MTable name list. They do not need automatic replacement merely because they are literals. |

## Verified corrections and missing entries

- Sect names add Lone Cloud `5`, Inkbound Order `8`, Hollow Vale `10`. Existing names remain unchanged.
- Sect statuses add 17 keys, correct four titles and remove the unsupported old `6_3` mapping. In current data `6_4` is Sufferer and `6_5` is Chief Elder. `2_2` is Supporting Artist; `11_2` is Inner Prentice; `3_5` uses Marshal in `name_school_identity` (its alternative display field says Division General).
- Vote extraction agrees with all 30 existing allowances and adds 24 explicit entries, including zero-vote statuses. Velvet Shade's top status is 4. The old votes were incomplete, rather than generally numerically wrong.
- Likes gain nine server-topic mappings: `105`, `114`, `132`, `139`, `142`, `152`, `174`, `181`, `183`. For example group `9` covers topics `103` **and** `114`, while group `1` covers `101` **and** `142`. Group IDs themselves must not be used as API topic IDs.
- Guild bosses add Bell Toller `32`, Halcyon `33`, Inkpincer Scorpion `36`, Cloud-Seizing Serpent `37`. `5` corrects “Snaker Doctor” to Snake Doctor; `29` currently localizes to Lunar Eidolon. Remove five `*BLANK*` placeholders so unknown IDs use the caller's normal unknown-ID fallback.

## Evidence and decoding checks

`api_constant_references.json` records the complete reviewed field signatures. The extractor relocates these schemas; it does not trust the frame numbers above after updates. It refuses missing/ambiguous schemas, duplicate IDs, unresolved English names, incomplete row coverage, incorrect keys, malformed records and duplicate server-topic associations.

The status table uses a **string-key index**, unlike ordinary numeric item tables. Its 55 keys are decoded from the index and independently checked against `school` + `_` + `identify`. Treating its packed string descriptors as numeric row IDs incorrectly drops every row.

Current Lua evidence:

- `hexm/common/consts/school_consts.lua`, lines 121–143: reads `datam.school_status[school.status].vote_time`; remaining-vote logic also checks `chief_campaign.vote_ts` and `is_same_week`.
- `hexm/client/entities/local/player_avatar_members/school/imp_school_status.lua`, lines 343–471 and 885–900: uses `name_school_identity` and `school_select_config.sect_name`.
- `hexm/client/ui/windows/school/school_status/status_vote_rank_window.lua`, lines 286–298 and 381–430: reads status `vote_time` and campaign `vote_num`, and resolves chief titles from the same status table.

These are offline bytecode prototype/source-line references, not a claim of live server behavior. Local extraction receipts and decoded evidence are in ignored `.asset-builds/constants-*` and audit JSON files.

## Remaining limits and follow-up

The bot's remaining-vote calculations subtract `vote_num` directly; the game's shared helper also checks the vote timestamp's week. Updating labels/allowances alone does not validate that weekly reset logic. The API snapshot's timestamp and the game's reset-time semantics need a separate focused check before changing that behavior.

Market code deliberately requests topic `129`; do not substitute the UI group ID or sum unrelated topics. Its label remains explicitly a compatibility fallback. Energy rules, PvP grades, quality labels and martial role policy remain outside this verified extraction.

Follow-up: the profile UI table is not complete API-topic coverage. The updater now also extracts 80 direct `TOPIC_*` assignments from the original `hexm/common/consts/likeunlike_consts.lua` bytecode without executing it. All 23 topics currently requested by `get_topics_likes` are covered by either the UI labels/explicit compatibility labels or these enum names. Nine requested IDs require symbolic labels: 102, 104, 113, 10008, 120, 121, 124, 133, 140. `get_like_topic_name` prefers the normal label and otherwise renders the enum identifier with “(game topic)”. These fallbacks identify the category but are not claimed to be official localized display names. Truly unknown IDs remain visibly unmapped. A restarted process and the applied updated bundle are required.

## Update and deployment

The normal `Update WWM Assets.cmd` now includes this extraction and only stages a review. For only these mappings use `Update WWM Assets.cmd --only constants`. After reviewing, use `Apply WWM Asset Update.cmd <run-id>`; add `--allow-removals` to that apply command only if you accept the reported removals. The first migration compares against the actual old public literal dictionaries, so its before/after report includes corrections rather than calling every mapping new.

The published `tables.json` contains names, allowances, source schemas, status metadata and like-topic associations. Runtime validators verify their joins. Bot and Activity use the same bundle; the Pi needs only the committed files and a restart, with no Toolkit or game installation. Old literals remain fallback for checkouts without the new bundle. No Discord roles/emojis are created or modified.
