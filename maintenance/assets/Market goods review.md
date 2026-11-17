# Market goods and item images — 16 November 2026

## Selected behavior

- Prefer extracted game names; use the host's approved names only when the goods ID is absent from the extracted catalogue.
- Keep approved-name database rows intact. No approval history, watchlist, player data or prices are migrated or exported.
- Show compact original item thumbnails beside each dashboard heading, with rankings at full width below. Keep larger images in item-focused details. No application-emoji upload is required.
- Keep weekly goods selection, price calculations, ranking order, likes and refresh schedules unchanged.

## Findings and evidence

The named game MANIFEST identifies `hoard_profiteer_products` at current index1:16480 with 21 products. Its primary key is the **good_item_id**, such as 2122001. The `id` column is a different small row ID; it must not be used as `main_good`.

Current game Lua references link the API `main_good` to the product item number. The market bulletin uses `stuff_misc.get_item_name(good_no)` and obtains its image through `get_item_sys_d(good_no).icon_no`. The exporter verifies those references in installed bytecode without running it, and records source hashes.

Each product joins to the corresponding primary item key in the named `stuff_items` table. All 21 selected item rows resolve to names and original image resources. Optional `stuff_id` fields are checked when present; the index key remains the identity when the field is omitted. The exporter traverses only declared primary product shards, excluding secondary numeric indexes which would otherwise add two unrelated records. The broader item manifest contains additional definitions beyond its base index; the exporter requires complete coverage of the selected market items rather than claiming to export that entire table.

Icons follow each row's **icon_no**, not a guessed filename from the item ID. In current data Ramie (2122010) references icon 2122011, and Safflower (2122011) references icon 2122010. The regular original UI variants are exported; 4k variants are not mixed into the chosen images. Each image has a checksum in the public bundle. Missing or ambiguous resources, mismatched IDs, unresolved names or missing selected items stop extraction and retain the prior bundle.

These are installed asset definitions, not a prediction that all 21 goods are available this week. Live weekly goods and price histories remain API data. The main computer's local approved-name table was empty during review; production approved names were not inspected or changed.

## Where to see the images

- `/market goods`: names from the game and approved fallbacks are listed together. Pick an item to open a private name/ID/image detail card.
- **View Rankings** on the existing report: the selected good's detailed rankings show its original image. Switching goods replaces or removes the previous attachment; page navigation retains the same item image.
- Main reports, new-week reports and online/guild-filtered reports use matching compact item-image cards. Only the displayed goods are attached; the dashboard does not upload the entire 21-item catalogue.

Unknown IDs continue to display their approved fallback or a visible goods ID. Missing/unreadable artwork leaves a text-only detail card rather than blocking the view. Known extracted goods no longer need a name-suggestion button. The full icon/name sheet is in `assets/wwm/current/market/contact-sheet.html` after applying this ready-to-run bundle.

## Updating and deployment

The existing updater now supports `Update WWM Assets.cmd --only market --force`. It generates a review candidate from the main computer's installed game, including names, artwork, named table bindings and bytecode receipts. Review/apply that candidate on the main computer before committing future changes.

This change is already applied locally and prepared for staging as a coherent bundle. Staged table data and working-table data are updated separately, preserving unrelated unstaged work. Your deployment remains **review → commit/push → git pull → normal Pi launcher**. There is no separate Apply step on the Pi, no manual icon IDs and no new dependency or Node.js requirement there. Detail pictures are regular Discord message attachments when someone opens a detail view, so they require no startup emoji synchronization.

The PvP rank display hold remains unchanged: Silver labels are still preserved. No production bot restart, deployment, Discord upload or message publication was performed for this change.

## Verification

Fixture tests cover game-name precedence, unchanged approved-name rows, fallback names, unknown IDs, artwork checksums and path validation, missing/unreadable images, private detail responses, attachment removal on item switches, and keeping images out of the main report. The asset-update pipeline was also run against the installed game and resolved all 21 goods and original icons. Control-panel/action compatibility checks were run without real publications.

Verification results: 47 asset tests passed. The broader 103-test panel run passed its market/action checks but hit an existing terminal-session cleanup race (the watchdog removed an already-closed session before its test assertion); all 14 targeted device checks passed on rerun, with one platform-specific skip. No terminal implementation was changed. The applied market bundle also passed validation with Python site packages disabled. Discord rendering was checked through component/attachment fixtures, not a live test-server publication.

## Dashboard images — 17 November 2026

The dashboard now includes a thumbnail to the right of each good's name/ID heading, followed by full-width ranking text. New-week and filtered snapshots use the same cards. Larger detail images remain available. Missing artwork falls back to text. If an unusually large report approaches Discord's component limit, thumbnail cards collapse to text without dropping goods or rankings.

Image bytes are pinned in memory when building a view. Filenames contain the item ID and image checksum. Scheduled edits retain matching existing Discord attachments, upload only changed/missing pictures, and remove pictures for goods no longer displayed. The returned edited message is retained so later refreshes see the current attachment IDs. New message creation and edit-failure fallback get fresh in-memory file streams.

Control-panel previews include image filenames, sizes and checksums. They send nothing; execution still requires the existing confirmation. Target fingerprints now include attachment identity, so an image change after preview requires another preview. No sample dashboard was posted to Discord during implementation.

Dashboard follow-up verification: 52 asset/market checks and 27 API-tool/action checks passed. Tests cover the normal/new-week cards, both filters, unchanged attachment reuse, changed/removed goods, component-budget fallback, scheduled edits, preview metadata, and rejection when target attachments change after preview. These checks use fixtures and mocked Discord messages; production messages remain unchanged.
