# PvP rank labels — display migration ON HOLD

**Current status:** keep the existing Silver I/II/III display. The extracted English strings say Skilled, but the owner reports Silver in the running client. Named-table verification confirms identity, not the final visible localization. The bundle now sets `display_policy: legacy_until_client_verified`; both Discord and Activity retain their existing title/suffix labels. The Mythic label change is also held. Future extraction retains this gate until the localization discrepancy is resolved and explicitly reviewed.

The findings below describe extracted file contents, not confirmed replacements for the running UI.

This step replaces only the rank text for API fields `lunjian` and `lunjian3v3_prop` in Discord player search and the Activity profile. It is not a general mapping for every PvP mode. It adds no icons, Discord uploads or live API requests. Avatars, equipment quality, rating calculations, matchmaking, rewards and notification rules are outside this step.

## Extracted differences — NOT enabled for display

| API grade / division | Old text | Extracted text |
|---|---|---|
| 3 / 1 | Silver I | Skilled I |
| 3 / 2 | Silver II | Skilled II |
| 3 / 3 | Silver III | Skilled III |
| 10 / 1 | Mythic I | Mythic |

All 28 shipped complete normal-arena labels are exported, rather than rebuilding names from a manually maintained title and Roman-numeral list. Discord and Activity share the same formatter and validated data. Numeric-string IDs work too. Unknown grade/division pairs remain explicit; no invented division names are supplied.

If a division is absent/zero, the formatter uses the game's consistent grade-only name where one exists. The current game has inconsistent grade-only labels for grade 8 (Hidden Edge versus Grandmaster); its complete row names are consistently Grandmaster I/II/III. The extractor records that discrepancy, omits the ambiguous grade-only alias, and reports a missing division rather than guessing. Legacy title/suffix dictionaries remain only for older checkouts without extracted PvP data.

## Source and update path

`lunjian_consts.ui_lunjian_get_grade_txt` reads `lunjian_grade_info[grade * 10 + small_grade].name`. The recovered 3v3 fight-info display uses the same table and combined-grade formula. These source references are recorded in the extraction. The verified normal-arena table currently appears at index2:1871; it includes grade/division/name and normal arena reward fields. Discovery uses its reviewed full field signature, not that fixed frame. Missing or ambiguous schemas, duplicate IDs, identity/padding errors, incomplete row coverage and unresolved English names stop the update.

The installed named MTable MANIFEST confirms these distinct bindings:

| Game table | Current schema | This change |
|---|---|---|
| `lunjian_grade_info` | index2:1871, 28 rows | Used only for API `lunjian` (1v1) and `lunjian3v3_prop` (3v3) rank text |
| `fight_shoulder_grade_info` | index1:15057, 28 rows | Excluded; the profile's existing `fight_shoulder` score stays a score |
| `huiwu_grade_info` | index2:621, 28 rows | Excluded |
| `lunjian_wuwo_grade` | index2:1934, 31 rows | Excluded; special-score/rank handling is not implemented here |
| `zzq_grade` | index1001:9543, 30 rows | Excluded; this is the table with additional Mythic divisions |

Correction to the initial notes: calling the index1001 table “Wuwo/event” was unsupported. The named manifest identifies it specifically as `zzq_grade`. It was never merged into the extracted labels. Matching names or matching division counts do not establish a mode binding.

The extractor now resolves `lunjian_grade_info` by name through MANIFEST before checking the reviewed schema and decoded row count. It also inspects the current installed Lua bytecode (without executing it) to verify the 1v1 and 3v3 references, recording bytecode hashes and prototype lines. An overseas manifest override or changed/missing references stops extraction for review. These checks strengthen the evidence; they do not claim that uninspected modes share this rank table.


The normal asset updater now includes a `pvp` section. Future focused updates use:

```powershell
& '.\Update WWM Assets.cmd' --only pvp --force
```

Review and apply the printed candidate on the main computer before committing, as with the other game data. The Pi needs only the committed bundle and Python code. No game installation, Toolkit, Node.js or new dependencies are required there.

## Commit/pull/start

This candidate is already applied and ready for review/commit. Staged assets were built from the previously staged table data; the working bundle separately preserves existing unstaged asset changes. Only the rank-formatting change is staged in the already-modified Activity profile module.

After reviewing and committing/pushing, use `git pull` and the normal launcher on the Pi. There is no additional Pi Apply step. Martial icon approval and uploads remain unchanged by this text-only update. Nothing has been committed, pushed or deployed during implementation.

## Verification

Nine focused tests cover exact labels for both modes, unknown and malformed IDs, absent divisions, legacy fallback precedence, conflicting grade-only labels, Discord/Activity formatting and rejection of divergent bundle copies. Existing asset, Abyss/offline and Activity checks were also run. Portable validation requires no third-party packages; no game login, bot session or Discord publication was used.

The mode re-check added named-manifest separation and explicit rejection of other mode names. No displayed rank label changed during that re-check, and no live player API requests or production deployment were performed.