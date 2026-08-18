"""Scratch-root accounting and process-safe exclusive locking."""
from __future__ import annotations

import fcntl
import os
import stat
import threading
from dataclasses import dataclass

from .base import InspectionError, _open_real_directory

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_PATH_DIRECTORY_FLAGS = (
    getattr(os, "O_PATH", os.O_RDONLY)
    | os.O_DIRECTORY
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True)
class ScratchLimits:
    max_bytes: int
    max_entries: int
    max_depth: int


@dataclass(frozen=True)
class ScratchUsage:
    bytes: int = 0
    entries: int = 0
    depth: int = 0

    def plus(self, other: ScratchUsage) -> ScratchUsage:
        return ScratchUsage(
            bytes=self.bytes + other.bytes,
            entries=self.entries + other.entries,
            depth=max(self.depth, other.depth),
        )


@dataclass
class _RemovalFrame:
    descriptor: int
    names: list[str]
    parent_descriptor: int | None = None
    name: str = ""
    identity: os.stat_result | None = None
    owned: bool = False
    index: int = 0


class _OwnedThreadLock:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.owner: tuple[int, int] | None = None

    def acquire(self) -> None:
        owner = (os.getpid(), threading.get_ident())
        with _thread_locks_guard:
            if self.owner == owner:
                raise InspectionError(
                    "nested scratch transaction for the same root is forbidden"
                )
        self.lock.acquire()
        with _thread_locks_guard:
            self.owner = owner

    def release(self) -> None:
        with _thread_locks_guard:
            self.owner = None
        self.lock.release()


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, _OwnedThreadLock] = {}
_active_root_fds: set[int] = set()
_registry_pid = os.getpid()


def _after_fork_child() -> None:
    global _active_root_fds
    global _registry_pid
    global _thread_locks
    global _thread_locks_guard

    for descriptor in _active_root_fds:
        try:
            os.close(descriptor)
        except OSError:
            pass
    _active_root_fds = set()
    _thread_locks_guard = threading.Lock()
    _thread_locks = {}
    _registry_pid = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


@dataclass
class ScratchRootLease:
    path: str
    descriptor: int
    identity: os.stat_result
    limits: ScratchLimits
    usage: ScratchUsage
    thread_lock: _OwnedThreadLock
    owner_pid: int
    active: bool = True

    def validate_identity(self) -> None:
        current = _open_real_directory(self.path, "scratch root")
        try:
            if not _same_object(self.identity, os.fstat(current)):
                raise InspectionError(
                    "scratch root changed during artifact operation"
                )
        finally:
            os.close(current)

    def release(self) -> None:
        if not self.active:
            return
        self.active = False
        if os.getpid() != self.owner_pid:
            return
        _active_root_fds.discard(self.descriptor)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self.descriptor)
            finally:
                self.descriptor = -1
                self.thread_lock.release()


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _raise_if_over(usage: ScratchUsage, limits: ScratchLimits) -> None:
    if usage.bytes > limits.max_bytes:
        raise InspectionError(
            "scratch byte budget exceeded: "
            f"{usage.bytes} > {limits.max_bytes}"
        )
    if usage.entries > limits.max_entries:
        raise InspectionError(
            "scratch entry budget exceeded: "
            f"{usage.entries} > {limits.max_entries}"
        )
    if usage.depth > limits.max_depth:
        raise InspectionError(
            "scratch depth budget exceeded: "
            f"{usage.depth} > {limits.max_depth}"
        )


def _scan_tree(
    descriptor: int,
    limits: ScratchLimits,
    *,
    depth: int = 0,
    parts: tuple[str, ...] = (),
) -> ScratchUsage:
    with os.scandir(descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    usage = ScratchUsage()
    for name in names:
        child_parts = (*parts, name)
        before = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        entry_depth = depth + 1
        child_usage = ScratchUsage(entries=1, depth=entry_depth)
        if stat.S_ISLNK(before.st_mode):
            raise InspectionError(
                f"scratch contains a symlink: {'/'.join(child_parts)}"
            )
        if stat.S_ISDIR(before.st_mode):
            try:
                child = os.open(
                    name,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise InspectionError(
                    "scratch directory could not be scanned: "
                    f"{'/'.join(child_parts)}"
                ) from exc
            try:
                opened = os.fstat(child)
                if not _same_object(before, opened):
                    raise InspectionError(
                        "scratch directory changed while scanning: "
                        f"{'/'.join(child_parts)}"
                    )
                child_usage = child_usage.plus(
                    _scan_tree(
                        child,
                        limits,
                        depth=entry_depth,
                        parts=child_parts,
                    )
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode):
            child = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if not _same_object(before, opened):
                    raise InspectionError(
                        "scratch file changed while scanning: "
                        f"{'/'.join(child_parts)}"
                    )
                child_usage = ScratchUsage(
                    bytes=opened.st_size,
                    entries=1,
                    depth=entry_depth,
                )
            finally:
                os.close(child)
        else:
            raise InspectionError(
                "scratch contains a non-regular entry: "
                f"{'/'.join(child_parts)}"
            )
        usage = usage.plus(child_usage)
        _raise_if_over(usage, limits)
    return usage


def _scan_descriptor(
    descriptor: int,
    limits: ScratchLimits,
) -> ScratchUsage:
    scan = os.open(".", _DIRECTORY_FLAGS, dir_fd=descriptor)
    try:
        usage = _scan_tree(scan, limits)
    finally:
        os.close(scan)
    _raise_if_over(usage, limits)
    return usage


def _open_owned_directory_for_cleanup(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> tuple[int, os.stat_result]:
    path_descriptor = os.open(
        name,
        _PATH_DIRECTORY_FLAGS,
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(path_descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_object(expected, opened)
        ):
            raise InspectionError(
                "scratch directory changed during cleanup"
            )
        if opened.st_uid != os.geteuid():
            raise InspectionError(
                "scratch directory owner changed during cleanup"
            )
        owner_mode = stat.S_IMODE(opened.st_mode) | stat.S_IRWXU
        if owner_mode != stat.S_IMODE(opened.st_mode):
            os.chmod(f"/proc/self/fd/{path_descriptor}", owner_mode)
        child = os.open(
            ".",
            _DIRECTORY_FLAGS,
            dir_fd=path_descriptor,
        )
        child_info = os.fstat(child)
        if not _same_object(opened, child_info):
            os.close(child)
            raise InspectionError(
                "scratch directory changed during cleanup"
            )
        return child, child_info
    finally:
        os.close(path_descriptor)


def _remove_tree(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        root_names = sorted(entry.name for entry in entries)
    stack = [_RemovalFrame(descriptor, root_names)]
    pending: BaseException | None = None
    try:
        while stack:
            frame = stack[-1]
            if frame.index < len(frame.names):
                name = frame.names[frame.index]
                frame.index += 1
                child = -1
                try:
                    info = os.stat(
                        name,
                        dir_fd=frame.descriptor,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(
                        info.st_mode
                    ):
                        child, opened = _open_owned_directory_for_cleanup(
                            frame.descriptor,
                            name,
                            info,
                        )
                        with os.scandir(child) as entries:
                            child_names = sorted(
                                entry.name for entry in entries
                            )
                        stack.append(
                            _RemovalFrame(
                                child,
                                child_names,
                                parent_descriptor=frame.descriptor,
                                name=name,
                                identity=opened,
                                owned=True,
                            )
                        )
                        child = -1
                    else:
                        os.unlink(name, dir_fd=frame.descriptor)
                except BaseException as exc:
                    pending = pending or exc
                finally:
                    if child >= 0:
                        os.close(child)
                continue
            if frame.parent_descriptor is not None:
                try:
                    current = os.stat(
                        frame.name,
                        dir_fd=frame.parent_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        frame.identity is None
                        or not _same_object(frame.identity, current)
                    ):
                        raise InspectionError(
                            "scratch directory changed during cleanup"
                        )
                    os.rmdir(
                        frame.name,
                        dir_fd=frame.parent_descriptor,
                    )
                except BaseException as exc:
                    pending = pending or exc
                finally:
                    os.close(frame.descriptor)
                    frame.owned = False
            stack.pop()
    except BaseException:
        for frame in reversed(stack):
            if frame.owned:
                try:
                    os.close(frame.descriptor)
                except OSError:
                    pass
                frame.owned = False
        raise
    if pending is not None:
        raise pending


def _thread_lock(path: str) -> _OwnedThreadLock:
    global _registry_pid
    if _registry_pid != os.getpid():
        _after_fork_child()
    with _thread_locks_guard:
        return _thread_locks.setdefault(path, _OwnedThreadLock())


def acquire_root_lease(
    path: str,
    limits: ScratchLimits,
) -> ScratchRootLease:
    thread_lock = _thread_lock(path)
    thread_acquired = False
    descriptor = -1
    file_locked = False
    try:
        thread_lock.acquire()
        thread_acquired = True
        descriptor = _open_real_directory(path, "scratch root")
        _active_root_fds.add(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        file_locked = True
        identity = os.fstat(descriptor)
        current = _open_real_directory(path, "scratch root")
        try:
            if not _same_object(identity, os.fstat(current)):
                raise InspectionError(
                    "scratch root changed before artifact operation"
                )
        finally:
            os.close(current)
        usage = _scan_descriptor(descriptor, limits)
        _active_root_fds.add(descriptor)
        return ScratchRootLease(
            path=path,
            descriptor=descriptor,
            identity=identity,
            limits=limits,
            usage=usage,
            thread_lock=thread_lock,
            owner_pid=os.getpid(),
        )
    except BaseException:
        if descriptor >= 0:
            _active_root_fds.discard(descriptor)
            try:
                if file_locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        if thread_acquired:
            thread_lock.release()
        raise
