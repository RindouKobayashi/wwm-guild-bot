# Asset coverage audit — November 14, 2026

The follow-up audit found and repaired another real omission: **171 item names**
were missing from the 273-column crafting table. Examples include Tier 110
Jadeware Weapon Chest, Tier 110 Starweave Weapon Chest and Custom Swap: Chest.

The row format packs the column number's upper two bits into the type byte:
`column = low_byte | ((packed_type & 3) << 8)`, with scalar/container type
`packed_type & 0xfc`. The old reader treated a column such as 266 as column 10
and mistook its packed type `0x05` for a floating-point value. The corrected
wide-table reader validates field count, distinct columns, schema bounds,
record reservation and zero padding. Nested Lua values retain their separate
encoding. Unsupported types fail explicitly.

All 171 recovered records have an explicit matching item ID, complete bounded
row parse and original English localization hash. The generated review adds
171 scoped item entries, with **zero changes or removals** to existing entries.
The bundle now contains **31,081 item IDs** and the earlier **5,344 affixes**.
The item workbook fallback is refreshed too. Eight parser regression tests,
including an original 96-byte record, and seven existing data-repair tests pass.

## Other checks and limits

| Area | Finding |
|---|---|
| Declared affix layouts | Both installed layouts checked. The alternate layout's 4,441 valid IDs were already covered; 571 overlapping records have conflicting values. Current primary mappings were preserved. |
| Schema-less affix shards | First/middle/last sampling covered 160,721 compact record frames outside the existing accepted shard list. Candidate frames received complete row checks; no additional affix ID was verified. Heterogeneous frames can evade sampling. |
| Equipment filters | 15,776 mixed-ID-band frames examined; 173,777 unknown in-band entries checked under supported equipment layouts. A separate structural pass sampled all 160,763 compact record frames without the ID-band filter. No additional equipment mapping was verified. |
| Weak equipment candidates | 742 layout-shaped observations covered 180 IDs. 736 observations have full foreign-schema context; six lie under a truncated schema and have no resolved English name. These are not published as equipment. A matching numeric layout alone is insufficient. |
| Item decode failures | The original 1,622 failed row reads included 1,451 complete scalar/list values in the referenced slots. Numeric payloads are not automatically item IDs or missing items. The remaining 171 were the genuine wide-table rows repaired above. |
| Remaining item exclusions | The new report retains 2,029 exclusions: 1,451 nonrow/unsupported-pointer read failures and blank-name/source-specific entries. Repeated title-source counts are not unique item counts. No placeholder names were invented. |
| Equipment English names | IDs 1170221–1170230 still lack names in the two installed English locale maps. Metadata remains available; labels are not invented. |
| Equipment null locators | 36 occupied keys have all-zero locators and no physical row. They do not establish deletions; prior decoded records are preserved. |
| Portrait deployment | The existing `data/avatars` directory is still a separate local image/upload store. No avatar files are tracked there or supplied by this portable bundle. A fresh Pi checkout does not acquire those local files. |
| Manual policy | Energy constants, PvP grade labels, quality labels, martial-role classification and UI slot labels remain explicit compatibility policy. |

The equipment and orphan passes are scoped structural audits, not proof of all
game content or runtime mount precedence. In particular, heterogeneous sampled
frames, unknown layouts, dynamically supplied data and unsupported animation or
shader behavior remain outside verified coverage. Original game files were
read-only; no game code, network/login/API operation or live-memory read was used.

The Windows decoder repair lives in `WWM-Toolkit/app/lib/item_codec.py` and its
item extractor. Pi runtime uses the generated bundle and needs no Toolkit.
Previous releases, workbooks and local audit receipts are preserved in ignored
Windows backup/build folders. No commit or push is performed by the repair.

[Machine-readable audit and report](audits/20261114/index.html)
