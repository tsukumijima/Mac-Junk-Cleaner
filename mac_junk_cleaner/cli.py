"""
容量分析・候補表示・明示的な削除を行う CLI を提供する。
"""

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from mac_junk_cleaner.discovery import PROJECT_PRUNE, Discovery
from mac_junk_cleaner.filesystem import LocalFilesystem, Measurement


def main() -> None:
    """
    通常はプレビューし、--apply と確認入力がある場合だけ削除を実行する。
    """

    parser = argparse.ArgumentParser(description='ローカルの Mac を分析し、再生成できるキャッシュを掃除します。')
    subparsers = parser.add_subparsers(dest='command', required=True)
    analyze = subparsers.add_parser('analyze', help='ディスク使用量を分析（削除なし）')
    analyze.add_argument('--root', type=Path, help='分析するローカル領域（標準は Data ボリューム）')
    analyze.add_argument('--depth', type=int, default=4, help='ディレクトリ別容量を残す階層数（標準: 4）')
    analyze.add_argument('--json', type=Path, help='結果の JSON 保存先')
    for name in ('scan', 'clean'):
        command = subparsers.add_parser(name, help='削除候補の測定' if name == 'scan' else '削除候補の表示と掃除')
        command.add_argument(
            '--category',
            action='append',
            choices=['apps', 'packages', 'builds', 'cache-storage', 'stale-node-modules', 'uv'],
            help='候補を分類で絞る（複数指定可）',
        )
        command.add_argument('--path', action='append', type=Path, help='表示した候補の絶対パスで絞る（複数指定可）')
        command.add_argument(
            '--project-root', action='append', type=Path, help='開発フォルダ（標準: ~/Develop 。指定時は置き換え）'
        )
        command.add_argument('--stale-days', type=int, default=183, help='古い node_modules の候補判定に使う日数')
        command.add_argument('--include-cache-storage', action='store_true', help='サイトのオフライン用データも選択')
        command.add_argument(
            '--include-stale-node-modules', action='store_true', help='更新が止まったプロジェクトの依存パッケージも選択'
        )
        command.add_argument('--json', type=Path, help='候補と判定根拠の JSON 保存先')
        command.add_argument('--verbose', action='store_true', help='小さな候補を含む全件のパスと影響を表示')
        if name == 'clean':
            mode = command.add_mutually_exclusive_group()
            mode.add_argument('--apply', action='store_true', help='候補を表示した後、確認して削除')
            mode.add_argument('--dry-run', action='store_true', help='削除予定の全パスを表示（削除なし）')
            command.add_argument('--manifest', type=Path, help='--dry-run の全パスを JSON Lines で保存')
            command.add_argument('--yes', action='store_true', help='表示した選択対象の削除確認を省略')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('この CLI は macOS 用です。')
    home = Path.home()
    if os.geteuid() == 0 and (sudo_user := os.environ.get('SUDO_USER')):
        home = Path(pwd.getpwnam(sudo_user).pw_dir)
    if args.command == 'analyze':
        if args.depth < 1 or args.depth > 8:
            parser.error('--depth は1〜8で指定してください。')
        root = (args.root or Path('/System/Volumes/Data')).expanduser().absolute()
        filesystem = LocalFilesystem(root)
        print(f'分析中: {root}（マウント・リンクを除外）', flush=True)
        measured = filesystem.measure(root, detail_depth=args.depth)
        disk = shutil.disk_usage(home)
        payload = {
            'measured_at': datetime.now().astimezone().isoformat(),
            'root': str(root),
            'disk': disk._asdict(),
            **asdict(measured),
            'note': 'ディレクトリの容量は親子で重複します。割当容量の合計は APFS の共有ブロック・スナップショット・読めない領域により実際の使用量と異なります。',
        }
        for path, size in sorted(measured.directories.items(), key=lambda item: item[1], reverse=True)[:30]:
            print(f'{size / 1e9:8.2f} GB  {path}')
        print(
            f'読めた割当容量: {measured.allocated_bytes / 1e9:.2f}GB / 論理容量: {measured.logical_bytes / 1e9:.2f}GB'
        )
        print(f'測定できなかった場所: {len(measured.errors)}件 / 除外: {len(measured.excluded)}件')
        if args.json is not None:
            args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        raise SystemExit(1 if measured.errors else 0)

    if args.stale_days < 1:
        parser.error('--stale-days は1以上で指定してください。')
    if args.command == 'clean' and args.manifest is not None and args.dry_run is False:
        parser.error('--manifest は --dry-run と一緒に指定してください。')
    discovery = Discovery(home, args.stale_days)
    categories = set(args.category or ['apps', 'packages', 'builds', 'cache-storage', 'stale-node-modules', 'uv'])
    if categories & {'apps', 'cache-storage'}:
        print('アプリのキャッシュを探索中...', flush=True)
        discovery.discover_apps()
    if categories & {'packages', 'uv'}:
        print('パッケージキャッシュを探索中...', flush=True)
        discovery.discover_packages()
    if categories & {'builds', 'stale-node-modules'}:
        print('開発フォルダを探索中...', flush=True)
        roots = [path.expanduser().absolute() for path in args.project_root or [home / 'Develop']]
        discovery.discover_projects(roots)
    exact_paths = {path.expanduser().absolute() for path in args.path or []}
    discovery.candidates = [
        candidate
        for candidate in discovery.candidates
        if candidate.category in categories and (not exact_paths or candidate.path in exact_paths)
    ]
    candidates = discovery.finish()
    discovered_paths = {candidate.path for candidate in discovery.candidates}
    if exact_paths - discovered_paths:
        parser.error(f'今回の探索で候補と確認できないパスです: {exact_paths - discovered_paths}')
    selected = []
    category_sizes: Counter[str] = Counter()
    for candidate in candidates:
        category_sizes[candidate.category] += candidate.allocated_bytes
    for category, size in category_sizes.items():
        print(f'分類合計: {category}: {size / 1e9:.2f}GB')
    enabled_optional = set()
    if args.include_cache_storage is True:
        enabled_optional.add('cache-storage')
    if args.include_stale_node_modules is True:
        enabled_optional.add('stale-node-modules')
    selection_paths = sorted(
        [
            candidate.path
            for candidate in candidates
            if candidate.optional is False or candidate.category in enabled_optional
        ],
        key=lambda path: len(path.parts),
    )
    unique_paths: list[Path] = []
    for path in selection_paths:
        if any(path.is_relative_to(parent) for parent in unique_paths) is False:
            unique_paths.append(path)
    for index, candidate in enumerate(candidates):
        is_selected = candidate.path in unique_paths
        if is_selected is True:
            selected.append(candidate)
        if index >= 20 and args.verbose is False:
            continue
        state = (
            '選択'
            if is_selected
            else (
                '親の選択対象に含む'
                if any(candidate.path.is_relative_to(parent) for parent in unique_paths)
                else '追加指定で選択'
            )
        )
        if candidate.is_running is True:
            state += '・関連プロセスあり（名前による推定）'
        print(
            f'{candidate.allocated_bytes / 1e9:8.2f} GB  [{state}] [{candidate.category}] {candidate.label}\n  {candidate.path}\n  根拠: {candidate.reason}\n  影響: {candidate.impact}'
        )
        for error in candidate.errors or []:
            print(f'  注意: {error}', file=sys.stderr)
    if len(candidates) > 20 and args.verbose is False:
        print(
            f'容量上位20件を表示しました。残り {len(candidates) - 20} 件の詳細は --verbose または --json で確認できます。'
        )
    total = sum(candidate.allocated_bytes for candidate in selected)
    print(f'選択された候補: {len(selected)}件 / 割当容量: {total / 1e9:.2f}GB')
    print(
        '削除の実行時は起動中のアプリも対象です。再作成・開いているファイル・APFS 共有ブロックにより、実際の空き増加は候補容量と異なります。'
    )
    for error in discovery.errors:
        print(f'探索時の注意: {error}', file=sys.stderr)
    if args.json is not None:
        payload = {
            'measured_at': datetime.now().astimezone().isoformat(),
            'disk': shutil.disk_usage(home)._asdict(),
            'selected_allocated_bytes': total,
            'candidates': [
                {**asdict(candidate), 'path': str(candidate.path), 'selected': candidate in selected}
                for candidate in candidates
            ],
            'errors': discovery.errors,
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    if args.command == 'clean' and args.dry_run is True:
        manifest = args.manifest.open('w') if args.manifest is not None else None
        planned_count = 0
        failed = False
        try:
            for candidate in selected:
                try:
                    for path, kind, allocated_bytes in discovery.filesystem.removal_entries(candidate.path):
                        planned_count += 1
                        entry = {
                            'path': str(path),
                            'kind': kind,
                            'allocated_bytes': allocated_bytes,
                            'candidate': str(candidate.path),
                            'category': candidate.category,
                        }
                        if manifest is not None:
                            manifest.write(json.dumps(entry, ensure_ascii=False) + '\n')
                        else:
                            print(f'削除予定: [{kind}] {path}')
                except (OSError, ValueError) as ex:
                    failed = True
                    print(f'削除予定の確認失敗: {candidate.path}: {ex}', file=sys.stderr)
        finally:
            if manifest is not None:
                manifest.close()
        print(f'dry-run 完了: 削除予定 {planned_count} 件 / 実削除なし')
        if args.manifest is not None:
            print(f'削除予定の全パス: {args.manifest}')
        raise SystemExit(1 if failed else 0)
    if args.command != 'clean' or args.apply is False:
        print('プレビュー完了。clean --apply で選択された候補を削除できます。')
        return
    if args.yes is False:
        if sys.stdin.isatty() is False:
            parser.error('削除には対話確認が必要です。確認済みの対象に --yes を付けてください。')
        if input('上記の選択された対象を削除します。続行するには DELETE と入力してください: ') != 'DELETE':
            print('キャンセルしました。')
            return
    free_before = shutil.disk_usage(home).free
    failures = []
    # 確認入力の間に変わった保存先も、現在のマウント一覧で再検証
    filesystem = LocalFilesystem(home, additional_roots=(Path('/Library'),))
    for candidate in selected:
        if filesystem.is_local(candidate.path) is True and os.access(candidate.path, os.W_OK) is False:
            if os.geteuid() == 0:
                failures.append(
                    f'sudo 起動でもアクセスできないため保持: {candidate.path}。アクセス権と macOS のプライバシー設定を確認してください。'
                )
            else:
                failures.append(
                    f'書き込み権限がないため保持: {candidate.path}。管理者権限が必要な対象は sudo で実行してください。'
                )
            continue
        if candidate.category in {'builds', 'stale-node-modules'}:
            project = (
                candidate.path.parent.parent
                if candidate.path.name == '.cache' or candidate.path.parent.name == '.yarn'
                else candidate.path.parent
            )
            if discovery.is_untracked(candidate.path, project) is False:
                failures.append(f'Git の追跡対象、または確認失敗のため保持: {candidate.path}')
                continue
            if candidate.category == 'stale-node-modules':
                source = filesystem.measure(project, PROJECT_PRUNE)
                latest = source.latest_mtime
                for relative in ('.git/HEAD', '.git/logs/HEAD', '.git/index'):
                    metadata = project / relative
                    if filesystem.is_local(metadata) is True:
                        latest = max(latest, metadata.stat().st_mtime)
                has_lockfile = any(
                    (project / name).is_file()
                    for name in (
                        'package-lock.json',
                        'npm-shrinkwrap.json',
                        'yarn.lock',
                        'pnpm-lock.yaml',
                        'bun.lock',
                        'bun.lockb',
                    )
                )
                if source.errors or (time.time() - latest) / 86400 < args.stale_days or has_lockfile is False:
                    failures.append(f'古いプロジェクトの条件を再確認できないため保持: {project}')
                    continue
        before = filesystem.measure(candidate.path)
        mounted = [path for path in before.excluded if Path(path).is_symlink() is False]
        if before.errors or mounted:
            failures.append(f'全体を検証できないため保持: {candidate.path}: {before.errors + mounted}')
            continue
        if candidate.category == 'uv':
            command = discovery.tool_command('uv', ['cache', 'clean', '--cache-dir', str(candidate.path)])
            if command is None:
                failures.append('uv の実行ファイルを確認できないため保持しました。')
                continue
            result = subprocess.run(command, cwd=home, text=True, capture_output=True)
            if result.returncode != 0:
                failures.append(f'uv cache clean に失敗: {result.stderr}')
                continue
        else:
            failures.extend(filesystem.remove_contents(candidate.path))
        after = filesystem.measure(candidate.path) if candidate.path.exists() is True else Measurement()
        failures.extend(after.errors)
        print(
            f'処理後: {candidate.path} / 割当容量の減少: {(before.allocated_bytes - after.allocated_bytes) / 1e9:.2f}GB / 残り: {after.allocated_bytes / 1e9:.2f}GB',
            flush=True,
        )
    free_after = shutil.disk_usage(home).free
    print(
        f'実際の空き容量: {free_before / 1e9:.2f}GB → {free_after / 1e9:.2f}GB（増加: {(free_after - free_before) / 1e9:.2f}GB）'
    )
    for failure in failures:
        print(f'処理時の注意: {failure}', file=sys.stderr)
    raise SystemExit(1 if failures else 0)
