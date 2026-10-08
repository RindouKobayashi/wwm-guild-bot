# Abyss Trial rankings

Use `/ranking view dungeon:abyss:3` for Ye Wanshan, or choose rank_type **Abyss Trial** and pick an extracted boss name from autocomplete. Numeric IDs are trial IDs, not boss IDs.

`abyss_mode` selects Fastest clears (default), No-hit completion dates (UTC), only. Preset-specific boards and preset IDs are omitted from Discord. The normal optional player lookup jumps to that player's rank when returned.

The public message includes original game artwork, 20 ranked entries per page and a player selector for the historical run's Inner Ways, Mystic Skills, martial-art IDs. Only the requester can navigate; navigation edits the same message. Unknown IDs remain explicit. These records do not supply damage breakdowns, replay data or lifetime history.

Runtime files live entirely inside `data/abyss_assets` (54 trial definitions and original PNGs). This directory is covered by the project's existing data ignore rule: include it when deploying the bot. To refresh it after running the Toolkit Abyss extraction and loadout-name mapping, run `python maintenance/assets/import_abyss_assets.py` from the bot folder. No game-file edits are performed. Reload the ranking cog and use the bot's normal slash-command sync workflow to expose the new options.

Offline verification: six new Abyss checks plus all 48 existing WWM regression checks passed. API calls are mocked; no bot login or command sync was performed.
