"""Shared avatar path constants for the 6 subfolder layout.

Both `cogs/live_chat_cog.py` and `cogs/wwm_cog.py` need body-type-aware
avatar lookups, but they shouldn't import each other (the live chat cog
imports `utility.wwm` and `utility.api_constants` only). Putting the path
constants here lets both cogs share them without creating a circular
import.

The folder layout (still vs animated × male / female / shared) is:

  data/avatars/still_male/      – PNG-only, male-only still images
  data/avatars/animated_male/   – WEBP-only, male-only animated
  data/avatars/still_female/    – PNG-only, female-only still images
  data/avatars/animated_female/ – WEBP-only, female-only animated
  data/avatars/still_shared/    – PNG-only, works for both genders
  data/avatars/animated_shared/ – WEBP-only, works for both genders
  data/avatars/mapped/<sub>/{head_id}.{ext}
                                – approved mappings; the body-type-aware
                                  resolver searches the 6 subfolders
                                  in priority order, falling back to
                                  legacy flat files in `mapped/` root.

The legacy flat `data/avatars/mapped/{head_id}.{ext}` path is still
checked as a last-resort fallback for any pre-existing flat-style mappings
that haven't been moved into a subfolder yet.
"""
from __future__ import annotations

from pathlib import Path

from settings import BASE_DIR


# ── Root avatar directory ──
AVATARS_DIR = BASE_DIR / "data" / "avatars"

# ── 6 source subfolders (the picker scans these) ──
AVATARS_STILL_MALE_DIR = AVATARS_DIR / "still_male"
AVATARS_ANIMATED_MALE_DIR = AVATARS_DIR / "animated_male"
AVATARS_STILL_FEMALE_DIR = AVATARS_DIR / "still_female"
AVATARS_ANIMATED_FEMALE_DIR = AVATARS_DIR / "animated_female"
AVATARS_STILL_SHARED_DIR = AVATARS_DIR / "still_shared"
AVATARS_ANIMATED_SHARED_DIR = AVATARS_DIR / "animated_shared"

# ── 6 mapped subfolders (the body-type-aware resolver scans these) ──
AVATARS_MAPPED_DIR = AVATARS_DIR / "mapped"
AVATARS_MAPPED_STILL_MALE_DIR = AVATARS_MAPPED_DIR / "still_male"
AVATARS_MAPPED_ANIMATED_MALE_DIR = AVATARS_MAPPED_DIR / "animated_male"
AVATARS_MAPPED_STILL_FEMALE_DIR = AVATARS_MAPPED_DIR / "still_female"
AVATARS_MAPPED_ANIMATED_FEMALE_DIR = AVATARS_MAPPED_DIR / "animated_female"
AVATARS_MAPPED_STILL_SHARED_DIR = AVATARS_MAPPED_DIR / "still_shared"
AVATARS_MAPPED_ANIMATED_SHARED_DIR = AVATARS_MAPPED_DIR / "animated_shared"

# ── All 6 source subfolders in still→animated order (used for cleanup) ──
AVATARS_ALL_SOURCE_SUBFOLDERS: list[Path] = [
    AVATARS_STILL_MALE_DIR,
    AVATARS_STILL_FEMALE_DIR,
    AVATARS_STILL_SHARED_DIR,
    AVATARS_ANIMATED_MALE_DIR,
    AVATARS_ANIMATED_FEMALE_DIR,
    AVATARS_ANIMATED_SHARED_DIR,
]

# ── All 6 source subfolders per body_type, in picker display order ──
# Picker concatenates files from these (gender-specific first, shared last),
# in still→animated order, so the fast PNGs load before the slow WEBPs.
AVATARS_SOURCE_SUBFOLDERS_BY_BODY_TYPE: dict[int, list[Path]] = {
    0: [  # female
        AVATARS_STILL_FEMALE_DIR,
        AVATARS_STILL_SHARED_DIR,
        AVATARS_ANIMATED_FEMALE_DIR,
        AVATARS_ANIMATED_SHARED_DIR,
    ],
    1: [  # male
        AVATARS_STILL_MALE_DIR,
        AVATARS_STILL_SHARED_DIR,
        AVATARS_ANIMATED_MALE_DIR,
        AVATARS_ANIMATED_SHARED_DIR,
    ],
}

# ── Lookup priority for the body-type-aware resolver ──
# The first existing file wins.
# Female:  still_female → animated_female → still_shared → animated_shared
# Male:    still_male   → animated_male   → still_shared → animated_shared
# Unknown: only the 2 shared subfolders.
AVATARS_MAPPED_LOOKUP_ORDER_BY_BODY_TYPE: dict[int, list[Path]] = {
    0: [  # female
        AVATARS_MAPPED_STILL_FEMALE_DIR,
        AVATARS_MAPPED_ANIMATED_FEMALE_DIR,
        AVATARS_MAPPED_STILL_SHARED_DIR,
        AVATARS_MAPPED_ANIMATED_SHARED_DIR,
    ],
    1: [  # male
        AVATARS_MAPPED_STILL_MALE_DIR,
        AVATARS_MAPPED_ANIMATED_MALE_DIR,
        AVATARS_MAPPED_STILL_SHARED_DIR,
        AVATARS_MAPPED_ANIMATED_SHARED_DIR,
    ],
}

# ── Valid subfolder values (string form, lowercase, with underscores) ──
AVATAR_VALID_SUBFOLDERS: set[str] = {
    "still_male",
    "animated_male",
    "still_female",
    "animated_female",
    "still_shared",
    "animated_shared",
}

# ── Body-type constants (mirror the WWM API) ──
BODY_TYPE_FEMALE: int = 0
BODY_TYPE_MALE: int = 1


__all__ = [
    "AVATARS_DIR",
    "AVATARS_STILL_MALE_DIR",
    "AVATARS_ANIMATED_MALE_DIR",
    "AVATARS_STILL_FEMALE_DIR",
    "AVATARS_ANIMATED_FEMALE_DIR",
    "AVATARS_STILL_SHARED_DIR",
    "AVATARS_ANIMATED_SHARED_DIR",
    "AVATARS_MAPPED_DIR",
    "AVATARS_MAPPED_STILL_MALE_DIR",
    "AVATARS_MAPPED_ANIMATED_MALE_DIR",
    "AVATARS_MAPPED_STILL_FEMALE_DIR",
    "AVATARS_MAPPED_ANIMATED_FEMALE_DIR",
    "AVATARS_MAPPED_STILL_SHARED_DIR",
    "AVATARS_MAPPED_ANIMATED_SHARED_DIR",
    "AVATARS_ALL_SOURCE_SUBFOLDERS",
    "AVATARS_SOURCE_SUBFOLDERS_BY_BODY_TYPE",
    "AVATARS_MAPPED_LOOKUP_ORDER_BY_BODY_TYPE",
    "AVATAR_VALID_SUBFOLDERS",
    "BODY_TYPE_FEMALE",
    "BODY_TYPE_MALE",
]
