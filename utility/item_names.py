"""
Item Name Index (from data/All_Item_Names.xlsx)
================================================
Maps numeric ``item_id`` values to their human-readable English names.

Why this is NOT a plain ``id -> name`` dict
-------------------------------------------
The workbook covers ~37 categories (craft, affix, outfit, title, mount, ...)
and **1,986 item_ids appear in more than one category**. A flat lookup
therefore produces confidently-wrong names. Verified examples:

    money token "3001"  -> title "Rainbow Slayer"      (it is a currency)
    identity "1"        -> 9 different names collide
    ride.no2num "10002" -> resource / skill_card / title collide

So every ID maps to a *list* of candidates, and callers must state which
category they expect. ``get_item_name`` returns ``None`` rather than guessing
when a lookup is ambiguous and no category was supplied.

Usage
-----
    load_item_names()                              # once, at cog startup
    get_item_name(2901001)                         # only if unambiguous
    get_item_name(2901001, category="fashion_pose")
    get_item_name(3001, categories=["resource"])   # priority order
"""

import json
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence
import xml.etree.ElementTree as ET

from settings import BASE_DIR, logger
from utility.xlsx_reader import iter_xlsx_rows

ITEM_NAMES_XLSX_PATH = BASE_DIR / "data" / "All_Item_Names.xlsx"
ITEM_NAMES_CACHE_PATH = BASE_DIR / "data" / "item_names_cache.json"

# Expected column order of the "ID & Name" sheet.
COLUMN_ITEM_ID = 0
COLUMN_CATEGORY = 1
COLUMN_ENGLISH_NAME = 2
COLUMN_NAME_STATUS = 3

STATUS_OFFICIAL = "official_english_localization"
STATUS_INFERRED = "inferred_english_missing_from_installed_en_maps"

# item_id (str) -> list of (category, english_name, name_status)
ItemNameIndex = Dict[str, List[tuple]]

_ITEM_NAMES_INDEX: Optional[ItemNameIndex] = None


def _from_xlsx(path: Path) -> ItemNameIndex:
    """Parse the workbook into an index, preserving every category per ID."""
    index: ItemNameIndex = {}
    for row in iter_xlsx_rows(path):
        if len(row) <= COLUMN_ENGLISH_NAME:
            continue
        item_id = row[COLUMN_ITEM_ID].strip()
        name = row[COLUMN_ENGLISH_NAME].strip()
        if not item_id or not name:
            # 25 rows carry a blank english_name (see name_status) — skip them.
            continue
        category = row[COLUMN_CATEGORY].strip() if len(row) > COLUMN_CATEGORY else ""
        status = row[COLUMN_NAME_STATUS].strip() if len(row) > COLUMN_NAME_STATUS else ""
        index.setdefault(item_id, []).append((category, name, status))
    return index


def _write_cache(index: ItemNameIndex, path: Path) -> None:
    """Persist as JSON so restarts skip the ~0.6s XML parse."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False)
    except OSError as exc:
        logger.warning(f"Could not write item names cache: {exc}")


def _from_cache(path: Path, xlsx: Path) -> Optional[ItemNameIndex]:
    """Load the JSON sidecar, but only if it is newer than the workbook."""
    try:
        if not path.exists() or path.stat().st_mtime < xlsx.stat().st_mtime:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # tuples become lists after a JSON round-trip; restore them.
        return {k: [tuple(c) for c in v] for k, v in raw.items()}
    except (OSError, ValueError) as exc:
        logger.warning(f"Ignoring unreadable item names cache: {exc}")
        return None


def load_item_names(
    xlsx_path: Optional[str] = None,
    use_cache: bool = True,
    force: bool = False,
) -> ItemNameIndex:
    """
    Load (and memoise) the item-name index.

    Safe to call repeatedly; the parse happens once per process. Returns an
    empty dict on any failure so a missing file degrades to "no names"
    instead of breaking bot startup.
    """
    global _ITEM_NAMES_INDEX
    if _ITEM_NAMES_INDEX is not None and not force:
        return _ITEM_NAMES_INDEX

    path = Path(xlsx_path) if xlsx_path else ITEM_NAMES_XLSX_PATH
    if not path.exists():
        logger.warning(f"Item names xlsx not found at {path}")
        _ITEM_NAMES_INDEX = {}
        return _ITEM_NAMES_INDEX

    index: Optional[ItemNameIndex] = None
    if use_cache:
        index = _from_cache(ITEM_NAMES_CACHE_PATH, path)

    if index is None:
        try:
            index = _from_xlsx(path)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            logger.warning(f"Failed to parse item names xlsx: {exc}")
            _ITEM_NAMES_INDEX = {}
            return _ITEM_NAMES_INDEX
        if use_cache:
            _write_cache(index, ITEM_NAMES_CACHE_PATH)

    _ITEM_NAMES_INDEX = index
    logger.info(f"Loaded {len(index)} item names from {path.name}")
    return _ITEM_NAMES_INDEX


def get_item_candidates(item_id) -> List[tuple]:
    """All (category, english_name, name_status) entries for this ID."""
    if item_id is None:
        return []
    if _ITEM_NAMES_INDEX is None:
        load_item_names()
    return list((_ITEM_NAMES_INDEX or {}).get(str(item_id), []))


def get_item_name(
    item_id,
    category: Optional[str] = None,
    categories: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """
    Resolve an item_id to an English name, scoped by category.

    Resolution order:
      1. ``category``      -> exact match only
      2. ``categories``    -> first match in the given priority order
      3. neither           -> name only if the ID is UNAMBIGUOUS, else None

    Returns ``None`` whenever a name cannot be established confidently, so
    callers can fall back to displaying the raw ID. Never guesses.
    """
    candidates = get_item_candidates(item_id)
    if not candidates:
        return None

    if category:
        for cat, name, _status in candidates:
            if cat == category:
                return name
        return None

    if categories:
        for wanted in categories:
            for cat, name, _status in candidates:
                if cat == wanted:
                    return name
        return None

    # Unscoped: only safe when this ID means exactly one thing.
    if len(candidates) == 1:
        return candidates[0][1]
    return None


def get_item_name_safe(item_id, default: str = "") -> str:
    """Convenience wrapper that never returns None."""
    return get_item_name(item_id) or default


def is_ambiguous(item_id) -> bool:
    """True when this ID resolves to more than one distinct name."""
    names = {name for _cat, name, _status in get_item_candidates(item_id)}
    return len(names) > 1


def get_all_categories() -> List[str]:
    """Every distinct category present in the workbook, sorted."""
    if _ITEM_NAMES_INDEX is None:
        load_item_names()
    cats = set()
    for entries in (_ITEM_NAMES_INDEX or {}).values():
        cats.update(cat for cat, _name, _status in entries)
    return sorted(cats)


def get_name_status(item_id, name: Optional[str] = None) -> Optional[str]:
    """
    name_status for an ID (or for a specific name within that ID).

    ``official_english_localization``      -> ship/official text
    ``inferred_english_missing_from_...``  -> guessed, no EN localisation found
    """
    for cat, entry_name, status in get_item_candidates(item_id):
        if name is None or entry_name == name:
            return status
    return None


def clear_cache() -> None:
    """Drop the in-process index (used by tests / manual refresh)."""
    global _ITEM_NAMES_INDEX
    _ITEM_NAMES_INDEX = None

