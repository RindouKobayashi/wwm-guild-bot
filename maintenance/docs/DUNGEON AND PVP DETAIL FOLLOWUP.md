# Dungeon and personal PvP detail follow-up

Date: 2026-10-09 (Asia/Singapore). Research only. No bot startup, Discord messages, RAM reads, game-directory edits, guessed endpoints or identifier enumeration.

## New work and limits

Focused original packaged Lua audit: 33 modules selected, 32 decoded, 54 relevant prototypes preserved. One module (dungeon_consts.lua) failed the current parser; do not claim complete coverage. Source paths were checked against the decoded chunk. Original constants/debug names establish field and call contracts, not reconstructed control flow or backend persistence.

Two bounded live probe runs made 14 read-only requests in total: four public boards and one existing research player in each run, plus two Sword Trial report tests in the second. All returned HTTP 200/code 0; success codes alone do not mean reports exist.

## HR/ST: personal records are separate from combat reports

Original local imp_dungeon.lua prototype at source line 1000 implements get_dungeon_rank_record_data by reading the logged-in avatar's dungeon.multi_guard_rank_team_data, indexed through rank_name_to_config.no. It constructs rank-list-shaped entries from stored score, ud, leader_id, hostnum and members. This is not an HTTP history fetch. The older recovery obscured the function body; the original prototype resolves that ambiguity.

Avatar DungeonRecordType has team_id, time_cost and ud; best_record and multi_guard_rank_team_data use owner-synchronized persistent properties. The public profile does return a dungeon group, but the tested player exposed only finished_xinshou, multi_guard_last_club_cnt and multi_guard_club_cnt. It did not expose best_record, multi_guard_rank_team_data or combat statistics. Thus requesting the dungeon group is not equivalent to retrieving the owner's run records.

Original server-proxy imp_dungeon.lua prototype at line 470 confirms settlement reads dungeon_combat_stat_detail, combat_stat and time_cost from synchronized space data. The existing active-session rpc_get_dungeon_statistic path remains distinct from historical HTTP retrieval.

Fresh first-three-row checks:

| Board | Advertised total | Members per sampled row | Space IDs per sampled row |
| --- | ---: | ---: | ---: |
| HR 22 | 500 | 10 | 2 |
| HR 3 | 300 | 10 | 0 |
| ST 11 | 500 | 5 | 2 |
| ST 1 | 500 | 4–5 | 0 |

Rows expose team members, leader, score, stored timestamp and per-member server/platform metadata. No sampled row contained damage, healing, skill breakdowns or boss split times. Current hydrated profiles must not be described as historical run gear.

Two exact public ST 11 IDs were submitted to known report readers. PvP reader returned result.result=[]; guild reader returned histories=null. This extends the old HR-only negative tests to an ST example. It does not prove no dungeon report service exists, and two IDs do not prove one per boss.

## Personal PvP: deeper report detail exists, public listing still missing

The original current history-window prototype (line 122) explicitly reads avatar mode battle_history, extracts battle_id values and passes space_ids to pvp_battle_get_history. The wrapper prototype (line 42) sends /flk/pvp_battle_history/get_history with space_ids. Owner property definitions add battle_history with OWN_CLIENT/PERSISTENT; the public profile probe again returned summaries but no history list. Supplying a Discord-bound player number does not grant that game-session owner property.

No private saved PvP IDs were transmitted during this follow-up. Ten previously approved API reports were analyzed offline:

- 20 participant records include damage, damage taken (absorb_dmg) and healing totals.
- Damage-category amounts (ORD_DMG, CRI_DMG, BASH_DMG and optional ABR_DMG) sum to reported damage for all 20 participants within the existing tolerance. Categories are a second grouping of the same damage: do not add them to skill totals or interpret amounts as hit counts/crit chance.
- Existing per-skill reconciliation remains 40/40 damage and damage-taken totals. Skill use counters are present, but are not verified hit counts; dividing damage by uses should not be marketed as damage per hit.
- Opponent-keyed combat_stat_detail allows damage dealt/taken by opponent. One detail record also contains healing detail; never promise that every report includes it.
- Participant combat_stat.time is absent in 17/20 records. Match time_cost is available; any damage/time_cost calculation should be called average damage per match second, not active-combat DPS.
- Match snapshots include old/new rating and grade, streaks, main/sub martial arts, portrait, level and school. These are run snapshots; unrelated current profile data is not.
- All ten extra_data fields are empty and all record_id values are empty. Neither reveals a timeline or replay in these samples.
- All ten reports have sid=1. This is not verification of 2v2/3v3 report coverage.

An external comparison does not resolve the listing gap: Ratz-GG's own Tools page describes its archived Match History as recorded Arena matches uploaded by its app, using its in-game reader, and showing last-synced data while that reader is off. It is not evidence of a public player-ID history API. Source checked: https://ratz-gg.net/tools .

## What is supportable in Discord

1. HR/ST ranked-run cards and a player membership index over cached boards: extracted dungeon art/name, rank, clear time, recorded timestamp and full team. Label coverage as ranked entries searched, not all dungeon attempts.
2. PvP report viewer for legitimately supplied actual battle IDs: outcomes/rating changes, participant snapshots, per-skill damage/taken, damage categories and available healing. The unresolved product dependency is how ordinary Discord users obtain IDs without a companion.
3. Guild battle history remains the verified RAM-free player-to-report route already implemented. It does not solve personal Arena history.

Next decisive evidence would be an actual client history-list read request taking pid/hostnum, an official match share/export containing battle IDs, or a dungeon report consumer that takes ranked spaceids. Guessing new endpoint names, probing IDs in bulk, or treating session RPCs as public HTTP will not establish those contracts.

## Reproducible evidence

- maintenance/research/audit_dungeon_pvp_contracts.py
- maintenance/research/probe_dungeon_pvp_followup.py (run from the bot directory; at most ten requests)
- output/dungeon-pvp-deep-contracts.json
- output/dungeon-pvp-followup-20261008T190950Z/summary.json and private-responses.json
- output/pvp-extra-detail-coverage.json
- Earlier approved reports: output/known-report-ids-20261008T054715Z

Generated evidence and raw responses remain in the existing ignored research output directory. No ranking or player commands were changed during this investigation.
