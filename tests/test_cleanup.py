"""
掃除対象の取り違えと、境界を越える削除を一時データで検証する。
"""

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mac_junk_cleaner.cli import main
from mac_junk_cleaner.discovery import Discovery
from mac_junk_cleaner.filesystem import LocalFilesystem


class CleanupTests(unittest.TestCase):
    """
    未知のアプリ、古い依存パッケージ、リンク・マウント・Git の境界を確認する。
    """

    def test_dry_run_manifest_preserves_files(self) -> None:
        """
        dry-run の一覧と削除対象を照合し、ファイルとリンク先を保持する。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            cache = home / 'Library/Caches/FixtureApp'
            cache.mkdir(parents=True)
            source = home / 'source'
            source.write_text('original')
            cached = cache / 'cached'
            cached.write_text('cached')
            link = cache / 'link'
            link.symlink_to(source)
            manifest = home / 'files.jsonl'
            discovery = Discovery(home, shared_root=None)
            arguments = ['mac-junk-cleaner', 'clean', '--category', 'apps', '--dry-run', '--manifest', str(manifest)]
            with (
                patch('pathlib.Path.home', return_value=home),
                patch('sys.argv', arguments),
                patch('mac_junk_cleaner.cli.Discovery', return_value=discovery),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as completed:
                    main()
            self.assertEqual(completed.exception.code, 0)
            entries = [json.loads(line) for line in manifest.read_text().splitlines()]
            self.assertEqual({entry['path'] for entry in entries}, {str(cached), str(link)})
            self.assertEqual({entry['kind'] for entry in entries}, {'file', 'symlink'})
            self.assertEqual(source.read_text(), 'original')
            self.assertEqual(cached.read_text(), 'cached')
            self.assertTrue(link.is_symlink())

    def test_shared_library_uses_isolated_fixture(self) -> None:
        """
        ホーム外の共有キャッシュを測定・削除し、設定ファイルを保持する。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / 'home'
            home.mkdir()
            shared = root / 'Library'
            cache = shared / 'Application Support/Logi/LogiOptionsPlus/cache'
            cache.mkdir(parents=True)
            (cache / 'cached').write_bytes(b'cached' * 1000)
            settings = cache.parent / 'settings.json'
            settings.write_text('{}')
            discovery = Discovery(home, shared_root=shared)
            discovery.discover_apps()
            self.assertIn(cache, {candidate.path for candidate in discovery.finish()})
            self.assertEqual(discovery.filesystem.remove_contents(cache), [])
            self.assertTrue(settings.exists())

    def test_sudo_queries_original_user(self) -> None:
        """
        sudo 起動時のツール照会を元のユーザーへ渡す。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            tool = home / '.local/bin/uv'
            tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\n')
            tool.chmod(0o755)
            discovery = Discovery(home, shared_root=None)
            with (
                patch('os.geteuid', return_value=0),
                patch.dict(os.environ, {'SUDO_USER': 'fixture-user'}),
                patch('shutil.which', return_value=None),
            ):
                command = discovery.tool_command('uv', ['cache', 'dir'])
            self.assertEqual(command, ['/usr/bin/sudo', '-u', 'fixture-user', '-H', str(tool), 'cache', 'dir'])

    def test_stalled_temporary_directory_does_not_block_rest(self) -> None:
        """
        読み取りが停止した一時領域の残りを除外し、ほかの領域の測定を続ける。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            temporary = home / 'Application/Data/tmp'
            (temporary / 'stalled').mkdir(parents=True)
            (temporary / 'other').mkdir()
            (home / 'normal').write_bytes(b'content' * 1000)
            original_scandir = os.scandir

            def scandir(path: str | os.PathLike[str]):
                if Path(path).parent == temporary:
                    raise TimeoutError('読み取り停止')
                return original_scandir(path)

            with patch('os.scandir', side_effect=scandir):
                measured = LocalFilesystem(home).measure(home)
            self.assertEqual(measured.files, 1)
            self.assertTrue(any('TimeoutError' in error for error in measured.errors))
            self.assertTrue(any(str(temporary) in excluded for excluded in measured.excluded))

    def test_unknown_chromium_preserves_site_data(self) -> None:
        """
        アプリ名を登録せずにキャッシュを発見し、履歴やサイトデータを保持する。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            profile = home / 'Library/Application Support/UnknownApp'
            for name in ('GPUCache', 'Code Cache', 'IndexedDB', 'Service Worker/CacheStorage', 'Local Storage'):
                path = profile / name
                path.mkdir(parents=True)
                (path / 'data').write_bytes(b'content' * 1000)
            (profile / 'Preferences').write_text('{}')
            ordinary = home / 'Library/Application Support/DocumentApp/Cache'
            ordinary.mkdir(parents=True)
            (ordinary / 'document.txt').write_text('original')
            discovery = Discovery(home, shared_root=None)
            discovery.discover_apps()
            candidates = discovery.finish()
            paths = {candidate.path for candidate in candidates}
            self.assertIn(profile / 'GPUCache', paths)
            self.assertNotIn(ordinary, paths)
            self.assertNotIn(profile / 'IndexedDB', paths)
            storage = next(candidate for candidate in candidates if candidate.category == 'cache-storage')
            self.assertTrue(storage.optional)
            for candidate in candidates:
                if candidate.optional is False:
                    self.assertEqual(discovery.filesystem.remove_contents(candidate.path), [])
            self.assertTrue((profile / 'IndexedDB/data').exists())
            self.assertTrue((profile / 'Local Storage/data').exists())
            self.assertTrue((profile / 'Service Worker/CacheStorage/data').exists())

    def test_mount_and_link_targets_survive(self) -> None:
        """
        マウント先のファイルと、キャッシュ内リンクが指す原資料を保持する。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            cache = home / 'cache'
            cache.mkdir()
            mounted = cache / 'mounted'
            mounted.mkdir()
            (mounted / 'source').write_text('original')
            source = home / 'source'
            source.write_text('original')
            (cache / 'link').symlink_to(source)
            (cache / 'junk').write_text('cached')
            filesystem = LocalFilesystem(home, {mounted})
            self.assertFalse(filesystem.is_local(mounted))
            with self.assertRaises(ValueError):
                list(filesystem.removal_entries(cache))
            failures = filesystem.remove_contents(cache)
            self.assertTrue(failures)
            self.assertTrue((mounted / 'source').exists())
            self.assertTrue(source.exists())
            self.assertFalse((cache / 'link').exists())
            self.assertFalse((cache / 'junk').exists())
            self.assertTrue(cache.exists())
            with self.assertRaises(ValueError):
                LocalFilesystem(mounted, {mounted})

    def test_recreated_cache_directory_is_retained(self) -> None:
        """
        削除中に再生成されたキャッシュを保持し、通常の残量として扱う。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            cache = home / 'cache'
            child = cache / 'child'
            child.mkdir(parents=True)
            (child / 'old').write_text('cached')
            original_rmdir = Path.rmdir

            def rmdir(path: Path) -> None:
                if path == child:
                    (path / 'new').write_text('regenerated')
                original_rmdir(path)

            with patch.object(Path, 'rmdir', rmdir):
                failures = LocalFilesystem(home).remove_contents(cache)
            self.assertEqual(failures, [])
            self.assertFalse((child / 'old').exists())
            self.assertEqual((child / 'new').read_text(), 'regenerated')

    def test_tracked_build_is_preserved(self) -> None:
        """
        生成物に見えるディレクトリでも Git の追跡ファイルがあれば保持する。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            project = home / 'Develop/Project'
            (project / 'target').mkdir(parents=True)
            (project / 'Cargo.toml').write_text('')
            (project / 'target/source').write_text('original')
            subprocess.run(['git', 'init', '-q', str(project)], check=True)
            subprocess.run(['git', '-C', str(project), 'add', 'target/source'], check=True)
            discovery = Discovery(home)
            discovery.discover_projects([home / 'Develop'])
            self.assertNotIn(project / 'target', {candidate.path for candidate in discovery.candidates})

    def test_stale_dependency_requires_lockfile_and_old_sources(self) -> None:
        """
        最近のソース更新とロックファイルなしの依存パッケージを対象外にする。
        """

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / 'Develop'
            old = time.time() - 200 * 86400
            for name in ('OldProject', 'RecentProject', 'NoLockProject'):
                project = root / name
                (project / 'node_modules').mkdir(parents=True)
                (project / 'package.json').write_text('{}')
                (project / 'main.js').write_text('')
                if name != 'NoLockProject':
                    (project / 'package-lock.json').write_text('{}')
                for path in [*project.iterdir(), project]:
                    os.utime(path, (old, old))
                if name == 'RecentProject':
                    os.utime(project / 'main.js', None)
            discovery = Discovery(home)
            discovery.discover_projects([root])
            stale = [candidate for candidate in discovery.candidates if candidate.category == 'stale-node-modules']
            self.assertEqual([candidate.path for candidate in stale], [root / 'OldProject/node_modules'])
            self.assertTrue(stale[0].optional)


if __name__ == '__main__':
    unittest.main()
