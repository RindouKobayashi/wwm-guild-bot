"""Stage assets for review; apply them only through an explicit --apply action."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
BOT = HERE.parents[1]
sys.path.insert(0, str(BOT))
from utility.game_assets import GameAssets, ROOT, digest, validate_release
from build_game_bundle import legacy_tables, apply_exports, add_rankings, finish, read, write

SECTIONS = {'affixes', 'equipment', 'items', 'dungeons', 'abyss', 'constants', 'martial', 'pvp', 'market', 'emotes'}
RANKING_SOURCE = 'installed_named_rank_data_and_activity_cards_v2'


def pointer(base):
    relative = read(base / 'latest.json')['run'].replace('\\', '/')
    path = Path(relative)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def installed_fingerprints(game, table_root, table_fingerprints):
    stamps = table_fingerprints(table_root)
    patch = game / 'LocalData/Patch'
    patch_tables = patch / 'HD/oversea'
    if patch_tables.is_dir():
        stamps.update({'LocalData/Patch/HD/oversea/' + key.replace('\\', '/'): value
                       for key, value in table_fingerprints(patch_tables).items()})
    paths = (table_root / 'MANIFEST', game / 'Resources.mpkinfo', patch_tables / 'MANIFEST',
             patch / 'Patch.mpkdb', patch / 'Patch.mpkdb-wal', *sorted(patch.glob('LT*.mpkinfo')))
    for path in paths:
        if path.is_file():
            stamps[path.relative_to(game).as_posix()] = digest(path)
    return stamps


def extractor_fingerprints(toolkit):
    """Invalidate the unchanged shortcut when an extractor or its rules change."""
    paths = {path for folder in (toolkit / 'app', toolkit / 'app/lib', HERE)
             for pattern in ('*.py', '*.json') for path in folder.glob(pattern)}
    paths.update((toolkit / 'tools/extract_abyss_trials.py',
                  BOT / 'activity/maintenance/extract_equipment_metadata.py',
                  BOT / 'utility/api_constants.py'))
    return {f'{label}/{path.relative_to(root).as_posix()}': digest(path)
            for label, root in (('toolkit', toolkit), ('bot', BOT))
            for path in sorted(paths) if path.is_relative_to(root)}


def input_changes(before, current):
    if not before:
        return {'baseline_available': False}
    return {'baseline_available': True,
            'added': sorted(current.keys() - before.keys()),
            'removed': sorted(before.keys() - current.keys()),
            'changed': sorted(k for k in current.keys() & before.keys() if current[k] != before[k])}


def missing_card_boards(cards, boards):
    known = {row['rank_name'] for row in boards.values()}
    missing = {}
    for record in cards:
        raw = record['raw']
        replacements = raw.get('rank_id_replace', [])
        for name in [raw.get('rank_id'), *(replacements if isinstance(replacements, list) else [])]:
            if (isinstance(name, str) and re.fullmatch(r'rank_team(?:10)?_dungeon_\d+', name)
                    and name not in known):
                missing.setdefault(name, set()).add(record['name'])
    return {name: sorted(titles) for name, titles in sorted(missing.items())}


def run_step(python, script, arguments, logs, name):
    print('Updating ' + name + ' ...', flush=True)
    log = logs / (name + '.log')
    with log.open('w', encoding='utf-8') as handle:
        result = subprocess.run([str(python), '-u', str(script), *map(str, arguments)],
                                cwd=BOT, stdout=handle, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'{name} exited with status {result.returncode}. See {log}')


def publish(stage, destination=ROOT):
    """Keep one tracked current bundle and preserve replaced copies locally."""
    validate_release(stage)
    destination = Path(destination).resolve()
    name = 'game-assets-' + datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    backups = BOT / '.asset-backups/wwm' if destination == ROOT.resolve() else destination.parent / '.asset-backups'
    backups.mkdir(parents=True, exist_ok=True)
    incoming = backups / (name + '-incoming')
    previous = backups / (name + '-previous')
    target = destination / 'current'
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage, incoming)
    validate_release(incoming)
    temp = destination / (name + '.tmp')
    write(temp, {'release': 'current', 'manifest_sha256': digest(incoming / 'manifest.json')})
    replaced = False
    installed = False
    try:
        if target.exists():
            target.rename(previous)
            replaced = True
        incoming.rename(target)
        installed = True
        temp.replace(destination / 'current.json')
    except Exception:
        if installed:
            target.rename(backups / (name + '-not-applied'))
        if replaced:
            previous.rename(target)
        raise
    finally:
        temp.unlink(missing_ok=True)
    return target


def prepare_review(work, baseline_sha256):
    """Seal a staged candidate against accidental edits and a changing baseline."""
    stage = Path(work) / 'release'
    validate_release(stage)
    write(Path(work) / 'review.json', {'format': 1, 'baseline_sha256': baseline_sha256,
                                      'manifest_sha256': digest(stage / 'manifest.json')})
    write(Path(work).parent / 'latest-review.json', {'run': Path(work).name})


def apply_review(work, destination=ROOT, allow_removals=False):
    work, destination = Path(work), Path(destination)
    lock = work.parent / 'update.lock'
    with lock.open('x') as handle:
        json.dump({'pid': os.getpid(), 'applying': work.name}, handle)
    try:
        receipt = read(work / 'review.json')
        stage = work / 'release'
        if receipt.get('format') != 1 or digest(stage / 'manifest.json') != receipt['manifest_sha256']:
            raise ValueError('Reviewed candidate changed; generate a new review')
        validate_release(stage)
        current_pointer = destination / 'current.json'
        baseline = digest(current_pointer) if current_pointer.exists() else None
        if baseline != receipt['baseline_sha256']:
            raise ValueError('Active assets changed since this review; generate a new review against the current bundle')
        report = read(stage / 'report.json')
        if any(row['removed'] for row in report['review']['summary'].values()) and not allow_removals:
            raise ValueError('Review includes removals. Inspect report.html, then apply with --allow-removals if intended')
        return publish(stage, destination)
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Offline bundle validation; no game or extraction needed')
    parser.add_argument('--apply', metavar='RUN_ID', help='Apply a previously reviewed candidate, or latest; no extraction')
    parser.add_argument('--open-report', action='store_true', help='Open the generated review HTML on Windows')
    parser.add_argument('--from-existing', action='store_true', help='Package local reviewed exports, explicitly not a fresh game scan')
    parser.add_argument('--game-root', type=Path)
    parser.add_argument('--toolkit', type=Path, default=BOT.parent / 'WWM-Toolkit')
    parser.add_argument('--only', help='Comma-separated: ' + ','.join(sorted(SECTIONS)))
    parser.add_argument('--include-avatars', action='store_true', help='Also refresh Toolkit avatar exports; does not publish bot avatars or guarantee complete animation rendering')
    parser.add_argument('--force', action='store_true', help='Run extractors even when source hashes match')
    parser.add_argument('--allow-removals', action='store_true', help='Explicitly permit reviewed missing IDs')
    args = parser.parse_args()
    if args.apply:
        if args.check or args.only or args.from_existing or args.game_root or args.force or args.include_avatars or args.open_report:
            parser.error('--apply cannot be combined with extraction or --check options')
        try:
            run_id = read(BOT / '.asset-builds/latest-review.json')['run'] if args.apply == 'latest' else args.apply
            if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{6}', run_id):
                raise ValueError('Use the run ID printed by the review command, or latest')
            target = apply_review(BOT / '.asset-builds' / run_id, allow_removals=args.allow_removals)
            print('Applied reviewed release: ' + str(target))
            print('Commit assets/wwm/current.json and assets/wwm/current/. Local backups stay ignored. Restart bot/Activity after pulling.')
            return 0
        except (OSError, ValueError, KeyError) as exc:
            print('Apply stopped; active assets retained: ' + str(exc), file=sys.stderr)
            return 1
    if args.allow_removals:
        parser.error('--allow-removals belongs to --apply after reviewing the generated report')
    if args.from_existing and args.include_avatars:
        parser.error('--include-avatars requires a fresh extraction, not --from-existing')
    existing = GameAssets()
    baseline_sha256 = digest(ROOT / 'current.json') if (ROOT / 'current.json').exists() else None
    if args.check:
        if existing.directory is None:
            print('No valid published bundle: ' + str(existing.error or 'not yet built'))
            return 1
        print('Validated portable release: ' + existing.directory.name)
        print(json.dumps(existing.manifest['counts'], indent=2))
        return 0
    selected = set(args.only.split(',')) if args.only else SECTIONS.copy()
    if not selected or not selected <= SECTIONS:
        parser.error('--only contains unsupported sections')
    if args.only and existing.directory is None:
        parser.error('Build a complete bundle before requesting a partial update')
    work = BOT / '.asset-builds' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:6])
    stage = work / 'release'
    stage.mkdir(parents=True)
    lock = BOT / '.asset-builds/update.lock'
    try:
        with lock.open('x') as handle:
            json.dump({'pid': os.getpid(), 'work': work.name}, handle)
    except FileExistsError:
        print('Another asset update is active, or update.lock is stale. Check it before retrying.')
        return 1
    try:
        if existing.error:
            raise ValueError('Existing published assets are corrupt; inspect before replacing')
        tables = json.loads(json.dumps(existing.tables)) if existing.directory else legacy_tables()
        provenance = {'mode': 'existing_reviewed_exports' if args.from_existing else 'fresh_game_extraction',
                      'sections_requested': sorted(selected),
                      'sections_retained': sorted(SECTIONS - selected),
                      'warnings': ['Manual compatibility labels remain explicit policy until verified game table sources are recovered.',
                                   'Abyss loadout display names reuse the existing reviewed loadout_name_tables.json; this updater does not yet regenerate those auxiliary names.']}
        dungeons = existing.directory / 'dungeons' if existing.directory else BOT / 'data/ranking_assets'
        abyss = existing.directory / 'abyss' if existing.directory else BOT / 'data/abyss_assets'
        if not args.from_existing:
            toolkit = args.toolkit.resolve()
            game = args.game_root or Path(read(toolkit / 'config/settings.json')['game_root'])
            table_root = game if (game / 'index1').exists() else game / 'Package/HD/oversea'
            game = table_root.parents[2] if (game / 'index1').exists() else game
            python = toolkit / 'runtime/python/python.exe'
            if not python.exists():
                python = Path(sys.executable)
            sys.path.insert(0, str(toolkit / 'app'))
            from update_game_data import fingerprints
            before = installed_fingerprints(game, table_root, fingerprints)
            extractor_before = extractor_fingerprints(toolkit)
            provenance['source_sha256'] = before
            provenance['extractor_sha256'] = extractor_before
            previous = existing.manifest.get('provenance', {})
            provenance['input_changes'] = {
                'game_files': input_changes(previous.get('source_sha256'), before),
                'extractor_files': input_changes(previous.get('extractor_sha256'), extractor_before),
            }
            unchanged = before == previous.get('source_sha256')
            if (unchanged and not args.force and not args.include_avatars and selected == SECTIONS
                    and extractor_before == previous.get('extractor_sha256')
                    and previous.get('ranking_coverage', {}).get('method') == RANKING_SOURCE
                    and existing.tables.get('pvp_ranks') and existing.tables.get('market_goods')):
                report_path = existing.directory / 'report.html'
                print('Installed sources and extractor files match the published release.')
                print(f'Last verified review: {report_path}')
                if args.open_report and hasattr(os, 'startfile'):
                    try:
                        os.startfile(str(report_path))
                    except OSError as exc:
                        print('Could not open the review: ' + str(exc))
                return 0
            if selected & {'affixes', 'equipment', 'items', 'dungeons'}:
                run_step(python, toolkit / 'app/update_game_data.py', ['--game-root', game], work, 'game-data')
                run = pointer(toolkit / 'output/game_data')
                coverage = read(run / 'coverage.json')
                provenance['warnings'].extend(coverage.get('warnings', []))
                metadata = work / 'equipment_metadata.json'
                if 'equipment' in selected:
                    run_step(python, BOT / 'activity/maintenance/extract_equipment_metadata.py',
                             ['--game-root', table_root, '--details', run / 'equipment/all_equipment_item_details.csv', '--output', metadata], work, 'equipment-metadata')
                apply_exports(tables, run, metadata, selected)
                if 'dungeons' in selected:
                    ranks = coverage['dungeon_table_coverage']['rank']
                    board_count = coverage['counts']['leaderboards']
                    if (ranks['binding']['table'] != 'rank_data' or ranks['exported_trial_boards'] != board_count
                            or ranks['name_index_crosscheck']['matching_board_count'] != board_count):
                        raise ValueError('Toolkit did not provide a complete named ranking export')
                    cards = work / 'cards'
                    run_step(python, HERE / 'refresh_dungeon_cards.py',
                             ['--toolkit', toolkit, '--game-root', game, '--output', cards], work, 'dungeon-cards')
                    dungeons = work / 'dungeons'
                    run_step(python, HERE / 'import_dungeon_assets.py', ['--game-data-run', run, '--destination', dungeons, '--cards-root', cards], work, 'dungeon-import')
                    card_export = read(cards / 'bindings.json')
                    imported = read(pointer(dungeons) / 'manifest.json')['boards']
                    if len(imported) != board_count:
                        raise ValueError('Imported board count differs from verified ranking tables')
                    provenance['ranking_coverage'] = {
                        'method': RANKING_SOURCE, 'boards': ranks,
                        'activity_card_sources': card_export['sources'],
                        'card_references_without_board': missing_card_boards(card_export['rows'], imported),
                    }
            if 'abyss' in selected:
                run_step(python, toolkit / 'tools/extract_abyss_trials.py', ['--game-root', table_root, '--artwork'], work, 'abyss-extract')
                abyss = work / 'abyss'
                run_step(python, HERE / 'import_abyss_assets.py', ['--source', toolkit / 'output/game_data/abyss_trials', '--destination', abyss], work, 'abyss-import')
            if 'constants' in selected:
                extracted = work / 'api_constants.json'
                run_step(python, HERE / 'extract_api_constants.py',
                         ['--toolkit', toolkit, '--game-root', table_root, '--output', extracted], work, 'api-constants')
                tables['api_constants'] = read(extracted)
                tables['unmigrated_policy'] = [value for value in tables.get('unmigrated_policy', []) if value != 'sect names']
            if 'pvp' in selected:
                from extract_pvp_ranks import extract as extract_pvp
                tables['pvp_ranks'] = extract_pvp(toolkit, table_root)
                tables['player_mappings']['pvp_ranks'] = tables['pvp_ranks']
            if 'market' in selected:
                from extract_market_goods import extract as extract_market
                market_output = work / 'market'
                tables['market_goods'] = extract_market(toolkit, game, market_output)
            if 'emotes' in selected:
                from extract_emotes import extract as extract_emotes
                emotes_output = work / 'emotes'
                tables['emotes'] = extract_emotes(toolkit, game, emotes_output)
            if 'martial' in selected:
                from extract_martial import extract
                martial_output = work / 'martial'
                tables['martial'] = extract(toolkit, game, martial_output)
            if args.include_avatars:
                run_step(python, toolkit / 'app/scan_game_avatars.py', ['--game-root', game], work, 'avatars')
                provenance['warnings'].append('Avatar source/render exports refreshed in Toolkit. Unsupported effects remain unresolved; no invented composites. Runtime avatar publishing is not yet enabled.')
            after = installed_fingerprints(game, table_root, fingerprints)
            if before != after:
                raise ValueError('Game changed during update; previous release retained')
            if extractor_before != extractor_fingerprints(toolkit):
                raise ValueError('Extractor files changed during the update; run it again')
            if args.only:
                provenance['warnings'].append('Partial update: unselected sections retain their previous release data.')
                provenance['source_sha256'] = {}  # Never call mixed-source data wholly current.
        elif args.only:
            raise ValueError('--only is for extraction; use a full --from-existing bootstrap')
        provenance['unresolved_artwork'] = add_rankings(stage, dungeons, abyss, tables)
        if 'martial' in selected and not args.from_existing:
            from extract_martial import copy_export
            copy_export(martial_output, stage / 'martial')
        elif existing.directory and (existing.directory / 'martial').exists():
            shutil.copytree(existing.directory / 'martial', stage / 'martial', dirs_exist_ok=True)
        if 'market' in selected and not args.from_existing:
            from extract_market_goods import copy_export as copy_market
            copy_market(market_output, stage / 'market')
        elif existing.directory and (existing.directory / 'market').exists():
            shutil.copytree(existing.directory / 'market', stage / 'market', dirs_exist_ok=True)
        if 'emotes' in selected and not args.from_existing:
            from extract_emotes import copy_export as copy_emotes
            copy_emotes(emotes_output, stage / 'emotes')
        elif existing.directory and (existing.directory / 'emotes').exists():
            shutil.copytree(existing.directory / 'emotes', stage / 'emotes', dirs_exist_ok=True)
        if tables.get('emotes'):
            provenance['unresolved_artwork'].extend({'section': 'emotes', 'id': k, 'reason': r.get('reason', 'Missing artwork')}
                for k, r in tables['emotes']['entries'].items() if r['status'] in ('unresolved', 'static_preview'))
        write(work / 'changes-before-validation.json', __import__('build_game_bundle').changes(existing.tables, tables))
        # Staging records all removals; accepting them is a separate apply action.
        report = finish(stage, tables, provenance, existing.tables, True, existing.directory)
        prepare_review(work, baseline_sha256)
        print('Staged for review. Active assets have NOT changed.', flush=True)
        print('Review report: ' + str(stage / 'report.html'), flush=True)
        removals = any(row['removed'] for row in report['review']['summary'].values())
        print('After review, run: "Apply WWM Asset Update.cmd" ' + work.name + (' --allow-removals' if removals else ''))
        if removals:
            print('This candidate includes removals; --allow-removals explicitly accepts them when applying.')
        print('Unresolved artwork: ' + str(len(report['unresolved_artwork'])))
        if args.open_report and hasattr(os, 'startfile'):
            try:
                os.startfile(str(stage / 'report.html'))
            except OSError as exc:
                print('Could not open the browser; use the report path above: ' + str(exc))
        return 0
    except Exception as exc:
        write(work / 'failure.json', {'status': 'failed_previous_release_retained', 'error': str(exc)})
        print('Update failed; previous release retained: ' + str(exc), file=sys.stderr)
        print('Logs and staged outputs: ' + str(work), file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
