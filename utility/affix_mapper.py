"""
Universal Affix Mapping System
================================
Provides a database-backed mapping of numeric affix IDs to human-readable names.
Includes a universal mapper that can annotate any JSON data structure with
human-readable affix names, context-aware so "1000" in one place isn't
mistaken for "1000" in another.

Database: SQLite stored at BASE_DIR/data/affix_mappings.db
"""

import aiosqlite
import csv
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET
from settings import BASE_DIR, logger

DB_PATH = BASE_DIR / "data" / "affix_mappings.db"
AFFIX_CSV_PATH = BASE_DIR / "data" / "all_affix_id_english_names.csv"
EQUIPMENT_NAMES_XLSX_PATH = BASE_DIR / "data" / "All_Equipment_Item_Names.xlsx"

# Chinese unit characters found in value_show_format → English equivalents
# e.g. "{0:.1f}米/秒" becomes "{0:.1f}m/s", "{0:.2f}秒" becomes "{0:.2f}s"
CHINESE_UNIT_TRANSLATIONS = {
    "米": "m",      # meter
    "秒": "s",      # second
    "点": "pt",     # point
}

def _sanitize_format_string(fmt: str) -> str:
    """Replace Chinese unit characters in a format string with English equivalents."""
    if not fmt:
        return fmt
    result = fmt
    for chinese_char, english_char in CHINESE_UNIT_TRANSLATIONS.items():
        result = result.replace(chinese_char, english_char)
    return result


# ---------------------------------------------------------------------------
# Equipment name lookup (from All_Equipment_Item_Names.xlsx)
# ---------------------------------------------------------------------------
_EQUIPMENT_NAME_CACHE: Dict[str, str] = {}


def load_equipment_names(xlsx_path: Optional[str] = None) -> Dict[str, str]:
    """Load equipment item names from the xlsx file.

    Reads the ``item_id`` / ``english_name`` columns from sheet1 and returns
    a dict mapping item_id (as str) -> english_name.

    Uses only stdlib (zipfile + xml.etree) — no openpyxl/pandas needed.
    """
    global _EQUIPMENT_NAME_CACHE
    path = Path(xlsx_path) if xlsx_path else EQUIPMENT_NAMES_XLSX_PATH
    if _EQUIPMENT_NAME_CACHE:
        return _EQUIPMENT_NAME_CACHE

    if not path.exists():
        logger.warning(f"Equipment names xlsx not found at {path}")
        return {}

    # Namespace used by the xlsx XML
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    try:
        with zipfile.ZipFile(str(path)) as z:
            # The real data lives in xl/worksheets/sheet1.xml
            tree = ET.parse(z.open("xl/worksheets/sheet1.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as e:
        logger.warning(f"Failed to parse equipment names xlsx: {e}")
        return {}

    root = tree.getroot()
    rows = root.findall(".//x:row", ns)
    for row in rows:
        cells = row.findall("x:c", ns)
        if len(cells) < 2:
            continue
        v0 = cells[0].find("x:v", ns)
        v1 = cells[1].find("x:v", ns)
        if v0 is None or v1 is None or not v0.text or not v1.text:
            continue
        item_id = v0.text.strip()
        name = v1.text.strip()
        if item_id and name:
            _EQUIPMENT_NAME_CACHE[item_id] = name

    logger.debug(f"Loaded {len(_EQUIPMENT_NAME_CACHE)} equipment names from {path.name}")
    return _EQUIPMENT_NAME_CACHE


def get_equipment_name(item_no) -> Optional[str]:
    """Look up an equipment name by its item No (int or str)."""
    if not _EQUIPMENT_NAME_CACHE:
        load_equipment_names()
    if item_no is None:
        return None
    return _EQUIPMENT_NAME_CACHE.get(str(item_no))

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS affix_mappings (
    affix_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    value_format TEXT NOT NULL DEFAULT '',
    minimum REAL,
    maximum REAL,
    name_min REAL,
    name_max REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ---------------------------------------------------------------------------
# Known affix key patterns – these define WHERE in a JSON tree affix IDs live.
# The mapper uses these to decide which values to look up.
# ---------------------------------------------------------------------------

# Keys whose *value* is a single affix ID (e.g. "tone_determin": 270701)
SINGLE_AFFIX_KEYS: Set[str] = {
    "tone_determin",
    "retone_raw_affix_no",
}

# Keys whose *value* is a 2-element list [affix_id, value]
# (e.g. "another_determin": [270805, 1])
PAIR_AFFIX_KEYS: Set[str] = {
    "another_determin",
}

# Keys whose *value* is a list of [affix_id, value] pairs
# (e.g. "base_affixes": [[9213005, 35.8], [9293002, 40.4]])
PAIR_LIST_AFFIX_KEYS: Set[str] = {
    "base_affixes",
}

# Keys whose *value* is a dict where the *keys* are affix IDs
# (e.g. "det_history": {"1": {"270702": 7}})
# The inner dict's keys are affix IDs
DICT_KEY_AFFIX_KEYS: Set[str] = {
    "det_history",
}

# Keys whose *value* is a dict where the *values* are lists of affix IDs
# (e.g. "retone_affix_history": {"2": [9293019]})
DICT_VALUE_LIST_AFFIX_KEYS: Set[str] = {
    "retone_affix_history",
}

# All known affix key patterns combined for easy checking
ALL_AFFIX_PATTERNS: Set[str] = (
    SINGLE_AFFIX_KEYS
    | PAIR_AFFIX_KEYS
    | PAIR_LIST_AFFIX_KEYS
    | DICT_KEY_AFFIX_KEYS
    | DICT_VALUE_LIST_AFFIX_KEYS
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create the database and table if they don't exist."""
    os.makedirs(DB_PATH.parent, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()
    logger.debug(f"Affix mapper DB ready at {DB_PATH}")


async def load_affix_csv(csv_path: Optional[str] = None) -> int:
    """Load affix mappings from the CSV file into the database.

    Reads ``data/all_affix_id_english_names.csv`` (affix_id, english_name,
    value_show_format, minimum, maximum, ...) and upserts each row into the
    affix_mappings table so ``map_data()`` can annotate affix IDs with
    human-readable names plus display metadata (format string, min/max).

    Returns the number of rows loaded (0 if the CSV is missing).
    """
    path = Path(csv_path) if csv_path else AFFIX_CSV_PATH
    if not path.exists():
        logger.warning(f"Affix CSV not found at {path}")
        return 0

    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                affix_id = int(row["affix_id"])
            except (ValueError, TypeError, KeyError):
                continue
            name = row.get("english_name", "").strip()
            if not name:
                continue
            value_format = _sanitize_format_string(
                row.get("value_show_format", "").strip()
            )
            min_raw = row.get("minimum", "")
            max_raw = row.get("maximum", "")
            try:
                minimum = float(min_raw) if min_raw not in (None, "") else None
            except (ValueError, TypeError):
                minimum = None
            try:
                maximum = float(max_raw) if max_raw not in (None, "") else None
            except (ValueError, TypeError):
                maximum = None
            rows.append((affix_id, name, value_format, minimum, maximum))

    if not rows:
        logger.warning(f"No valid affix rows found in {path.name}")
        return 0

    # Compute name-level min/max: for each unique english_name, the lowest
    # minimum and the highest maximum across all affix IDs with that name.
    name_min_map: Dict[str, float] = {}
    name_max_map: Dict[str, float] = {}
    for _, name, _, minimum, maximum in rows:
        if minimum is not None:
            if name not in name_min_map or minimum < name_min_map[name]:
                name_min_map[name] = minimum
        if maximum is not None:
            if name not in name_max_map or maximum > name_max_map[name]:
                name_max_map[name] = maximum

    # Build final rows with name_min / name_max
    final_rows = []
    for affix_id, name, value_format, minimum, maximum in rows:
        final_rows.append((
            affix_id,
            name,
            value_format,
            minimum,
            maximum,
            name_min_map.get(name),
            name_max_map.get(name),
        ))

    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.executemany(
            """INSERT OR REPLACE INTO affix_mappings
               (affix_id, name, category, description, value_format, minimum, maximum, name_min, name_max)
               VALUES (?, ?, '', '', ?, ?, ?, ?, ?)""",
            final_rows,
        )
        await db.commit()

    logger.info(f"Loaded {len(final_rows)} affix mappings from {path.name}")
    return len(final_rows)


async def add_mapping(
    affix_id: int,
    name: str,
    category: str = "",
    description: str = "",
) -> bool:
    """Add a new affix mapping. Returns True if inserted, False if already exists."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        try:
            await db.execute(
                """INSERT INTO affix_mappings (affix_id, name, category, description)
                   VALUES (?, ?, ?, ?)""",
                (affix_id, name, category, description),
            )
            await db.commit()
            logger.debug(f"Added affix mapping: {affix_id} -> {name}")
            return True
        except aiosqlite.IntegrityError:
            logger.warning(f"Affix {affix_id} already exists")
            return False


async def edit_mapping(
    affix_id: int,
    name: Optional[str] = None,
    category: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    """Edit an existing affix mapping. Only updates provided fields."""
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if category is not None:
        updates.append("category = ?")
        params.append(category)
    if description is not None:
        updates.append("description = ?")
        params.append(description)

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(affix_id)

    sql = f"UPDATE affix_mappings SET {', '.join(updates)} WHERE affix_id = ?"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount > 0


async def delete_mapping(affix_id: int) -> bool:
    """Delete an affix mapping. Returns True if deleted."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM affix_mappings WHERE affix_id = ?", (affix_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_mapping(affix_id: int) -> Optional[Dict[str, Any]]:
    """Get a single affix mapping."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM affix_mappings WHERE affix_id = ?", (affix_id,)
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def get_all_mappings(
    category: str = "",
    page: int = 1,
    per_page: int = 25,
) -> Tuple[List[Dict[str, Any]], int]:
    """Get all affix mappings with pagination. Returns (items, total_count)."""
    offset = (page - 1) * per_page
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        if category:
            cursor = await db.execute(
                "SELECT * FROM affix_mappings WHERE category = ? ORDER BY affix_id LIMIT ? OFFSET ?",
                (category, per_page, offset),
            )
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM affix_mappings WHERE category = ?", (category,)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM affix_mappings ORDER BY affix_id LIMIT ? OFFSET ?",
                (per_page, offset),
            )
            count_cursor = await db.execute("SELECT COUNT(*) FROM affix_mappings")

        rows = await cursor.fetchall()
        total = (await count_cursor.fetchone())[0]
        return [dict(r) for r in rows], total


async def get_all_affix_ids() -> Set[int]:
    """Get all affix IDs from the database as a set for fast lookup."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT affix_id FROM affix_mappings")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def get_all_categories() -> List[str]:
    """Get all distinct categories."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT DISTINCT category FROM affix_mappings WHERE category != '' ORDER BY category"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def search_mappings(query: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Search affix mappings by name or ID."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM affix_mappings
               WHERE name LIKE ? OR CAST(affix_id AS TEXT) LIKE ?
               ORDER BY affix_id LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Data Scanner – finds all affix IDs in a JSON structure
# ---------------------------------------------------------------------------

def _extract_affix_ids(data: Any, path: str = "") -> List[Tuple[int, str, Any]]:
    """
    Recursively walk a JSON data structure and find all affix IDs along with
    their context path and associated value.

    Returns list of (affix_id, path_description, associated_value)
    """
    found: List[Tuple[int, str, Any]] = []

    if isinstance(data, dict):
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key

            if key in SINGLE_AFFIX_KEYS:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    found.append((int(value), f"{current_path}", value))

            elif key in PAIR_AFFIX_KEYS:
                if isinstance(value, list) and len(value) >= 2:
                    affix_id = value[0]
                    affix_val = value[1]
                    if isinstance(affix_id, (int, float)):
                        found.append((int(affix_id), f"{current_path}[0]", affix_val))

            elif key in PAIR_LIST_AFFIX_KEYS:
                if isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, list) and len(item) >= 2:
                            affix_id = item[0]
                            affix_val = item[1]
                            if isinstance(affix_id, (int, float)):
                                found.append((int(affix_id), f"{current_path}[{i}][0]", affix_val))

            elif key in DICT_KEY_AFFIX_KEYS:
                if isinstance(value, dict):
                    for inner_key, inner_value in value.items():
                        if isinstance(inner_value, dict):
                            for affix_key, affix_val in inner_value.items():
                                try:
                                    affix_id = int(affix_key)
                                    found.append((affix_id, f"{current_path}.{inner_key}.{affix_key}", affix_val))
                                except (ValueError, TypeError):
                                    pass
                            else:
                                found.extend(_extract_affix_ids(inner_value, f"{current_path}.{inner_key}"))
                        else:
                            found.extend(_extract_affix_ids(inner_value, f"{current_path}.{inner_key}"))

            elif key in DICT_VALUE_LIST_AFFIX_KEYS:
                if isinstance(value, dict):
                    for inner_key, inner_value in value.items():
                        if isinstance(inner_value, list):
                            for j, item in enumerate(inner_value):
                                if isinstance(item, (int, float)):
                                    found.append((int(item), f"{current_path}.{inner_key}[{j}]", item))
                                else:
                                    found.extend(_extract_affix_ids(item, f"{current_path}.{inner_key}[{j}]"))
                        else:
                            found.extend(_extract_affix_ids(inner_value, f"{current_path}.{inner_key}"))

            else:
                found.extend(_extract_affix_ids(value, current_path))

    elif isinstance(data, list):
        for i, item in enumerate(data):
            found.extend(_extract_affix_ids(item, f"{path}[{i}]"))

    return found


async def find_unmapped_affixes(data: Any) -> List[Tuple[int, str, Any]]:
    """
    Scan data for affix IDs that are NOT yet mapped in the database.
    Returns list of (affix_id, path, associated_value) for unmapped IDs.
    """
    all_found = _extract_affix_ids(data)
    mapped_ids = await get_all_affix_ids()

    unmapped = []
    seen_ids: Set[int] = set()
    for affix_id, path, value in all_found:
        if affix_id not in mapped_ids and affix_id not in seen_ids:
            unmapped.append((affix_id, path, value))
            seen_ids.add(affix_id)

    return unmapped


# ---------------------------------------------------------------------------
# Universal Mapper
# ---------------------------------------------------------------------------

async def _load_affix_cache() -> Dict[int, Dict[str, Any]]:
    """Load all affix IDs and metadata into a dict for fast mapping."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT affix_id, name, value_format, minimum, maximum, name_min, name_max FROM affix_mappings"
        )
        rows = await cursor.fetchall()
        return {
            row[0]: {
                "name": row[1],
                "value_format": row[2] or "",
                "minimum": row[3],
                "maximum": row[4],
                "name_min": row[5],
                "name_max": row[6],
            }
            for row in rows
        }


def _make_affix_object(affix_id: int, info: Dict[str, Any]) -> Dict[str, Any]:
    """Create a standard affix marker object with display metadata."""
    return {
        "_affix": True,
        "id": affix_id,
        "name": info.get("name", str(affix_id)),
        "format": info.get("value_format", ""),
        "min": info.get("minimum"),
        "max": info.get("maximum"),
        "name_min": info.get("name_min"),
        "name_max": info.get("name_max"),
    }


async def map_data(data: Any) -> Any:
    """
    Recursively walk a JSON data structure and replace affix IDs with
    human-readable marker objects, but ONLY in positions that are known
    to hold affix IDs (based on key name patterns).

    This ensures "1000" in one context isn't mistaken for "1000" in another.
    """
    cache = await _load_affix_cache()
    if not cache:
        return data  # No mappings defined yet

    return _map_recursive(data, cache, parent_key="")


def _map_recursive(data: Any, cache: Dict[int, str], parent_key: str) -> Any:
    """Internal recursive mapper."""
    if isinstance(data, dict):
        return _map_dict(data, cache, parent_key)
    elif isinstance(data, list):
        return _map_list(data, cache, parent_key)
    else:
        return data


def _map_dict(data: dict, cache: Dict[int, str], parent_key: str) -> dict:
    """Map values inside a dictionary."""
    result = {}

    for key, value in data.items():
        # Check if this key is a known affix-key pattern
        if key in SINGLE_AFFIX_KEYS:
            # The value itself is an affix ID
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                int_val = int(value)
                if int_val in cache:
                    result[key] = _make_affix_object(int_val, cache[int_val])
                else:
                    result[key] = value
            else:
                result[key] = _map_recursive(value, cache, key)

        elif key in PAIR_AFFIX_KEYS:
            # Value is [affix_id, value]
            if isinstance(value, list) and len(value) >= 2:
                affix_id = value[0]
                affix_val = value[1]
                if isinstance(affix_id, (int, float)) and int(affix_id) in cache:
                    int_id = int(affix_id)
                    result[key] = [
                        _make_affix_object(int_id, cache[int_id]),
                        affix_val,
                    ]
                else:
                    result[key] = _map_recursive(value, cache, key)
            else:
                result[key] = _map_recursive(value, cache, key)

        elif key in PAIR_LIST_AFFIX_KEYS:
            # Value is a list of [affix_id, value] pairs
            if isinstance(value, list):
                mapped_pairs = []
                for item in value:
                    if isinstance(item, list) and len(item) >= 2:
                        affix_id = item[0]
                        affix_val = item[1]
                        if isinstance(affix_id, (int, float)) and int(affix_id) in cache:
                            int_id = int(affix_id)
                            mapped_pairs.append([
                                _make_affix_object(int_id, cache[int_id]),
                                affix_val,
                            ])
                        else:
                            mapped_pairs.append(_map_recursive(item, cache, key))
                    else:
                        mapped_pairs.append(_map_recursive(item, cache, key))
                result[key] = mapped_pairs
            else:
                result[key] = _map_recursive(value, cache, key)

        elif key in DICT_KEY_AFFIX_KEYS:
            # Value is a dict where inner keys are affix IDs
            if isinstance(value, dict):
                mapped_inner = {}
                for inner_key, inner_value in value.items():
                    # The inner dict's keys are affix IDs
                    if isinstance(value[inner_key], dict):
                        # e.g. det_history: {"1": {"270702": 7}}
                        inner_dict = value[inner_key]
                        mapped_inner_dict = {}
                        for affix_key, affix_val in inner_dict.items():
                            try:
                                affix_id = int(affix_key)
                                if affix_id in cache:
                                    affix_obj = _make_affix_object(affix_id, cache[affix_id])
                                    affix_obj["value"] = affix_val
                                    mapped_inner_dict[str(affix_key)] = affix_obj
                                else:
                                    mapped_inner_dict[str(affix_key)] = affix_val
                            except (ValueError, TypeError):
                                mapped_inner_dict[str(affix_key)] = affix_val
                        mapped_inner[inner_key] = mapped_inner_dict
                    else:
                        mapped_inner[inner_key] = _map_recursive(inner_value, cache, key)
                result[key] = mapped_inner
            else:
                result[key] = _map_recursive(value, cache, key)

        elif key in DICT_VALUE_LIST_AFFIX_KEYS:
            # Value is a dict where values are lists of affix IDs
            if isinstance(value, dict):
                mapped_inner = {}
                for inner_key, inner_value in value.items():
                    if isinstance(inner_value, list):
                        mapped_list = []
                        for item in inner_value:
                            if isinstance(item, (int, float)) and int(item) in cache:
                                int_id = int(item)
                                mapped_list.append(_make_affix_object(int_id, cache[int_id]))
                            else:
                                mapped_list.append(_map_recursive(item, cache, key))
                        mapped_inner[inner_key] = mapped_list
                    else:
                        mapped_inner[inner_key] = _map_recursive(inner_value, cache, key)
                result[key] = mapped_inner
            else:
                result[key] = _map_recursive(value, cache, key)

        else:
            # Not a known affix key – recurse normally
            result[key] = _map_recursive(value, cache, key)

    return result


def _map_list(data: list, cache: Dict[int, str], parent_key: str) -> list:
    """Map values inside a list."""
    result = []
    for item in data:
        result.append(_map_recursive(item, cache, parent_key))
    return result