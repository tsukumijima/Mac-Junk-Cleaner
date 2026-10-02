"""
Chromium の保存構造と開発ツールの保存先から、掃除候補を発見する。
"""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from mac_junk_cleaner.filesystem import LocalFilesystem


CHROMIUM_CACHES = {
    'Cache',
    'GPUCache',
    'Code Cache',
    'CodeCache',
    'ScriptCache',
    'DawnCache',
    'DawnGraphiteCache',
    'DawnWebGPUCache',
    'ShaderCache',
    'GrShaderCache',
    'GraphiteDawnCache',
    'GPUPersistentCache',
    'Media Cache',
    'component_crx_cache',
    'extensions_crx_cache',
}
PRESERVE = {
    'IndexedDB',
    'Local Storage',
    'Session Storage',
    'databases',
    'User',
    'History',
    'Backups',
    'extensions',
    'node_modules',
    'Local Extension Settings',
    'Session Extension Settings',
    'vm_bundles',
    'File System',
    'Storage',
    '.git',
    '.venv',
    'venv',
    'models',
    'model_assets',
    'blob_storage',
}
PROJECT_PRUNE = PRESERVE | {
    'Pods',
    'vendor',
    'build',
    'target',
    '.dart_tool',
    '.next',
    '.nuxt',
    '.svelte-kit',
    '.turbo',
    'dist',
    'dist_electron',
    '.yarn',
}
PACKAGE_PATHS = {
    'npm': ['.npm/_cacache', '.npm/_npx', '.npm/_logs'],
    'Yarn': ['Library/Caches/Yarn', '.cache/yarn', '.yarn/berry/cache'],
    'pnpm': ['Library/pnpm/store', '.local/share/pnpm/store', '.pnpm-store'],
    'pip': ['Library/Caches/pip', '.cache/pip'],
    'Poetry': [
        'Library/Caches/pypoetry/cache',
        'Library/Caches/pypoetry/artifacts',
        '.cache/pypoetry/cache',
        '.cache/pypoetry/artifacts',
    ],
    'Dart / Flutter pub': ['.pub-cache/hosted', '.pub-cache/hosted-hashes', '.pub-cache/git', '.pub-cache/_temp'],
    'Flutter SDK': ['Develop/Flutter/bin/cache'],
    'Cargo': [
        '.cargo/registry/cache',
        '.cargo/registry/src',
        '.cargo/registry/index',
        '.cargo/git/db',
        '.cargo/git/checkouts',
    ],
    'rustup': ['.rustup/downloads', '.rustup/tmp'],
    'Gradle': ['.gradle/caches', '.gradle/wrapper/dists'],
    'Ivy / sbt': ['.ivy2/cache', '.sbt/boot', 'Library/Caches/Coursier', '.cache/coursier'],
    'Go': ['Library/Caches/go-build', 'go/pkg/mod'],
    'Homebrew': ['Library/Caches/Homebrew'],
    'Bun': ['.bun/install/cache'],
    'Composer': ['Library/Caches/composer', '.cache/composer', '.composer/cache'],
    'NuGet': ['.nuget/packages', '.local/share/NuGet/v3-cache'],
    'ccache / sccache': [
        'Library/Caches/ccache',
        'Library/Caches/Mozilla.sccache',
        '.cache/ccache',
        '.cache/sccache',
        '.ccache',
    ],
    'CocoaPods': ['Library/Caches/CocoaPods'],
    'SwiftPM': ['Library/Caches/org.swift.swiftpm', '.swiftpm/cache'],
    'Xcode': ['Library/Developer/Xcode/DerivedData'],
    'Xcode Simulator': ['Library/Developer/CoreSimulator/Caches'],
    'Google Updater': ['Library/Application Support/Google/GoogleUpdater/crx_cache'],
    'pre-commit': ['.cache/pre-commit'],
    'Tombi': ['.cache/tombi/https'],
}


@dataclass
class Candidate:
    """
    削除候補の場所、選択条件、利用者が判断するための根拠を保持する。
    """

    path: Path
    category: str
    label: str
    reason: str
    impact: str
    optional: bool = False
    allocated_bytes: int = 0
    logical_bytes: int = 0
    age_days: int | None = None
    is_running: bool = False
    errors: list[str] | None = None


class Discovery:
    """
    アプリの固定リストを使わずに、保存構造からキャッシュを探す。
    """

    def __init__(self, home: Path, stale_days: int = 183, shared_root: Path | None = Path('/Library')) -> None:
        """
        探索するホームと、古いプロジェクトを見つける日数を指定する。

        Args:
            home (Path): 対象ユーザーのローカルホーム
            stale_days (int): 更新が止まったプロジェクトの候補判定に使う日数
            shared_root (Path | None): 共有領域の Library (隔離した検証では None)
        """

        self.home = home
        self.shared_root = shared_root
        self.filesystem = LocalFilesystem(home, additional_roots=(shared_root,) if shared_root is not None else ())
        self.stale_days = stale_days
        self.candidates: list[Candidate] = []
        self.errors: list[str] = []

    def add(
        self,
        path: Path,
        category: str,
        label: str,
        reason: str,
        impact: str,
        optional: bool = False,
        age_days: int | None = None,
    ) -> None:
        """
        存在するローカルディレクトリを掃除候補へ加える。

        Args:
            path (Path): 候補ディレクトリ
            category (str): CLI で選択できる分類
            label (str): アプリまたは開発ツールの表示名
            reason (str): 削除候補にした根拠
            impact (str): 再取得やオフライン利用への影響
            optional (bool): 追加指定で選択する候補かどうか
            age_days (int | None): ソースと Git の更新から推定した経過日数
        """

        if self.filesystem.is_local(path) is True and path.is_dir() is True:
            self.candidates.append(Candidate(path, category, label, reason, impact, optional, age_days=age_days))

    def discover_apps(self) -> None:
        """
        アプリキャッシュと Chromium プロファイル内の再取得可能な領域を探す。
        """

        cache_roots = [self.home / 'Library/Caches']
        if self.shared_root is not None:
            cache_roots.append(self.shared_root / 'Caches')
            self.add(
                self.shared_root / 'Application Support/Logi/LogiOptionsPlus/cache',
                'apps',
                'Logi Options+',
                '更新プログラムのキャッシュ領域',
                '更新ファイルの再取得が必要。管理者権限が必要',
            )
        for caches in cache_roots:
            if self.filesystem.is_local(caches) is True:
                try:
                    for path in caches.iterdir():
                        self.add(
                            path,
                            'apps',
                            path.name,
                            'アプリのキャッシュ領域',
                            '再取得・再生成が必要。大きなキャッシュにはモデルやオフライン素材も含まれ得る',
                        )
                except OSError as ex:
                    self.errors.append(f'{caches}: {type(ex).__name__}: {ex}')
        self.add(
            self.home / 'Library/Application Support/LarkInternational/update',
            'apps',
            'Lark 更新ファイル',
            'ダウンロードした更新 ZIP と展開済みアプリの保存領域',
            '保留中の更新を適用するには再ダウンロードが必要。実行中のアプリと設定は別領域',
        )
        roots = [self.home / 'Library/Application Support']
        containers = self.home / 'Library/Containers'
        if self.filesystem.is_local(containers) is True:
            for container in containers.iterdir():
                self.add(
                    container / 'Data/Library/Caches',
                    'apps',
                    container.name,
                    'サンドボックス内のアプリキャッシュ',
                    'アプリによる再取得・再生成が必要',
                )
        for root in roots:
            if self.filesystem.is_local(root) is False:
                continue
            pending = [(root, 0, False)]
            while pending:
                current, depth, is_chromium = pending.pop()
                try:
                    entries = list(current.iterdir())
                    names = {entry.name for entry in entries}
                    # 通常の作業ファイルに付いた Cache という名前だけでは候補にしない
                    markers = names & {
                        'Preferences',
                        'Local State',
                        'Cookies',
                        'Network',
                        'Service Worker',
                        'GPUCache',
                        'Code Cache',
                    }
                    is_chromium = is_chromium or len(markers) >= 2
                    label = current.relative_to(root).parts[0] if current != root else root.name
                    for path in entries:
                        if self.filesystem.is_local(path) is False or path.is_dir() is False or path.name in PRESERVE:
                            continue
                        if is_chromium is True and path.name in CHROMIUM_CACHES:
                            self.add(
                                path,
                                'apps',
                                label,
                                'Chromium の保存構造と既知のキャッシュ名が一致',
                                '描画・HTTP キャッシュを再生成。起動中は再作成や解放の遅延があり得る',
                            )
                        elif is_chromium is True and path.name == 'CacheStorage':
                            self.add(
                                path,
                                'cache-storage',
                                label,
                                'Chromium の CacheStorage',
                                'サイトのオフライン用データを消去。起動中のサイトは再読み込みが必要になる場合がある',
                                True,
                            )
                        elif is_chromium is True and path.name in {'CachedData', 'CachedExtensionVSIXs'}:
                            self.add(
                                path,
                                'apps',
                                label,
                                'エディタのコンパイル・拡張配布キャッシュ',
                                '再コンパイル・拡張パッケージの再取得が必要',
                            )
                        elif depth < 5:
                            pending.append((path, depth + 1, is_chromium))
                except OSError as ex:
                    self.errors.append(f'{current}: {type(ex).__name__}: {ex}')

    def discover_packages(self) -> None:
        """
        パッケージの標準保存先と、導入済みツールが返す保存先を確認する。
        uv は並行実行を調整する公式の clean コマンドでキャッシュ全体を削除する。
        """

        for label, paths in PACKAGE_PATHS.items():
            for relative in paths:
                self.add(
                    self.home / relative,
                    'packages',
                    label,
                    '開発ツールの標準キャッシュ保存先',
                    'パッケージの再取得や再ビルドが必要。オフライン環境では復元できない場合がある',
                )
        uv = self.tool_command('uv', ['cache', 'dir'])
        if uv is not None:
            result = subprocess.run(uv, cwd=self.home, text=True, capture_output=True)
            if result.returncode == 0 and result.stdout.strip().startswith('/'):
                self.add(
                    Path(result.stdout.strip()),
                    'uv',
                    'uv',
                    'uv が返したキャッシュ保存先',
                    '公式の uv cache clean で全キャッシュを削除。再取得・再ビルドが必要。symlink モードで作成した環境は再インストールが必要',
                )
        queries = [
            ('npm', 'npm', ['config', 'get', 'cache'], ['_cacache', '_npx', '_logs']),
            ('Yarn', 'yarn', ['cache', 'dir'], ['']),
            ('Yarn', 'yarn', ['config', 'get', 'cacheFolder'], ['']),
            ('pnpm', 'pnpm', ['store', 'path'], ['']),
            ('pip', 'pip', ['cache', 'dir'], ['']),
            ('Homebrew', 'brew', ['--cache'], ['']),
            ('Go', 'go', ['env', 'GOCACHE', 'GOMODCACHE'], ['']),
        ]
        for label, tool, arguments, suffixes in queries:
            command = self.tool_command(tool, arguments)
            if command is None:
                continue
            try:
                result = subprocess.run(
                    command,
                    cwd=self.home,
                    text=True,
                    capture_output=True,
                    timeout=20,
                    env={**os.environ, 'HOMEBREW_NO_AUTO_UPDATE': '1'},
                )
                if result.returncode != 0:
                    self.errors.append(f'保存先の照会に失敗: {tool}: {result.stderr.strip() or result.stdout.strip()}')
                    continue
                for line in result.stdout.splitlines():
                    if line.startswith('/') is True:
                        for suffix in suffixes:
                            self.add(
                                Path(line.strip()) / suffix,
                                'packages',
                                label,
                                f'{tool} が返したキャッシュ保存先',
                                'パッケージの再取得・再ビルドが必要',
                            )
            except (OSError, subprocess.TimeoutExpired) as ex:
                self.errors.append(f'{tool}: {type(ex).__name__}: {ex}')
        for variable, label, suffixes in [
            ('CARGO_HOME', 'Cargo', ['registry/cache', 'registry/src', 'registry/index', 'git/db', 'git/checkouts']),
            ('RUSTUP_HOME', 'rustup', ['downloads', 'tmp']),
            ('PUB_CACHE', 'Dart / Flutter pub', ['hosted', 'hosted-hashes', 'git', '_temp']),
            ('GRADLE_USER_HOME', 'Gradle', ['caches', 'wrapper/dists']),
        ]:
            if value := os.environ.get(variable):
                for suffix in suffixes:
                    self.add(
                        Path(value).expanduser() / suffix,
                        'packages',
                        label,
                        f'{variable} の指定先',
                        'パッケージの再取得・再ビルドが必要',
                    )

    def tool_command(self, tool: str, arguments: list[str]) -> list[str] | None:
        """
        sudo で起動していても、元のユーザーのツールと保存先を使う。

        Args:
            tool (str): パッケージ管理ツールの実行名
            arguments (list[str]): ツールへ渡す引数

        Returns:
            list[str] | None: 実行コマンド (ツールがなければ None)
        """

        executable = shutil.which(tool)
        if executable is None:
            for directory in (
                self.home / '.local/bin',
                self.home / '.pyenv/shims',
                self.home / '.nodenv/shims',
                Path('/opt/homebrew/bin'),
                Path('/usr/local/bin'),
            ):
                path = directory / tool
                if path.is_file() is True and os.access(path, os.X_OK) is True:
                    executable = str(path)
                    break
        if executable is None:
            return None
        prefix = []
        if os.geteuid() == 0 and (sudo_user := os.environ.get('SUDO_USER')):
            prefix = ['/usr/bin/sudo', '-u', sudo_user, '-H']
        return [*prefix, executable, *arguments]

    def discover_projects(self, roots: list[Path]) -> None:
        """
        ビルドキャッシュと、更新が止まったプロジェクトの依存パッケージを探す。

        Args:
            roots (list[Path]): 探索するローカルの開発フォルダ
        """

        for root in roots:
            if self.filesystem.is_local(root) is False:
                self.errors.append(f'開発フォルダは対象外、または存在しません: {root}')
                continue
            pending = [root]
            while pending:
                project = pending.pop()
                try:
                    entries = list(project.iterdir())
                    names = {entry.name for entry in entries}
                    generated = []
                    if 'package.json' in names:
                        generated.extend(
                            ['.next', '.nuxt', '.svelte-kit', '.turbo', 'node_modules/.cache', '.yarn/cache']
                        )
                    if 'pubspec.yaml' in names:
                        generated.extend(['build', '.dart_tool'])
                    if 'Cargo.toml' in names:
                        generated.append('target')
                    if names & {'build.gradle', 'build.gradle.kts'}:
                        generated.extend(['build', '.gradle'])
                    for relative in generated:
                        path = project / relative
                        if self.filesystem.is_local(path) is True and self.is_untracked(path, project) is True:
                            self.add(
                                path,
                                'builds',
                                project.name,
                                'プロジェクト形式に対応する生成物で Git の追跡対象外',
                                '次のビルドや実行で再生成が必要',
                            )
                    dependencies = project / 'node_modules'
                    if 'package.json' in names and self.filesystem.is_local(dependencies) is True:
                        source = self.filesystem.measure(project, PROJECT_PRUNE)
                        latest = source.latest_mtime
                        # Git の取得・切り替えも利用の手掛かりとして更新日時へ含める
                        for relative in ('.git/HEAD', '.git/logs/HEAD', '.git/index'):
                            metadata = project / relative
                            if self.filesystem.is_local(metadata) is True:
                                latest = max(latest, metadata.stat().st_mtime)
                        age_days = int((time.time() - latest) / 86400)
                        lockfiles = names & {
                            'package-lock.json',
                            'npm-shrinkwrap.json',
                            'yarn.lock',
                            'pnpm-lock.yaml',
                            'bun.lock',
                            'bun.lockb',
                        }
                        if (
                            age_days >= self.stale_days
                            and lockfiles
                            and source.errors == []
                            and self.is_untracked(dependencies, project) is True
                        ):
                            self.add(
                                dependencies,
                                'stale-node-modules',
                                project.name,
                                f'ソース・Git メタデータの最新更新から {age_days} 日、ロックファイルあり。最終利用日は推定',
                                '依存パッケージの再インストールが必要。手修正した node_modules は失われる',
                                True,
                                age_days,
                            )
                    for path in entries:
                        if (
                            path.name not in PROJECT_PRUNE
                            and self.filesystem.is_local(path) is True
                            and path.is_dir() is True
                        ):
                            pending.append(path)
                except OSError as ex:
                    self.errors.append(f'{project}: {type(ex).__name__}: {ex}')

    def is_untracked(self, path: Path, project: Path) -> bool:
        """
        プロジェクトの生成物に Git の追跡ファイルが含まれるか確認する。

        Args:
            path (Path): 生成物の候補
            project (Path): Git の作業ディレクトリ

        Returns:
            bool: Git 管理外、または Git の追跡ファイルがない場合は True
        """

        command = self.tool_command('git', ['-C', str(project), 'rev-parse', '--show-toplevel'])
        if command is None:
            self.errors.append(f'Git を確認できないため保持: {path}')
            return False
        repository = subprocess.run(command, capture_output=True, text=True)
        if repository.returncode != 0:
            # リポジトリ内部のエラーと、Git を使っていないプロジェクトを区別
            if any(
                (parent / '.git').exists() for parent in (project, *project.parents) if parent.is_relative_to(self.home)
            ):
                self.errors.append(f'Git の作業状態を確認できません: {project}: {repository.stderr.strip()}')
                return False
            return True
        result = subprocess.run([*command[:-2], 'ls-files', '--', str(path)], capture_output=True, text=True)
        if result.returncode != 0 or result.stdout.strip():
            self.errors.append(f'Git の追跡対象、または確認失敗のため保持: {path}')
            return False
        return True

    def finish(self) -> list[Candidate]:
        """
        重複する候補を統合し、容量と関連アプリの起動状態を表示用に取得する。

        Returns:
            list[Candidate]: 親子の重複を除いた、容量の大きい順の候補
        """

        processes = subprocess.run(
            ['/bin/ps', '-axo', 'command='], capture_output=True, text=True, check=True
        ).stdout.casefold()
        selected: list[Candidate] = []
        # パッケージ名で表示できる候補を先に採用し、Library/Caches の重複を統合
        for candidate in sorted(
            self.candidates,
            key=lambda item: (len(item.path.parts), item.category not in {'packages', 'uv'}, str(item.path)),
        ):
            if any(
                candidate.path.is_relative_to(previous.path)
                and (previous.optional is False or candidate.optional is True)
                for previous in selected
            ):
                continue
            measured = self.filesystem.measure(candidate.path)
            candidate.allocated_bytes = measured.allocated_bytes
            candidate.logical_bytes = measured.logical_bytes
            candidate.errors = measured.errors + [
                f'容量測定から除外したリンク・マウント: {path}' for path in measured.excluded
            ]
            candidate.is_running = candidate.label.casefold() in processes
            if candidate.allocated_bytes > 0 or candidate.errors:
                selected.append(candidate)
        return sorted(selected, key=lambda item: item.allocated_bytes, reverse=True)
