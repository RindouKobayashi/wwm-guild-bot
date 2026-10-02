"""
Minimal XLSX reader (stdlib only)
==================================
Shared helper for reading the item-name workbooks in ``data/``.

Uses only the standard library (``zipfile`` + ``xml.etree.ElementTree``) —
no openpyxl / pandas — matching the existing convention in this project.

Why this module exists
----------------------
The workbooks store cells in three different ways, and a naive reader that
only looks at ``<v>`` elements silently returns *zero* rows:

  * ``t="inlineStr"`` -> ``<is><t>name</t></is>``  <- the NAME columns in
    both ``All_Item_Names.xlsx`` and ``All_Equipment_Item_Names.xlsx``
  * numeric / ``t="n"`` -> ``<v>1100001</v>``      <- the item_id column
  * ``t="s"`` -> index into ``xl/sharedStrings.xml``

``iter_xlsx_rows`` normalises all three so callers just get plain strings.
"""

import zipfile
from pathlib import Path
from typing import Iterator, List, Optional
import xml.etree.ElementTree as ET

# Namespace used by the xlsx XML
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

DEFAULT_SHEET = "xl/worksheets/sheet1.xml"
SHARED_STRINGS = "xl/sharedStrings.xml"


def _cell_text(cell: Optional[ET.Element], shared: Optional[List[str]]) -> str:
    """Return the text of a single <c> cell, or "" when empty/missing."""
    if cell is None:
        return ""

    # Formulas: prefer the cached <v> result, else the inline text.
    cell_type = cell.get("t")

    if cell_type == "inlineStr":
        node = cell.find("x:is/x:t", NS)
        return node.text if node is not None and node.text else ""

    value = cell.find("x:v", NS)
    if value is not None and value.text:
        # t="s" means the number is an index into sharedStrings.
        if cell_type == "s" and shared is not None:
            try:
                return shared[int(value.text)]
            except (ValueError, IndexError):
                return ""
        return value.text

    # Some writers emit inline text without declaring t="inlineStr".
    node = cell.find("x:is/x:t", NS)
    return node.text if node is not None and node.text else ""


def _load_shared_strings(zf: zipfile.ZipFile) -> Optional[List[str]]:
    """Read xl/sharedStrings.xml when present, else None."""
    try:
        with zf.open(SHARED_STRINGS) as fh:
            root = ET.parse(fh).getroot()
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return None
    return [
        (node.text or "")
        for node in root.findall(".//x:si/x:t", NS)
    ]


def iter_xlsx_rows(
    xlsx_path,
    sheet: str = DEFAULT_SHEET,
    skip_header: bool = True,
) -> Iterator[List[str]]:
    """
    Yield each row of ``sheet`` as a list of strings.

    Empty trailing cells are preserved as "" so column positions stay stable.
    The header row is skipped by default; pass ``skip_header=False`` to keep it.

    Raises zipfile.BadZipFile / KeyError / ET.ParseError on a malformed file
    so callers can decide how loudly to fail.
    """
    path = Path(xlsx_path)
    with zipfile.ZipFile(str(path)) as zf:
        shared = _load_shared_strings(zf)
        with zf.open(sheet) as fh:
            root = ET.parse(fh).getroot()

        rows = root.findall(".//x:row", NS)
        if skip_header and rows:
            rows = rows[1:]

        for row in rows:
            yield [_cell_text(cell, shared) for cell in row.findall("x:c", NS)]
