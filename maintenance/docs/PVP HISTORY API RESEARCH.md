# PvP history access research

Updated 2026-10-08. Scope: Discord users supplying player identifiers, without RAM or a local game companion. No Discord runtime changes.

Later research supersedes the old-ID-only report result below: ten explicitly approved saved match IDs returned ten full PvP reports, and public current-guild history retrieval now works. See ACTIVITY API FINDINGS.md for the verified scope, statistics and remaining personal-arena listing gap.

## Conclusion

No verified end-to-end player-ID-to-PvP-history route has been found. A report lookup endpoint exists, but discovery of a player's report IDs is the unresolved dependency. This is a limit of the recovered client and tested public responses, not proof that no other service exists.

## Verified live requests

`maintenance/research/probe_pvp_history_api.py` resolves two existing repository player numbers and requests their PvP property groups. Both return HTTP 200/code 0 and public summaries, without battle_history. Explicit requests for battle_history and dotted mode-specific history fields return only the player id. Dotted field syntax itself is not established; absence from full mode groups is stronger evidence.

`/flk/pvp_battle_history/get_history` with the existing repository test space ID returns HTTP 200/code 0 and an empty result list. The ID's age and retention are unknown. This validates a responding endpoint, not successful report retrieval, report permissions, retention, or the full report schema. Private responses are in the ignored maintenance/research/output directory.

## Recovered Lua traces

All paths below are relative to the workspace's WWM_LUA directory. Decompiled code is incomplete and sometimes corrupt; expressions and missing method names cannot be treated as exact original code.

* `hexm/common/property_define/avatar/lunjian__record_1389955.recovered.lua`: battle_history is added with OWN_CLIENT | PERSISTENT. Equivalent owner flags exist for lunjian2v2, lunjian3v3 and fight_shoulder.
* `hexm/common/property_define/other_avatar/lunjian__record_1726524.recovered.lua`: other-player fields include ranks and streaks, without battle_history.
* `hexm/client/entities/local/player_avatar_members/imp_lunjian__record_271691.recovered.lua:153`: accessor reads self:get_server_entity().lunjian.battle_history. This is an entity property read, not a standalone HTTP lookup.
* `hexm/client/ui/windows/tianxia_baixiao/social_game/social_game_lunjian_history_window__record_821329.recovered.lua:310`: chooses the logged-in avatar's mode property; at 337 reads battle_history; at 342 calls pvp_battle_get_history(space_ids). The loop extracting IDs is not properly recovered.
* Same history window, lines 264–274: Share calls photo_take_photo_to_share. This specific sharing path is an image, not an exposed match-ID URL.
* `hexm/common/uwsgi_manager_members/imp_pvp_battle__record_1126185.recovered.lua`: get_history consumes space_ids. Other operations save/delete/like/remain-card are mutations and were not invoked. No player-history listing operation appears in this wrapper.
* `hexm/common/uwsgi_manager_members/imp_common_history__record_577555.recovered.lua`: /history_service/get_history_data accepts tag, pid, ts, sub_id, limit. Located client callers are private_service_employee_model and private_service_employer_model using PRIVATE_SERVE_HISTORY_TAG. No PvP tag or caller was found. Guessing a PvP tag is not a supported contract.
* `hexm/common/uwsgi_manager_members/imp_game_history__record_619905.recovered.lua`: list_game_history is used by homeland history/notifications (types 1/2). No PvP list caller was found.
* `hexm/client/ui/windows/competition/competition_history_window__record_1054318.recovered.lua`: competition history reads avatar.compe_lunjian.compe_record/stage_records. It is a separate mode and another avatar property, not an arbitrary-player PvP HTTP listing.
* Club battle windows use club.league_info or club.pk_match_info recent_space_idx, then /flk/club_combat/club_combat_read_history. This is separate guild combat and does not establish personal duel history access.
* `hexm/common/uwsgi_manager_members/imp_room__record_1760879.recovered.lua`: room lookups find rooms; player-meta-index requests are indexed by host/world level. Neither declares a PvP history list.

Searches covered battle_history references throughout the recovered tree, PvP/lunjian history variants, history services and call sites, player lookup services, competition history, sharing and guild history. The tree currently contains 40,868 files. Internet searches found no primary documentation establishing the missing personal PvP listing contract.

## Feasible product routes

1. Public PvP summary: usable now from player number -> internal pid + hostnum -> public mode groups. Present current rank, available counts and streaks; do not imply a match ledger or damage details.
2. Summary snapshots: periodically retain public summaries for users who link their character. Show observed changes and timestamps. Counter/rank changes cannot reliably identify individual matches, outcomes, opponents or damage, especially across resets or multiple matches.
3. User-submitted results: accept screenshots and explicitly mark extracted statistics as user-submitted. This works inside Discord without RAM, but only covers submitted matches and requires OCR/confirmation; it is not server-verified history.
4. Report import by actual space ID: experimental only until one fresh, legitimate match ID returns a real report. Verify participant IDs, timestamp, mode, schema and retention before shipping. A successful lookup still would not solve automatic discovery of a player's IDs.
5. Full automatic history: requires evidence of a public history-list endpoint or a supported account-linking/export service. Account authentication alone is not sufficient evidence; no suitable login/export integration has been established. Do not collect game passwords or treat the transport uid field as account authentication.

## Next validation gates

First validate one fresh existing match ID from a legitimate non-RAM source, if available (for example an actual game export or documented sharing URL). The inspected Share control does not supply one. Do not enumerate IDs.

Separately inspect newer client assets or an official companion/account-history feature if it exists; require a concrete read request mapping pid + hostnum to space_ids before implementing automatic Discord history. Repeating an empty lookup or inventing endpoint names does not resolve this dependency.

Once both list and report retrieval are established, the Discord implementation can normalize participants and mode, cache reports, map skill IDs through extracted skill tables, and expose a paginated match selector with damage/healing breakdowns. Affix tables are not a general substitute for skill-ID mapping.
