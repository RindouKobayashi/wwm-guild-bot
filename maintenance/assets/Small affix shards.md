# Small equipment-affix shards

The November 14, 2026 equipment check found two real game IDs missing from the
export: `100931001` and `100941001`. Both localize to **All Mystic Skill DMG
Boost**, use `{0:.1%}`, and carry the original ID reference `0.057`–`0.114`.
The reported values `0.098` and `0.112` display as **9.8%** and **11.2%**.

The old discovery filter required 50 IDs overlapping the primary table. The
original `index1001` shards containing these rows have 28 and 29 records, with
24 and 26 overlapping IDs. They could never pass that filter.

The Windows Toolkit repair is in `WWM-Toolkit/app/update_all_affixes.py`:
small shards may qualify with eight overlapping IDs and at least 50% overlap,
but also require ranged-affix structure: matching row ID, integer description
and attribute keys, finite ordered limits and value range, and valid types for
format, accuracy and quality fields. At least 90% of a shard must validate.
Unrelated control/NPC tables that reuse numeric IDs are rejected.

Supplemental shards add missing IDs without overwriting existing core rows.
Conflicting supplemental references fail rather than choosing an arbitrary
source. This recovered 54 original references, producing 5,344 affix mappings;
the 5,290 existing mappings are unchanged. The scoped item index was refreshed
too. Completely disjoint shards and unsupported schemas remain outside this
discovery method; the report does not claim complete game coverage or prove
runtime table selection from asset presence alone.

Source provenance for the reported IDs:

| Affix ID | Index frame | Original row |
|---|---|---|
| 100931001 | index1001:3711 | data1002, frame 326, byte offset 4600 |
| 100941001 | index1001:3698 | data1002, frame 326, byte offset 4768 |

Their names resolve from the installed `translate_words_map_en_diff`. No API
request, game-memory access or game modification was needed.

The Pi consumes the committed `assets/wwm/current` bundle and needs no Toolkit.
After pulling the committed update, validate the bundle and restart the bot and
Activity. Bot startup loads the published affixes into its local database;
copying a Windows database is unnecessary. The loose CSV/workbook fallbacks are
also refreshed. Previous releases and fallback files are preserved in local,
ignored backups.
