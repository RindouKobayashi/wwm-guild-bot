# Updating WWM assets

Run `Update WWM Assets.cmd` on the Windows computer with the installed game and the sibling `WWM-Toolkit` checkout. Close the game updater first. The game itself does not need to run. No game files are edited, no game memory is read, and no API login or Discord connection is used.

The launcher extracts affixes, equipment names and metadata (sets, slots and relay limits), scoped item names, HR/ST dungeon definitions and verified dungeon cards, Abyss Trial definitions and original artwork, and verified API labels (sect names/statuses, election vote allowances, like-topic groups and guild boss names). Bot utilities and the Activity read the same validated release. See [API mapping audit](API%20mapping%20audit.md) for sources, callers and remaining limits.

## Normal update

1. Run `Update WWM Assets.cmd` after a game patch. It stages a candidate and opens the HTML review; **it does not change active assets**. Use `--game-root "C:\path\to\Where Winds Meet"` if the Toolkit configuration points elsewhere.
2. Read the printed `report.html`. It lists additions, changes, removals, unresolved artwork and remaining manual policies. Source hashes are checked before and after extraction. Unchanged sources skip extraction unless `--force` is passed.

   The review includes a section summary, search by ID/name/field, status and section filters, before/after records, and field-level differences. It covers all mapping dictionaries, scoped item names, equipment sets/relay limits, ranking boards and original artwork content hashes. Results load 100 at a time. “Removed” means absent from the new export; it does not establish that the game deleted the content. The JSON report contains the same detailed review for other tools. An initial bootstrap is explicitly identified as having no comparison baseline.
3. When satisfied, stop the bot/Activity and run `Apply WWM Asset Update.cmd <run-id>` using the exact run ID printed by the update command. Double-clicking the Apply CMD selects the latest successfully staged review. If the report contains removals, explicitly add `--allow-removals` **to the apply command**, after confirming they are intended. Applying does not repeat extraction. It validates the exact staged checksums and refuses a candidate if the active baseline changed since the review. Regenerate the review if either the candidate or baseline changed. Extraction/validation failures leave the active pointer untouched and retain logs in ignored `.asset-builds/<run>/`.
4. After applying, review and commit the code changes, `assets/wwm/current.json`, and the entire `assets/wwm/current/` folder. This fixed folder contains only the latest applied bundle. Replaced copies go to ignored `.asset-backups/wwm/` on this computer; Git preserves committed history. Do not commit `.asset-builds`, `.asset-backups`, credentials, `.env`, databases, or research captures. Restart the local bot/Activity after applying.
5. Pull on the Pi, validate, then restart using your usual launcher:

```sh
cd ~/Desktop/wwm-guild-bot
git pull
venv/bin/python maintenance/assets/update_game_assets.py --check
bash run.sh
```

Stop the existing bot before starting it again. Use `.venv/bin/python` instead if that is your Pi environment name. The Pi needs neither the game nor Toolkit. Test and production both consume the committed bundle; their `.env` and authentication remain device-specific. Runtime assets are pinned until restart.

The shared data folder is named `current`, not `production`, because both environments consume the same game exports. The small `current.json` pointer and manifest hash are still committed for integrity validation. During the one-time migration, the previously applied bundle was preserved byte-for-byte and older release directories were moved into local backups. No extraction was repeated. Pending reviews made before this layout migration must be generated again because their baseline pointer changed.

## Other options

- `--check`: validate the published bundle offline; works on Windows and Linux.
- `--only affixes,equipment`: update selected sections after a complete bundle exists. Unselected sections retain their previous data and the report identifies this as a mixed-source update.
- `--from-existing`: bootstrap reviewed local exports without scanning the installed game. This is explicitly marked as older exports, not a fresh extraction.
- `--include-avatars`: additionally refresh Toolkit avatar exports. It does **not** publish bot avatar files or establish complete animation coverage. Original-engine effects remain a separate rendering task.

## What is still manual

Energy rules, PvP grade labels, quality labels, martial role classifications and UI slot labels remain compatibility policy until their game-table sources and joins are verified. Market/Kitty/Pet like labels remain explicitly attributed fallbacks because the extracted profile UI table does not include their server topics. Abyss loadout names currently reuse the existing reviewed `loadout_name_tables.json`; they are not freshly extracted by this pipeline. These limitations appear in the report. No new name is invented to fill missing data.

Legacy `data/` exports remain as fallback when no bundle exists; a corrupt published bundle raises an error instead of silently loading old data. Explicit file paths used by maintenance tools and tests still work. Existing avatar files are not deleted or replaced.

## Files to review for this change

- New: `Update WWM Assets.cmd`, `Apply WWM Asset Update.cmd`, `utility/game_assets.py`, `utility/api_mapping_data.py`, `maintenance/assets/build_game_bundle.py`, `maintenance/assets/asset_review.py`, `maintenance/assets/update_game_assets.py`, `maintenance/assets/extract_api_constants.py`, `maintenance/assets/api_constant_references.json`, both guides, `maintenance/checks/tests/test_game_asset_pipeline.py`, and `assets/wwm/`.
- Updated runtime consumers: `utility/item_names.py`, `utility/affix_mapper.py`, `utility/api_constants.py`, `utility/dungeon_assets.py`, `utility/abyss_rankings.py`, `activity/server.py`, `activity/runtime_version.py`.
- Likes/equipment follow-up: also include `cogs/wwm_cog.py`, new `utility/component_pages.py`, `maintenance/assets/extract_like_topics.py`, `maintenance/assets/lua_symbols.py`, and the updated API mapping extractor/validator and offline tests. The topic enum is extracted offline and shipped in the bundle; the Pi does not parse game Lua. Equipment pages split by display-text budget instead of assuming each slot group fits one Discord message.
- Updated maintenance: the equipment metadata extractor, player mapping builder, dungeon refresh/import, Abyss importer, offline check runner, and `.gitignore`.
- Separate Toolkit fixes: `app/table_references.json`, `app/table_discovery.py`, `app/update_trial_dungeons.py`. These support the current moved schemas and split HR/ST tables; review them in that project separately. They are needed for future Windows extraction, not Pi runtime.

There may be unrelated work in Git status. This list is specific to the asset pipeline; no commit or staging is performed automatically.
