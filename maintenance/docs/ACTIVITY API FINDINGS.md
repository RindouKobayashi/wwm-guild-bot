# Activity API findings and Discord implementation path

Research completed 2026-10-08. This supersedes the earlier inconclusive report-only checks. No Discord bot was started, no messages were sent, and no live RAM was read. Game archives and the external catalog were read only. Runtime cogs and utility/wwm.py were not changed.

## Verified outcomes

| Capability | Live evidence | Remaining boundary |
| --- | --- | --- |
| Player number -> current guild -> guild battle IDs -> participant statistics | Four-step public API chain works. The sample guild exposed 39 unique battle IDs; all 39 returned reports. Requested player appears in 25. | Current guild's exposed records only; not personal arena history, every past guild, or every match ever played. |
| Known personal PvP match ID -> full report | Ten explicitly approved saved match IDs returned ten reports. | IDs came from a prior saved capture. Public player-ID-to-arena-match-ID discovery remains unverified. |
| PvP per-skill damage and damage-taken breakdown | 40 of 40 participant/metric sums agree with API totals within floating-point tolerance. | No fabricated labels: 91 of 104 distinct counter/damage IDs have extracted names; 13 remain numeric, including -1. |
| Personal PvP replay | API reports contain record_id. | All ten returned values are empty. No usable replay was obtained. |
| Player -> ranked dungeon entries | Two complete board lists returned 800 entries, representing 4,168 unique players. 1,650 appear in multiple entries across the sample. | These are current ranked entries, not a complete run log. Coverage is two boards only. |
| Dungeon space IDs -> extra combat report | All 500 dungeon-22 entries contain two IDs; all 300 dungeon-3 entries lack them. | Tested PvP/guild report lookups returned no dungeon reports. Treat IDs as run-associated metadata; purpose and per-boss meaning are unresolved. |

## Public guild history: a real feature we can build

The validated chain is:

1. `/flk/find_people/by_number_id`: resolve display number to internal pid and hostnum.
2. `/flk/redis_player/get_players_info`: request club for that exact pid/server pair.
3. `/flk/club_service/get_club_info`: use club_id and the guild's hostnum; decode the MessagePack response and nested binary fields. The tested response includes league_info and pk_match_info with recent_space_idx / season_recent_space_idx.
4. `/flk/club_combat/club_combat_read_history`: request returned battle IDs in batches of ten.
5. Parse each report's StatisticsJsonStr. Match the requested pid exactly against its participant keys. Expose only matches where that participant is present as that player's history; distinguish the rest as guild reports.

The tested 39 reports span 2026-04-05 through 2026-10-05 UTC. This is observed availability for this guild, not a guaranteed retention period.

Available report fields include TimeStamp, SpaceId, SeasonNo, CurRound, EnterMode, TopType, red/blue guild IDs and server numbers, old/new guild scores, win flags, StatisticsJsonStr and BattleInfoJsonStr.

Per-player fields observed include camp, hostnum, total_damage, damage_taken, healing_done, kills, deaths, assists, damage_wall, damage_goose, baiye_token, continue_kill_cnt and last_continue_kill_time. Field presence varies. The sample player's 25 records contain healing_done in all 25 and total_damage / damage_taken in 20. Do not silently replace absent metrics with zero.

BattleInfoJsonStr can contain non-UTF8 bytes inside JSON strings. UTF-8 surrogateescape allows offline structural inspection without destroying those bytes. Observed top-level fields include space_start_time, space_m_state and guild-keyed objective data. This can support start/end timing and objective state research; it is not a verified event timeline or full replay. Preserve raw response bytes when investigating it.

This enables a Discord guild-match selector, player participation history within the current guild's records, damage/healing/KDA tables, score changes and match comparisons. Camp labels/outcomes must use the game's camp constants rather than inferred list order. Metric definitions such as total_damage versus objective damage should be explained before calculating shares or DPS.

## Personal PvP: report retrieval is solved, discovery is not

`/flk/pvp_battle_history/get_history` accepts space_ids. Real returned reports include win/lose participants, rating before/after, grade, streaks, portrait ID, main/sub martial arts IDs, ts, time_cost, combat_stat, combat_stat_detail, own_skill_counter and record_id.

Integer-keyed skill mappings use the `__` list-of-pairs encoding. Read that structure rather than treating it as arbitrary JSON or attempting to map every number through an affix table. Damage class keys such as CRI_DMG, ORD_DMG and BASH_DMG are a separate classification. Skill uses and damage contribution can refer to different ID sets.

The offline decoder validated 20 participant records and 40 damage/damage-taken sums. All matched the corresponding totals with rel_tol=1e-7 / abs_tol=0.01. Named mappings come from the existing extracted live-labels skill table; unmapped IDs are retained.

The player's battle_history list is still owner-synchronized avatar data in the inspected code. Public profile requests did not expose it. The transport uid is not established as a player/account selector. Knowing a player number therefore does not currently yield their personal duel history through the verified public chain.

The saved-ID test required specific approval because those IDs originated in private capture data. That approval was granted; the requests succeeded. This proves the report lookup, not a RAM-free discovery mechanism for other Discord users. Default public lookup tools do not read those saved captures.

## Dungeon ranked runs: useful without a damage endpoint

The tested boards are rank_team10_dungeon_22 and rank_team10_dungeon_3. The API caps each response at 25 rows even when end=500. Paging by start/end retrieved their advertised totals of 500 and 300 respectively. Do not interpret the first response as the complete board.

All 800 entries' pid keys equal their member-ID set joined by underscores. They are team keys, not 16-character player IDs or match IDs. The API's my_data lookup with the tested team key remained empty / my_rank=-1; do not advertise direct team-key lookup as verified.

A membership index can join each character pid to returned board entries, rank, score, recorded timestamp, team roster and optional spaceids. Hydrating the first roster through member-specific hostnums returned all ten profiles. Those profiles are current data, not a snapshot of equipment or rank during the dungeon run.

All 1,000 space IDs from dungeon-22 entries decode to 12 bytes. Their first four big-endian bytes produce timestamps roughly 119–1,175 seconds before the entry's ts. This supports an ObjectId-like creation-time interpretation, not boss duration, match start/end, or completed activity semantics. The game's client IdManager uses a BSON ObjectId path, while the server uses a native generator. Standard ObjectId layout is documented at https://www.mongodb.com/docs/manual/reference/bson-types/#objectid. Exact server ID semantics remain inferred.

The known replay endpoint `/flk/recorder/fetch_record` expects unique_key=record_id. Substituting two dungeon space IDs failed; a directly inspected request returned HTTP 500. Personal PvP reports demonstrate that record_id is a separate report field, but the tested replay values are empty. Do not call dungeon IDs replay keys without a demonstrated mapping.

Dungeon statistics screens request rpc_get_dungeon_statistic on the active game connection and receive rpc_dungeon_show_statistic; settlement reads dungeon_combat_stat_detail from the synchronized space. No arbitrary historical dungeon HTTP report lookup was established.

## Stronger source audit

The old recovered Lua manifest's record numbers do not align with the current game archive index. The newer external asset catalog was opened read-only and used to locate original packaged chunks. Of 47,638 catalogued chunks, 47,224 decoded and matched their source paths; 414 failed decoding. Therefore the scan is broad but not complete.

Only engine/tools/watcher.lua contained the exact spaceids bytes; that engine-debug context is not a demonstrated dungeon-report consumer. The relevant client modules contained known report, replay, ranking and active-session statistic operations, but no extra dungeon-history HTTP route was found.

Current bytecode also exposed guild play-history wrapper endpoints missing from the older recovered tree. The single get_play_history_data request succeeded with empty data for the tested tag. A batch request returned HTTP 400, so its full contract is unverified. Neither is required for the working guild combat route above.

Prototype constants and debug parameters were decoded directly for selected current modules, avoiding invented control flow from broken decompilation. This recovers endpoint fields, not the unavailable backend implementation.

## Concrete Discord work order

1. Add a current-guild history command using the verified chain. Cache reports by space ID, cap report batches, offer match selection, and filter exact participant IDs. Show incomplete fields explicitly and separate participant statistics from guild totals.
2. Add a ranked-run lookup over cached public boards using exact roster membership. Display dungeon artwork from existing verified mappings, rank, result time/score, timestamp and roster. Preserve space IDs for future joins without presenting unverified statistics.
3. Add a report-import path for known PvP space IDs only after defining how Discord users legitimately obtain them. Reuse normalized skill/totals logic; do not claim automatic personal history.
4. Keep replay unavailable until a nonempty record_id returns a valid recording. Keep historical dungeon damage unavailable until a concrete successful lookup is discovered.

## Tools and evidence

* `maintenance/research/Lookup Public Guild Matches.cmd`: prompts for a player number; queries up to 40 public current-guild battle IDs and writes results. Does not start Discord or read RAM.
* `lookup_public_guild_matches.py PLAYER_NUMBER --limit 40`: equivalent research CLI.
* `probe_ranked_run_joins.py`: bounded two-board pagination, membership index and public guild joins; maximum 40 read-only requests.
* `probe_activity_details.py`: small endpoint/schema probe; maximum 16 requests.
* `probe_known_report_ids.py`: public guild IDs by default. Private saved PvP capture input is opt-in and must remain explicitly authorized.
* `decode_pvp_api_reports.py`: offline normalization, name mapping and total reconciliation.
* `decode_guild_combat_reports.py`: offline participant indexing and metric-presence summaries.
* `scan_activity_bytecode.py --all`: read-only packaged Lua constant audit. Requires the existing external asset catalog and original archive helper modules.

Generated/private evidence remains under ignored maintenance/research/output. Key runs: ranked-run-joins-20261008T054446Z (800 entries), known-report-ids-20261008T054715Z (ten PvP reports and preserved binary guild responses), public-guild-lookup-20261008T054948Z (39 reports, 25 with the sample player). Scripts were syntax-checked and run against actual responses; Discord runtime code was not changed.
