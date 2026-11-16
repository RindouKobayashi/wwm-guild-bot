# Game exports and local runtime data

The four versioned exports in this folder contain public game data:

- `all_affix_id_english_names.csv`
- `All_Equipment_Item_Names.xlsx`
- `All_Item_Names.xlsx`
- `equipment_metadata.json`

They were refreshed from the November 14, 2026 extraction and checked against the
published asset bundle: 5,344 named affixes, 2,624 equipment names, 31,081 item IDs
and 2,634 equipment metadata records.

This includes 54 original ranged-affix references recovered from small shards;
the existing 5,290 core mappings were preserved. See the
[small-shard repair](../maintenance/assets/Small%20affix%20shards.md).
The broader audit also recovered 171 item names by correcting packed column
indices in the 273-column crafting table. See the
[coverage audit](../maintenance/assets/Asset%20coverage%20audit%202026-11-14.md).

The bot and Activity normally read the validated bundle in `assets/wwm/current`.
These files support legacy fallback and maintenance tools. The normal asset
updater publishes the bundle; it does not refresh these loose fallback exports.
When refreshing them separately, use the same extraction and verify agreement
with the published bundle before staging.

Databases, player JSON files, credentials, caches, guild settings and uploaded
images remain local and ignored. The affix database is populated from the
published bundle when the bot starts; do not transfer the Windows database to
the Pi.

See [the asset update guide](../maintenance/assets/Asset%20updates.md) for the
Windows review/apply and Pi pull/validate/restart steps.
