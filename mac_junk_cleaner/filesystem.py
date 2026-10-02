"""
マウントとリンクを除外し、ローカルファイルの割当容量を測定する。
"""

import errno
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Measurement:
    """
    読めたファイルの容量と、測定できなかった場所を保持する。
    APFS の共有ブロックやスナップショットは、ファイル単位の容量には含まれない。
    """

    allocated_bytes: int = 0
    logical_bytes: int = 0
    files: int = 0
    latest_mtime: float = 0
    errors: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    directories: dict[str, int] = field(default_factory=dict)
    largest_files: list[tuple[int, str]] = field(default_factory=list)


class LocalFilesystem:
    """
    指定したローカル領域を、別ボリュームやリンクへ入らず探索する。
    マウント一覧を先に取得し、ネットワークドライブへの stat 呼び出しも抑える。
    """

    def __init__(self, root: Path, mounts: set[Path] | None = None, additional_roots: tuple[Path, ...] = ()) -> None:
        """
        探索境界と現在のマウント一覧を取得する。

        Args:
            root (Path): 探索を許すローカル領域
            mounts (set[Path] | None): 測定用に固定するマウント一覧 (省略時は実機から取得)
            additional_roots (tuple[Path, ...]): 同じボリューム内で追加の探索を許す領域
        """

        self.root = Path(os.path.abspath(root.expanduser()))
        self.roots = (self.root, *additional_roots)
        if mounts is None:
            result = subprocess.run(['/sbin/mount'], capture_output=True, text=True, check=True)
            mounts = set()
            for line in result.stdout.splitlines():
                if ' on ' in line and ' (' in line:
                    mounts.add(Path(line.split(' on ', 1)[1].rsplit(' (', 1)[0]))
        self.mounts = mounts
        if any(
            self.root.is_relative_to(mount)
            for mount in mounts
            if mount not in {Path('/'), Path('/System/Volumes/Data')}
        ):
            raise ValueError(f'追加マウント配下の探索は対象外です: {self.root}')
        for ancestor in (self.root, *self.root.parents):
            if ancestor.is_symlink() is True:
                raise ValueError(f'リンクを経由する探索は対象外です: {self.root}')
        self.device = self.root.lstat().st_dev
        if self.root.is_symlink() is True or self.is_local(self.root) is False:
            raise ValueError(f'ローカルの実体ディレクトリを指定してください: {self.root}')

    def is_local(self, path: Path) -> bool:
        """
        境界内の同じボリュームへ、リンクを経由せず到達できるか確認する。

        Args:
            path (Path): 確認する絶対パス

        Returns:
            bool: マウントとリンクを経由しないローカルパスなら True
        """

        boundary = next((root for root in self.roots if path.is_relative_to(root)), None)
        if path.is_absolute() is False or '..' in path.parts or boundary is None:
            return False
        if any(
            mount.is_relative_to(boundary) and path.is_relative_to(mount) for mount in self.mounts if mount != boundary
        ):
            return False
        if 'Volumes' in path.relative_to(boundary).parts:
            return False
        current = boundary
        try:
            for ancestor in boundary.parents:
                if ancestor.is_symlink() is True:
                    return False
            for component in (None, *path.relative_to(boundary).parts):
                if component is not None:
                    current /= component
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) is True or info.st_dev != self.device:
                    return False
            return True
        except OSError:
            return False

    def measure(self, path: Path, prune: set[str] | None = None, detail_depth: int = 0) -> Measurement:
        """
        ファイルの内容を読まず、容量・更新日時・大きなファイルを集計する。

        Args:
            path (Path): 測定するローカルパス
            prune (set[str] | None): 探索から除くディレクトリ名
            detail_depth (int): ディレクトリ別容量を残す階層数

        Returns:
            Measurement: 読めた範囲の割当容量と論理容量、および除外・失敗
        """

        result = Measurement()
        seen: set[tuple[int, int]] = set()
        prune = prune or set()
        if self.is_local(path) is False:
            result.errors.append(f'対象外の場所: {path}')
            return result
        pending = [path]
        blocked_temporary: set[Path] = set()
        previous_handler = signal.getsignal(signal.SIGALRM)
        last_progress = time.monotonic()

        def interrupt_slow_directory(signum: int, frame: object) -> None:
            raise TimeoutError('ディレクトリの読み取りが10秒を超えました')

        signal.signal(signal.SIGALRM, interrupt_slow_directory)
        while pending:
            current = pending.pop()
            if any(current.is_relative_to(blocked) for blocked in blocked_temporary):
                continue
            if detail_depth > 0 and time.monotonic() - last_progress >= 10:
                print(
                    f'分析中: {current} / 測定済み: {result.allocated_bytes / 1e9:.2f}GB', file=sys.stderr, flush=True
                )
                last_progress = time.monotonic()
            try:
                # 一時領域の読み取りが止まった場合も、エラーを記録してほかの領域を測定
                signal.setitimer(signal.ITIMER_REAL, 10)
                info = current.lstat()
                if stat.S_ISDIR(info.st_mode) is True:
                    result.latest_mtime = max(result.latest_mtime, info.st_mtime)
                    with os.scandir(current) as entries:
                        for entry in entries:
                            child = Path(entry.path)
                            # 親の検証済み経路を引き継ぎ、マウントへの stat は実行前に除外
                            if entry.name == 'Volumes' or child in self.mounts or entry.name in prune:
                                result.excluded.append(str(child))
                                continue
                            try:
                                child_info = entry.stat(follow_symlinks=False)
                                if stat.S_ISLNK(child_info.st_mode) is True or child_info.st_dev != self.device:
                                    result.excluded.append(str(child))
                                else:
                                    pending.append(child)
                            except OSError as ex:
                                if isinstance(ex, TimeoutError) is True:
                                    raise
                                result.errors.append(f'{child}: {type(ex).__name__}: {ex}')
                    continue
                if stat.S_ISREG(info.st_mode) is False:
                    continue
                result.latest_mtime = max(result.latest_mtime, info.st_mtime)
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                allocated = info.st_blocks * 512
                result.allocated_bytes += allocated
                result.logical_bytes += info.st_size
                result.files += 1
                if detail_depth > 0:
                    relative = current.relative_to(path)
                    for depth in range(1, min(detail_depth, len(relative.parts) - 1) + 1):
                        key = str(path.joinpath(*relative.parts[:depth]))
                        result.directories[key] = result.directories.get(key, 0) + allocated
                    if info.st_size >= 100_000_000:
                        result.largest_files.append((info.st_size, str(current)))
                        result.largest_files = sorted(result.largest_files, reverse=True)[:100]
            except OSError as ex:
                result.errors.append(f'{current}: {type(ex).__name__}: {ex}')
                # 同じ一時領域の多数の子で停止する場合は、その領域を記録して残りへ進む
                if isinstance(ex, TimeoutError) is True:
                    temporary = next(
                        (
                            parent
                            for parent in (current, *current.parents)
                            if parent.name in {'tmp', 'T', 'TemporaryItems'}
                            and parent != path
                            and parent.is_relative_to(path)
                        ),
                        None,
                    )
                    if temporary is not None:
                        blocked_temporary.add(temporary)
                        result.excluded.append(f'{temporary}（読み取り停止により残りの測定を省略）')
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        return result

    def removal_entries(self, path: Path) -> Iterator[tuple[Path, str, int]]:
        """
        削除予定の全パスを列挙し、リンク先のデータを保持する。

        Args:
            path (Path): 掃除候補のディレクトリ

        Yields:
            tuple[Path, str, int]: 削除予定のパス、種類、割当容量
        """

        if path == self.root or self.is_local(path) is False or path.is_dir() is False:
            raise ValueError(f'削除対象を再検証できません: {path}')
        pending = [path]
        while pending:
            directory = pending.pop()
            if self.is_local(directory) is False:
                raise ValueError(f'探索中に変更されたパスです: {directory}')
            with os.scandir(directory) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    if child in self.mounts or child.name == 'Volumes':
                        raise ValueError(f'マウント先を含む候補です: {child}')
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) is True:
                        yield child, 'symlink', info.st_blocks * 512
                    elif self.is_local(child) is False:
                        raise ValueError(f'リンク・別ボリューム・変更されたパスです: {child}')
                    elif stat.S_ISDIR(info.st_mode) is True:
                        yield child, 'directory', 0
                        pending.append(child)
                    else:
                        yield child, 'file', info.st_blocks * 512

    def remove_contents(self, path: Path) -> list[str]:
        """
        対象ディレクトリを残し、その中のローカルファイルだけを削除する。
        起動中のアプリが参照する親ディレクトリを維持し、リンク先の原資料とマウントを保持する。

        Args:
            path (Path): 削除直前に再検証するキャッシュディレクトリ

        Returns:
            list[str]: 削除できなかったパスとエラー
        """

        failures: list[str] = []
        if path == self.root or self.is_local(path) is False or path.is_dir() is False:
            return [f'削除対象を再検証できません: {path}']

        def remove(directory: Path) -> None:
            try:
                with os.scandir(directory) as entries:
                    children = [Path(entry.path) for entry in entries]
                for child in children:
                    # リンクそのものだけを削除し、リンク先の原資料は保持
                    if self.is_local(directory) is True and child.is_symlink() is True:
                        child.unlink()
                        continue
                    if self.is_local(child) is False:
                        failures.append(f'リンク・別ボリューム・変更されたパスを保持: {child}')
                        continue
                    if child.is_dir() is True:
                        remove(child)
                        try:
                            child.rmdir()
                        except PermissionError:
                            directory.chmod(directory.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
                            child.rmdir()
                        except OSError as ex:
                            # 起動中のアプリが再生成した内容は残し、処理後の残量へ含める
                            if ex.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                                failures.append(f'{child}: {ex}')
                    else:
                        # Go の読み取り専用キャッシュも、対象内の親ディレクトリだけ書き込み可能にする
                        try:
                            child.unlink()
                        except PermissionError:
                            directory.chmod(directory.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
                            child.unlink()
            except OSError as ex:
                failures.append(f'{directory}: {type(ex).__name__}: {ex}')

        remove(path)
        return failures
