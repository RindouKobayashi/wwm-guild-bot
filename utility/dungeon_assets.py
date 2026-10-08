"""Bot-local ranking artwork. No game, toolkit, network or settings dependency."""
import json
import re
from pathlib import Path


def normalize_title(value):
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    value = re.sub(r"\bchallenge\b|\b(?:top|phases?|seasons?)\s+\d+\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+", " ", re.sub(r"\d{1,2}$", "", value)).strip()


class DungeonAssets:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._stamp = None
        self._entries = {}
        self._run = None

    def _refresh(self):
        try:
            pointer = self.root / "latest.json"
            stamp = pointer.stat().st_mtime_ns
            if stamp == self._stamp:
                return
            run = (self.root / json.loads(pointer.read_text(encoding="utf-8"))["run"]).resolve()
            if not run.is_relative_to(self.root):
                raise ValueError("Asset run outside bot data directory")
            data = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            self._entries = data["boards"]
            self._run, self._stamp = run, stamp
        except (OSError, ValueError, KeyError, TypeError):
            self._entries, self._run, self._stamp = {}, None, None

    def entries(self):
        """Names come from the extracted leaderboard table, never user aliases."""
        self._refresh()
        result = []
        for key, row in self._entries.items():
            if not isinstance(row, dict) or ":" not in key:
                continue
            team, identifier = key.split(":", 1)
            if team not in ("hr", "st") or not identifier.isdigit():
                continue
            prefix = "rank_team10_dungeon_" if team == "hr" else "rank_team_dungeon_"
            title = row.get("title")
            if row.get("rank_name") != prefix + identifier or not isinstance(title, str) or not title.strip():
                continue
            name = re.sub(r"\s+Top\s+\d+\s*$", "", title, flags=re.IGNORECASE).strip()
            result.append({"rank_type": team, "dungeon_id": int(identifier),
                           "name": name, "aliases": [title], "verified": True,
                           "source": "extracted_leaderboard_table"})
        return sorted(result, key=lambda e: (e["rank_type"], e["dungeon_id"]))

    def get(self, rank_type, dungeon_id):
        self._refresh()
        if rank_type not in ("hr", "st") or dungeon_id is None:
            return None
        row = self._entries.get(f"{rank_type}:{dungeon_id}")
        if not isinstance(row, dict):
            return None
        prefix = "rank_team10_dungeon_" if rank_type == "hr" else "rank_team_dungeon_"
        if row.get("rank_name") != prefix + str(dungeon_id):
            return None
        result = dict(row)
        result["path"] = None
        result["images"] = []
        if row.get("art_status") not in ("explicit_leaderboard_art", "verified_dungeon_card", "reviewed_dungeon_card"):
            return result
        images = row.get("images") or ([row] if row.get("file") else [])
        if len(images) != 1:
            return result
        resolved = []
        for item in images:
            if not isinstance(item, dict) or not item.get("file"):
                return result
            path = (self._run / item["file"]).resolve()
            if not path.is_relative_to(self._run) or not path.is_file() or not 0 < path.stat().st_size <= 8 * 1024 * 1024:
                return result
            resolved.append({**item, "path": path})
        if sum(i["path"].stat().st_size for i in resolved) > 8 * 1024 * 1024:
            return result
        result["images"] = resolved
        result["path"] = resolved[0]["path"] if resolved else None
        return result
