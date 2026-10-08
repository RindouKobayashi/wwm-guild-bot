"""Copy verified original artwork from a toolkit export into a portable bot bundle."""
import argparse
import csv
import hashlib
import html
import json
import re
from collections import Counter
from pathlib import Path
import shutil
import sys
import unicodedata
from datetime import datetime, timezone, timedelta

BOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0, str(BOT))


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def choose_asset(art, resource):
    candidates = [art["assets"][uid] for uid in art["references"].get(resource, [])
                  if uid in art["assets"] and art["assets"][uid].get("status") == "EXPORTED"]
    if not candidates:
        return None
    hashes = {a.get("png_sha256") for a in candidates}
    if len(hashes) == 1 and None not in hashes:
        return candidates[0]
    # The registry also exports original normal/4k variants. Accept only the
    # same texture name with that explicit suffix, never arbitrary alternatives.
    bases = {a.get("n", "").removesuffix("_4k") for a in candidates}
    if len(bases) != 1 or "" in bases or any(not a.get("png_sha256") for a in candidates):
        return None
    if any(len(a.get("size", [])) != 2 for a in candidates):
        return None
    ordered = sorted(candidates, key=lambda a: (a["size"][0] * a["size"][1], a["png_sha256"]), reverse=True)
    largest = ordered[0]
    if any(a["size"] == largest["size"] and a["png_sha256"] != largest["png_sha256"] for a in ordered):
        return None
    return largest


def direct_resources(catalogue, dungeon, field):
    key = "trial_displays:" + dungeon["row_id"]
    entry = catalogue.get(key, {})
    variants = entry.get("variants", [])
    if entry.get("names") and dungeon.get("name") not in entry["names"]:
        return set()
    if not variants or any(str(v.get("raw", {}).get("fuben_id")) != dungeon["fuben_id"] for v in variants):
        return set()
    return {a["resource"] for a in entry.get("art", [])
            if a.get("owner") == key and a.get("field") == field
            and a.get("relation") == "direct table field"
            and all(v.get("raw", {}).get(field) == a["resource"] for v in variants)}


def dungeon_family(title):
    """Remove only explicit leaderboard metadata; retain meaningful name words.

    Phase/season boards share dungeon-level art, never a claimed phase portrait.
    Challenge is returned separately and must agree with the trial row's mode.
    Unexplained ordinals and localization aliases are deliberately not guessed.
    """
    title = re.sub(r"\s*[-:]?\s*Top\s+\d+(?:\s*[-:]?\s*(?:Phase|Season)\s+\d+)?\s*$",
                   "", title, flags=re.IGNORECASE).strip()
    challenge = bool(re.search(r"\bchallenge\b", title, re.IGNORECASE))
    title = re.sub(r"\bchallenge\b", "", title, flags=re.IGNORECASE)
    return re.sub(r"[^\w]+", " ", title.casefold()).strip(), "challenge" if challenge else "normal"


def consistent_raw(entry, fields):
    variants = entry.get("variants", []) if entry else []
    if not variants:
        raise ValueError("missing instance/profile record")
    signatures = {json.dumps({f:v.get("raw", {}).get(f) for f in fields}, sort_keys=True) for v in variants}
    if len(signatures) != 1:
        raise ValueError("conflicting instance/profile versions")
    return variants[0]["raw"]


def resolve_boss_art(catalogue, matches, art):
    """Cross-check fight entities, then use exact named boss introduction art.

    all_boss_id and trial image fields can be copied template data. They are
    not sufficient: the boss_no must match the actual fight_entity_id instead.
    next_id is followed only inside the same instance_group. All difficulty
    rows must yield the same complete gallery; never guess a phase-to-boss ID.
    """
    if not matches:
        return [], "no exact dungeon family/mode match"
    by_entity = {}
    for key, profile in catalogue.items():
        if profile.get("family") != "boss_profiles" or len(profile.get("names", [])) != 1:
            continue
        try:
            raw = consistent_raw(profile, ("boss_id", "boss_no"))
        except ValueError:
            continue
        if raw.get("boss_id") != profile.get("id") or not isinstance(raw.get("boss_no"), int):
            continue
        by_entity.setdefault(raw["boss_no"], []).append((key, profile))
    galleries = []
    try:
        for dungeon in matches:
            trial = catalogue.get("trial_displays:" + dungeon["row_id"])
            trial_raw = consistent_raw(trial, ("id", "name", "fuben_id"))
            if str(trial_raw.get("id")) != dungeon["row_id"] or str(trial_raw.get("fuben_id")) != dungeon["fuben_id"] or dungeon["name"] not in trial.get("names", []):
                raise ValueError("trial identity/instance mismatch")
            identifier, group, seen, images = int(dungeon["fuben_id"]), None, set(), []
            while identifier:
                if identifier in seen or len(seen) >= 16:
                    raise ValueError("cyclic or excessive instance chain")
                seen.add(identifier)
                instance = catalogue.get("instances:" + str(identifier))
                raw = consistent_raw(instance, ("id", "instance_group", "fight_entity_id", "next_id"))
                if raw.get("id") != identifier or not isinstance(raw.get("instance_group"), int):
                    raise ValueError("invalid instance identity/group")
                if group is None:
                    group = raw["instance_group"]
                if raw["instance_group"] != group:
                    break  # next unlock/instance is not part of this dungeon.
                entities = raw.get("fight_entity_id")
                if not isinstance(entities, list) or not entities:
                    raise ValueError("no fight entities")
                for entity in entities:
                    profiles = by_entity.get(entity, [])
                    names = {p["names"][0] for _, p in profiles}
                    if len(names) != 1:
                        raise ValueError("fight entity has no unambiguous boss profile")
                    name = next(iter(names))
                    replays = [(k, e) for k, e in catalogue.items() if e.get("family") == "replay_encounters" and e.get("names") == [name]]
                    resources = set()
                    for key, replay in replays:
                        replay_raw = consistent_raw(replay, ("id", "name", "introduction_pic"))
                        resource = replay_raw.get("introduction_pic")
                        if not resource or not any(x.get("owner") == key and x.get("relation") == "direct table field" and x.get("field") == "introduction_pic" and x.get("resource") == resource for x in replay.get("art", [])):
                            raise ValueError("unverified replay artwork field")
                        resources.add(resource)
                    if len(resources) != 1 or not choose_asset(art, next(iter(resources))):
                        raise ValueError("no original introduction artwork for " + name)
                    resource = next(iter(resources))
                    if any(x["boss_name"] == name and x["resource"] == resource for x in images):
                        continue
                    images.append({"boss_name": name, "resource": resource,
                                   "evidence": {"trial_row": dungeon["row_id"], "instance_id": identifier,
                                                "instance_group": group, "fight_entity_id": entity,
                                                "boss_profiles": [k for k, _ in profiles],
                                                "replay_entries": [k for k, _ in replays],
                                                "art_field": "introduction_pic"}})
                identifier = raw.get("next_id")
                if identifier is not None and not isinstance(identifier, int):
                    raise ValueError("invalid next instance ID")
            if not images or len(images) > 10:
                raise ValueError("missing/excessive boss gallery")
            galleries.append(images)
        signatures = {tuple((x["boss_name"], x["resource"]) for x in images) for images in galleries}
        if len(signatures) != 1:
            raise ValueError("difficulty rows disagree on boss chain/artwork")
        result = galleries[0]
        for i, image in enumerate(result):
            image["evidence_by_difficulty"] = [g[i]["evidence"] for g in galleries]
        return result, "fight entity/profile agreement and exact named boss introduction art"
    except (ValueError, KeyError, TypeError) as error:
        return [], str(error)


def resolve_dungeon_card(rank, bindings, art):
    """Use explicit activity-card ranking IDs, then exact extracted card names.

    Replacement IDs are phase/season aliases declared by the game itself.
    No boss artwork, trial template fields or ordinal guesses are accepted.
    """
    rows = bindings.get("rows", [])
    matched = []
    for row in rows:
        raw = row.get("raw", {})
        replacements = raw.get("rank_id_replace", [])
        if not isinstance(replacements, list):
            continue
        if rank["rank_name"] in [raw.get("rank_id"), *replacements]:
            matched.append(row)
    join = "explicit activity-card rank_id/rank_id_replace"
    if not matched:
        family = dungeon_family(rank["leaderboard_title"])
        prefix = "rank_team10_dungeon_" if rank["team"] == "hr" else "rank_team_dungeon_"
        matched = [r for r in rows if isinstance(r.get("name"), str)
                   and dungeon_family(r["name"]) == family
                   and str(r.get("raw", {}).get("rank_id", "")).startswith(prefix)]
        join = "exact extracted activity-card name/mode and ranking namespace"
    resources = {r.get("raw", {}).get("game_list_image") for r in matched}
    if not matched:
        return [], "No explicit ranking binding or exact activity-card name", join
    if len(resources) != 1 or not all(isinstance(r, str) and r.startswith("wulinlu_tongyou_pic_xj_") for r in resources):
        return [], "Conflicting activity-card artwork bindings", join
    resource = resources.pop()
    if not choose_asset(art, resource):
        return [], "Original dungeon card texture unavailable", join
    evidence = [{k:r.get(k) for k in ("source", "key", "locator", "name")} |
                {"rank_id":r["raw"].get("rank_id"), "rank_id_replace":r["raw"].get("rank_id_replace", []),
                 "game_list_image":resource} for r in matched]
    return [{"boss_name": "Dungeon selection card", "resource":resource,
             "evidence_by_difficulty":evidence}], "Original dungeon activity card; " + join, join


def resolve_reviewed_card(rank, matches, catalogue, reviews, art):
    """Resolve hash-pinned whole-dungeon cards through explicit trial mode links.

    Direct guild-card references and visually reviewed original art identities
    remain distinct evidence types. A changed image requires another review.
    """
    candidates=[]
    for review in reviews.get("reviews", []):
        key="trial_displays:" + str(review["trial_row_id"])
        entry=catalogue.get(key, {})
        try:
            raw=consistent_raw(entry, ("id", "fuben_id", "name", "guild_image", "jingsu_index"))
        except ValueError:
            continue
        if (raw.get("id") != review["trial_row_id"] or entry.get("names") != [review.get("expected_trial_name")]
                or raw.get("fuben_id") != review.get("expected_fuben_id")
                or raw.get("jingsu_index") != review.get("expected_challenge_row_id")):
            continue
        if dungeon_family(entry["names"][0])[0] != dungeon_family(rank["leaderboard_title"])[0]:
            continue
        linked={str(raw["id"]), str(raw.get("jingsu_index"))}
        matched=[]
        for d in matches:
            if d["row_id"] not in linked:
                continue
            try:
                mode_raw=consistent_raw(catalogue.get("trial_displays:"+d["row_id"]), ("id", "fuben_id", "normal_dungeon_index"))
            except ValueError:
                continue
            if str(mode_raw.get("fuben_id")) != d["fuben_id"]:
                continue
            if d["mode"] == "challenge" and (d["row_id"] != str(raw.get("jingsu_index")) or mode_raw.get("normal_dungeon_index") != raw["id"]):
                continue
            if d["mode"] == "normal" and d["row_id"] != str(raw["id"]):
                continue
            matched.append(d)
        if not matched:
            continue
        resource=review["resource"]
        if review["basis"] == "direct_guild_card" and str(raw.get("guild_image", "")).removesuffix(".png") != resource:
            continue
        asset=choose_asset(art, resource)
        if not asset or asset.get("png_sha256") != review["png_sha256"]:
            continue
        evidence={"trial_row":key,"trial_name":entry["names"][0],"fuben_id":raw["fuben_id"],
                  "jingsu_index":raw.get("jingsu_index"),"matched_mode_rows":[d["row_id"] for d in matched],
                  "basis":review["basis"],"notes":review["notes"],"reviewed_png_sha256":review["png_sha256"]}
        candidates.append((resource,evidence))
    if not candidates or len({r for r,e in candidates}) != 1:
        return [], "No matching hash-pinned dungeon card review", "unresolved"
    resource=candidates[0][0]
    basis=candidates[0][1]["basis"]
    return [{"boss_name":"Dungeon selection card","resource":resource,
             "evidence_by_difficulty":[e for r,e in candidates]}], "Original dungeon card; " + basis, basis


def readable_slug(value):
    value = value.replace("’", "").replace("'", "").replace("&", " and ")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:100] or "unnamed-dungeon"


def readable_bundle(destination, fingerprints):
    """Reuse identical exports; give new snapshots dated, readable names."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for folder in sorted(destination.glob("ranking-artwork-*")):
        try:
            if json.loads((folder / "manifest.json").read_text(encoding="utf-8")).get("source_sha256") == fingerprints:
                return folder.name
        except (OSError, ValueError):
            pass
    date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    base = "ranking-artwork-" + date
    name = base
    revision = 2
    while (destination / name).exists():
        name = f"{base}-revision-{revision:02d}"
        revision += 1
    return name


def copy_art(art, resource, source_root, output, filename=None):
    asset = choose_asset(art, resource)
    if not asset:
        raise ValueError("Missing or ambiguous artwork")
    source = (source_root / asset["file"]).resolve()
    if not source.is_relative_to(source_root):
        raise ValueError("Artwork path outside export")
    raw = source.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(raw).hexdigest() != asset["png_sha256"]:
        raise ValueError("Artwork hash/signature mismatch")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Artwork exceeds upload budget")
    filename = filename or "dungeon-" + readable_slug(resource.removesuffix(".png")) + ".png"
    target = (output / filename).resolve()
    if not target.is_relative_to(output.resolve()):
        raise ValueError("Artwork destination outside bundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(source, target)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != asset["png_sha256"]:
        raise ValueError("Existing bot artwork changed; refusing to overwrite it")
    return {"file": filename, "sha256": asset["png_sha256"], "bytes": len(raw)}


def build(run, destination, cards_root=None):
    run = Path(run).resolve()
    rank_path = run / "dungeons/trial_rank_ids.csv"
    dungeon_path = run / "dungeons/trial_dungeons.csv"
    art_path = run / "dungeon_library/art_manifest.json"
    catalogue_path = run / "dungeon_library/dungeon_catalogue.json"
    ranks, dungeons = read_rows(rank_path), read_rows(dungeon_path)
    art = json.loads(art_path.read_text(encoding="utf-8"))
    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8")) if catalogue_path.exists() else {}
    fingerprints = {str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (rank_path, dungeon_path, art_path, catalogue_path) if p.exists()}
    cards_root = Path(cards_root) if cards_root is not None else BOT / "maintenance/research/output/dungeon_cards"
    reviews_path = BOT / "maintenance/assets/dungeon_card_reviews.json"
    reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else {"reviews": []}
    bindings_path = cards_root / "bindings.json"
    cards_art_path = cards_root / "art_manifest.json"
    bindings = json.loads(bindings_path.read_text(encoding="utf-8")) if bindings_path.exists() else {"rows": []}
    if cards_art_path.exists():
        art = json.loads(cards_art_path.read_text(encoding="utf-8"))
        art_path = cards_art_path
    for p in (bindings_path, cards_art_path, reviews_path):
        if p.exists():
            fingerprints[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    fingerprints["art_selection_policy"] = "readable-dungeon-artwork-paths-v10"
    bundle_id = readable_bundle(destination, fingerprints)
    output = Path(destination) / bundle_id
    output.mkdir(parents=True, exist_ok=True)
    boards = {}
    for rank in ranks:
        team, identifier = rank["team"], rank["dungeon_id"]
        prefix = "rank_team10_dungeon_" if team == "hr" else "rank_team_dungeon_"
        if team not in ("hr", "st") or not identifier.isdigit() or rank["rank_name"] != prefix + identifier or rank["team_agrees"] not in ("True", ""):
            raise ValueError("Unvalidated leaderboard namespace")
        key = f"{team}:{identifier}"
        if key in boards:
            raise ValueError("Duplicate leaderboard")
        title = rank["leaderboard_title"]
        name, mode = dungeon_family(title)
        matches = [d for d in dungeons if dungeon_family(d["name"])[0] == name and d["mode"] == mode]
        images, reason, join = resolve_dungeon_card(rank, bindings, art)
        if not images and reason != "Conflicting activity-card artwork bindings":
            images, reason, join = resolve_reviewed_card(rank, matches, catalogue, reviews, art)
        row = {"rank_name": rank["rank_name"], "title": title, "file": None,
               "images": [], "art_status": "unresolved", "art_reason": reason,
               "join": join,
               "alias_used": False, "dungeon_family": name, "mode": mode,
               "dungeon_rows": [d["row_id"] for d in matches]}
        for image in images:
            category = "hero-realm" if team == "hr" else "sword-trial"
            display_name = re.sub(r"\s*[-:]?\s*Top\s+\d+.*$", "", title, flags=re.IGNORECASE)
            display_name = re.sub(r"\bchallenge\b", "", display_name, flags=re.IGNORECASE)
            filename = "artwork/" + category + "/" + readable_slug(display_name) + ("-challenge" if mode == "challenge" else "") + ".png"
            image.update(copy_art(art, image["resource"], art_path.parent, output, filename))
        if sum(x["bytes"] for x in images) > 8 * 1024 * 1024:
            images = []
            row["art_reason"] = "Complete gallery exceeds upload budget"
        if images:
            row.update(images=images, file=images[0]["file"], resource=images[0]["resource"],
                       sha256=images[0]["sha256"], art_status="reviewed_dungeon_card" if join == "reviewed_boss_art_identity" else "verified_dungeon_card",
                       art_scope="whole_dungeon_activity_card")
        boards[key] = row
    usage = Counter(image["file"] for row in boards.values() for image in row["images"])
    signatures = Counter(tuple((i["boss_name"], i["file"]) for i in r["images"]) for r in boards.values() if r["images"])
    for row in boards.values():
        row["shared_by_boards"] = signatures.get(tuple((i["boss_name"], i["file"]) for i in row["images"]), 0)
    manifest = {"source_run": str(run), "source_sha256": fingerprints, "boards": boards}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    sections = []
    for key, row in boards.items():
        pictures = ''.join('<figure><img loading="lazy" src="' + i["file"] + '" alt="' + html.escape(i["boss_name"], quote=True) + '"><figcaption>' + html.escape(i["boss_name"] + " — " + i["resource"]) + '</figcaption></figure>' for i in row["images"])
        evidence = html.escape(json.dumps([i["evidence_by_difficulty"] for i in row["images"]], indent=2))
        sections.append('<section><h2>' + html.escape(key + " — " + row["title"]) + '</h2><p>' + html.escape(row["art_reason"]) + '</p><div class="pictures">' + pictures + '</div><details><summary>Mapping evidence</summary><pre>' + evidence + '</pre></details></section>')
    document = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Corrected ranking artwork audit</title>'
                '<style>body{font:16px system-ui;background:#15191f;color:#e6eaf0;max-width:1200px;margin:32px auto;padding:0 20px}'
                'section{background:#222832;padding:20px;margin:20px 0;border-radius:10px}.pictures{display:flex;flex-wrap:wrap;gap:16px}figure{margin:0;max-width:48%}img{max-width:100%;max-height:350px}'
                'pre{white-space:pre-wrap;overflow-wrap:anywhere}figcaption{font-size:13px;overflow-wrap:anywhere}h2{font-size:18px}</style>'
                '<h1>Corrected ranking artwork audit</h1><p>Original whole-dungeon cards. Evidence includes activity ranking bindings, direct dungeon guild-card references and visually reviewed boss-art identities. '
                'One original card per dungeon. Evidence distinguishes explicit activity bindings, direct guild cards and hash-pinned visual reviews. Boss galleries and copied template fields are excluded.</p>' + ''.join(sections) + '</html>')
    (output / "artwork_audit.html").write_text(document, encoding="utf-8")
    temporary = Path(destination) / "latest.tmp"
    temporary.write_text(json.dumps({"run": bundle_id}), encoding="utf-8")
    temporary.replace(Path(destination) / "latest.json")
    return {"boards": len(boards), "with_art": sum(bool(r["images"]) for r in boards.values()),
            "unique_images": len({i["sha256"] for r in boards.values() for i in r["images"]}), "bundle": bundle_id, "audit": str(output / "artwork_audit.html")}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data-run", type=Path)
    parser.add_argument("--destination", type=Path, default=BOT / "data/ranking_assets")
    args = parser.parse_args()
    run = args.game_data_run
    if run is None:
        base = BOT.parent / "WWM-Toolkit/output/game_data"
        run = base / json.loads((base / "latest.json").read_text(encoding="utf-8"))["run"].replace("\\", "/")
    print(json.dumps(build(run, args.destination)))
