# Dungeon ranking space ID investigation

2026-10-08. Read-only requests; no RAM reads, bot startup or Discord messages.

Follow-up: ACTIVITY API FINDINGS.md records the larger 800-entry audit, original-bytecode scan and successful public guild-report chain. Dungeon-specific damage retrieval remains unverified; personal PvP report retrieval now succeeds with approved saved IDs.

## Observed ranking data

A live request for rank_team10_dungeon_22 returned code 0 and 20 entries. The first entry contained ud.spaceids (two strings), ten members, per-member hostnum/device metadata, leader_id, hostnum, ts, and score. These fields are sufficient for a ranked-run team view, but do not contain damage, healing, or per-boss timings. The exact meaning of each space ID and timestamp has not been established. Two IDs do not establish one ID per boss.

Private response: maintenance/research/output/ranking-space-investigation.json.

## Report and replay tests

The two exact ranking IDs were submitted to the known read-only /flk/pvp_battle_history/get_history endpoint: code 0, empty result list. This establishes no dungeon report retrieval through that request; it does not establish that no dungeon report service exists.

Found another concrete endpoint in hexm/client/entities/server/player_avatar_members/imp_watch__record_1952132.recovered.lua (under workspace WWM_LUA): /flk/recorder/fetch_record. Its request uses unique_key=record_id. A successful response is expected to contain compressed info, optional next_key, optional md5, spaceno and spaceid. The client decompresses and joins chunks, checks the digest, then plays a spectator recording. This is replay data, not necessarily a structured damage report.

Each ranking ID was tried as unique_key, without success (the existing helper returned None). A direct HTTP inspection of the first request returned HTTP 500, text/plain, 50 bytes. This is an inconclusive failure: record IDs may differ from space IDs, records may be unavailable, or the deployed service/request context may differ. Do not interpret it as a definitive permission denial or absence of all records. No pagination or replay data was downloaded because no initial response succeeded.

The located caller of fetch_record_data_and_play is the player lunjian module, passing a record_id. No dungeon-ranking caller linking ud.spaceids to recorder unique_key was found. Exact string spaceids has no match in the recovered Lua tree, despite its presence in the live response. It may be server-created metadata, unused client-side data, or omitted by incomplete recovery.

Private test summaries: maintenance/research/output/ranking-space-replay-response.json and ranking-space-replay-http.json.

## Actual dungeon statistics path

hexm/client/ui/windows/fuben/fuben_guide_window__record_2374239.recovered.lua:38 requests G.net:call_server("rpc_get_dungeon_statistic", dungeon_consts.STAT_DETAIL). The live HUD uses STAT_TICK; settlement uses STAT_JIESUAN. These are active game-session calls, not evidenced standalone HTTP requests accepting arbitrary historical space IDs.

hexm/client/entities/server/player_avatar_members/imp_dungeon__record_1674286.recovered.lua:407 receives rpc_dungeon_show_statistic(data), caches it, and dispatches UI events. Settlement also reads server_space:get_space_data("dungeon_combat_stat_detail") and time_cost. The server directory here contains client-side server-entity proxies/stubs; it does not supply the backend implementation or prove persistent storage.

Rank controllers read personal dungeon rank records and request ranking/player information. The inspected code does not expose a historical damage-report lookup using spaceids. /rank_sync_service/get_rank_by_key returns a ranking position, not a combat report. Debug battle replay code reads local LocalData files; it is not evidence of public dungeon replay storage.

## Interpretation and next evidence needed

Space IDs can be useful provenance or internal correlation keys even without a public retrieval service. A backend can retain them for validation, duplicate detection or internal investigation; those are possibilities, not confirmed uses. Returning a key does not guarantee it is a public report handle.

To advance beyond this result, locate a concrete client consumer of dungeon space IDs, an official exported/shared dungeon report, or an authenticated game request that explicitly retrieves a past run. If a future asset recovery restores missing rank/dungeon methods, repeat the consumer search. For replay, establish a real record_id-to-spaceid relationship before treating ranking spaceids as record keys.

Until then, the Discord bot can show team, rank, score and recorded timestamp from leaderboard data. It must not label current player equipment as equipment used in that run, invent per-boss statistics, or promise archived damage reports.

All Lua references are best-effort decompilations; missing expressions and methods limit negative conclusions.
