# Upcoming dungeon artwork: deeper check

The images were present. The previous 62/98 figure measured supported activity-card bindings, not asset existence. All 98 extracted leaderboard boards now have one original whole-dungeon card, using 24 distinct images. These counts do not establish release dates or whether every board is currently available in the API.

## Scope and results

The read-only scan decompressed all 29 installed Package/HD/oversea data archives: 77,020 readable Zstandard frames, 1,354,813,352 bytes. It searched for the six newer original card resources and their banner-name equivalents, then resolved index locators and decoded candidate table rows. This is a complete search of those readable data frames for these names, not a search of every binary/script/resource representation in the game.

Three new card names occur in the data tables: trial_displays:66 (Liminal Ascension, guild_image xj_25), trial_displays:78 (Horizon's Chart, guild_image xj_28) and trial_displays:81 (Gilded Wolf, guild_image xj_30). Their explicit jingsu_index / normal_dungeon_index links connect the normal and challenge variants. Names and IDs come from extracted game data.

The other three card textures are present in Resources.mpk assets despite having no literal reference in those data frames. Visual review matches their original boss artwork:

| Dungeon | Original combined card | Evidence |
| --- | --- | --- |
| Liminal Ascension | xj_25 | Direct guild_image in trial row 66; challenge 67 |
| Compass & Chime | xj_26 | Lodestar Swordmaster flame-armour pose matches original replay artwork; Bell Toller panel; instances 143/146 establish pair |
| Rouge & Rite | xj_27 | Rosy-Faced Dancer and The Mourner; latter pose, fur collar and staff match Yelu A'buli replay artwork; instances 164/165 |
| Horizon's Chart | xj_28 | Direct guild_image in trial row 78; challenge 80 |
| Dushan Encounters | xj_29 | Both panels match Inkpincer Scorpion and Cat Emperor original replay artwork; instances 174/173 and boss profile IDs 124/123 |
| Gilded Wolf | xj_30 | Direct guild_image in trial row 81; challenge 84 |

Dushan's Scorpion fight_entity_id is copied from Bell Toller. It is not used as proof. Display names also vary between instance/profile tables; no new player-facing aliases were invented.

## Evidence levels and update behavior

61 boards use explicit activity rank_id/rank_id_replace bindings; one uses an exact extracted activity name within its ranking namespace. 18 additional boards use direct dungeon guild-card references. 18 use reviewed original boss-art identities, explicitly marked reviewed_dungeon_card rather than verified_dungeon_card in the manifest.

The reviewed associations live in maintenance/assets/dungeon_card_reviews.json. They pin the original PNG SHA-256, trial name, fuben ID and normal/challenge row IDs. The importer checks those identities and reciprocal mode links. Changed artwork requires a new review. It does not guess by asset number or fall back to individual boss pictures. The Discord attachment is the original combined game card; comparison sheets are research presentation only.

Run Refresh WWM Ranking Assets.cmd after updating the toolkit's game-data export. It refreshes activity bindings and exports the reviewed candidate resources before rebuilding the portable bot bundle. Old bundles remain preserved. No game files are edited and the command does not start the bot.

Current audit: data/ranking_assets/fc102a3f8d6725e3eae4/artwork_audit.html.

Read-only research: maintenance/research/probe_new_dungeon_cards.py; machine report: maintenance/research/output/new_card_occurrences.json. Visual comparison: maintenance/research/output/new_card_boss_comparison.jpg. Six combined cards: maintenance/research/output/upcoming_dungeon_cards.jpg.

48 offline tests cover original attachments, all six upcoming families in both modes, changed-image rejection, trial identity drift, reciprocal mode links and cross-name rejection, plus prior WWM fixes. No bot launch, Discord connection or game API call was used.


Readable artwork naming: active snapshot data/ranking_assets/ranking-artwork-2026-10-08-revision-02/; PNGs under artwork/hero-realm/ and artwork/sword-trial/. Historical snapshots are preserved; hashes remain in manifest.json.
