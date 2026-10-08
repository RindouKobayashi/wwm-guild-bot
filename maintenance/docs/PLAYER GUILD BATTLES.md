# Player search guild battle history

Use `/player search <number or nickname>`, then select **Player Guild Battles**.
The requesting Discord user must have an account in `verified_members` with a
nonempty character UID or player PID. The searched player does not need a Discord
binding. Public profile search remains available under its existing rules.

Reports replace the original public profile message, visible to other readers.
Only the requester can change the selection or return to the profile. Binding
is checked before fetching, after fetching, and on every report interaction.
The view expires after five minutes. No RAM access or saved personal captures are used.
The battle view uses Discord components v2, matching the existing profile, and
restores profile attachments when returning to the overview.

The service fetches the searched player's current guild, collects its returned
battle IDs, and filters reports by that player's exact internal PID. It never
substitutes the requesting user's character or shows unrelated matches. Opponent
guild names use public API metadata; unavailable names retain the guild ID.
The searched player's performance remains the focus, with that character pinned
first in the roster and marked with a star. Selected matches retain both teams'
participant statistics. Red/blue summaries show score changes and sums of available
damage/healing fields, including the number of recorded values. No readings means
a dash, not a zero total. Unknown camps stay unassigned in the roster.

Rosters use six participants per page to stay within Discord text/component limits.
A participant selector shows damage, healing, K/D/A and objective/resource values.
Names and player numbers are fetched for the selected match only, in batches of
40 (up to 200 lookups), with a 20-second overall deadline. Lookup failures preserve
combat statistics and show internal IDs. Successful names are reused while browsing.

Limits: up to 100 newest IDs by ObjectId-style sorting hint, in batches of ten;
displayed dates always use the report timestamp. Two lookups can run concurrently.
Successful results are cached for three minutes, at most 128 player/server keys.
A lookup has a 90-second deadline. Partial responses and unavailable names degrade
without inventing values. No-guild, unavailable API, no matching reports, and
incomplete scans have distinct messages. Former guilds and lifetime history cannot
be discovered through this chain. Missing statistics display as a dash; zero is
preserved. Objective/resource units and skill breakdowns are not inferred.

The guild request must include `play`: current indexes and historical
`season_2_league_pk_info.*.spaceidx` live inside that field. Requesting only
standalone `league_info` / `pk_match_info` silently yields zero battle IDs.
A successful guild reply with no IDs is reported as “no battle IDs returned”,
not as evidence that the searched player never participated.

Read-only production pipeline check (does not start Discord):

```text
.venv\Scripts\python.exe -B maintenance/research/check_player_guild_pipeline.py <number-or-nickname>
```

Validation on 2026-10-08 for 月无双: 83 IDs, 83 retrieved reports, 79 containing
the searched player; no incomplete response. Public metadata and retention can
change, so those counts are a validation receipt, not a fixed expectation.

Results follow the recovered game record screen: EnterMode 3 uses team win flags;
other modes compare score gains. Missing/contradictory results stay unavailable.

Offline validation (no bot login):

```text
.venv\Scripts\python.exe -B -m unittest discover -s maintenance/checks/tests -p test_guild_battles.py
.venv\Scripts\python.exe -B -m unittest discover -s maintenance/checks/tests -p test_wwm_offline_improvements.py
```
