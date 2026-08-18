"""Isolated request scratch transactions with locked root commits."""
from __future__ import annotations

import contextvars
import errno
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from .base import (
    InspectionError,
    _absolute_path,
    _open_real_directory,
    validate_logical_path,
)
from ._scratch_root import (
    ScratchLimits,
    ScratchRootLease,
    ScratchUsage,
    _raise_if_over,
    _remove_tree,
    _same_object,
    _scan_descriptor,
    acquire_root_lease,
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
)
_COPY_CHUNK_BYTES = 1024 * 1024
_current_transaction: contextvars.ContextVar[ScratchTransaction | None] = (
    contextvars.ContextVar("artifact_scratch_transaction", default=None)
)


def _after_fork_child() -> None:
    _current_transaction.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def scratch_bytes(scratch_dir: str, maximum: int) -> int:
    """Return bounded regular-file bytes under a stable scratch root."""
    descriptor = _open_real_directory(scratch_dir, "scratch root")
    try:
        usage = _scan_descriptor(
            descriptor,
            ScratchLimits(
                max_bytes=maximum,
                max_entries=100_000,
                max_depth=64,
            ),
        )
        return usage.bytes
    finally:
        os.close(descriptor)


def _open_relative_parent(
    root_descriptor: int,
    parts: tuple[str, ...],
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            child = os.open(
                part,
                _DIRECTORY_FLAGS,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stable_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise InspectionError("rendered output could not be committed")
        view = view[written:]


def _copy_stable_file(
    source: int,
    destination: int,
    before: os.stat_result,
) -> None:
    copied = 0
    while copied < before.st_size:
        amount = min(_COPY_CHUNK_BYTES, before.st_size - copied)
        chunk = os.pread(source, amount, copied)
        if not chunk:
            raise InspectionError("rendered output changed during commit")
        _write_all(destination, chunk)
        copied += len(chunk)
    after = os.fstat(source)
    if (
        copied != before.st_size
        or _stable_file_identity(before) != _stable_file_identity(after)
    ):
        raise InspectionError("rendered output changed during commit")
    if os.fstat(destination).st_size != copied:
        raise InspectionError("rendered output commit has an invalid size")


@dataclass
class ScratchFile:
    logical_path: str
    descriptor: int

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


class ScratchTransaction:
    """One root-locked request running in an isolated outer/work tree."""

    def __init__(
        self,
        scratch_dir: str,
        *,
        max_bytes: int,
        max_entries: int,
        max_depth: int,
    ) -> None:
        self.root_path = _absolute_path(scratch_dir, "scratch root")
        self.requested_limits = ScratchLimits(
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_depth=max_depth,
        )
        self.limits = self.requested_limits
        self.root_descriptor = -1
        self.outer_parent_descriptor = -1
        self.outer_descriptor = -1
        self.descriptor = -1
        self.outer_name = ""
        self.outer_identity: os.stat_result | None = None
        self._lease: ScratchRootLease | None = None
        self._committed_name = ""
        self._committed_identity: os.stat_result | None = None
        self._token: contextvars.Token[ScratchTransaction | None] | None = None

    @property
    def proc_path(self) -> str:
        if self.descriptor < 0:
            raise InspectionError("scratch transaction is not active")
        return f"/proc/self/fd/{self.descriptor}"

    def _create_outer(self) -> None:
        temporary = _absolute_path(
            tempfile.gettempdir(),
            "trusted temporary root",
        )
        common = os.path.commonpath((temporary, self.root_path))
        if common == self.root_path:
            raise InspectionError(
                "trusted temporary root must not overlap scratch root"
            )
        self.outer_parent_descriptor = _open_real_directory(
            temporary,
            "trusted temporary root",
        )
        for _attempt in range(16):
            name = f".skillopt-artifact-{secrets.token_hex(12)}"
            try:
                os.mkdir(
                    name,
                    0o700,
                    dir_fd=self.outer_parent_descriptor,
                )
            except FileExistsError:
                continue
            self.outer_name = name
            break
        else:
            raise InspectionError("could not allocate scratch transaction")
        self.outer_descriptor = os.open(
            self.outer_name,
            _DIRECTORY_FLAGS,
            dir_fd=self.outer_parent_descriptor,
        )
        self.outer_identity = os.fstat(self.outer_descriptor)
        os.mkdir("work", 0o700, dir_fd=self.outer_descriptor)
        self.descriptor = os.open(
            "work",
            _DIRECTORY_FLAGS,
            dir_fd=self.outer_descriptor,
        )

    def __enter__(self) -> ScratchTransaction:
        try:
            self._lease = acquire_root_lease(
                self.root_path,
                self.requested_limits,
            )
            self.root_descriptor = self._lease.descriptor
            root_usage = self._lease.usage
            self.limits = ScratchLimits(
                max_bytes=self.requested_limits.max_bytes - root_usage.bytes,
                max_entries=(
                    self.requested_limits.max_entries - root_usage.entries
                ),
                max_depth=self.requested_limits.max_depth,
            )
            if self.limits.max_bytes < 0 or self.limits.max_entries < 1:
                raise InspectionError("scratch budget has no request capacity")
            self._create_outer()
            self.check()
            self._token = _current_transaction.set(self)
            return self
        except BaseException:
            self._close_failed_entry()
            raise

    def _remove_committed(self) -> None:
        if not self._committed_name:
            return
        if self._committed_identity is None:
            raise InspectionError("committed output identity is unavailable")
        descriptor = os.open(
            self._committed_name,
            _DIRECTORY_FLAGS,
            dir_fd=self.root_descriptor,
        )
        try:
            if not _same_object(
                self._committed_identity,
                os.fstat(descriptor),
            ):
                raise InspectionError("committed output changed before rollback")
            _remove_tree(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(
            self._committed_name,
            dir_fd=self.root_descriptor,
            follow_symlinks=False,
        )
        if not _same_object(self._committed_identity, current):
            raise InspectionError("committed output changed before rollback")
        os.rmdir(self._committed_name, dir_fd=self.root_descriptor)
        self._committed_name = ""
        self._committed_identity = None

    def _destroy_outer(self) -> None:
        pending: BaseException | None = None
        if self.descriptor >= 0:
            try:
                os.close(self.descriptor)
            except BaseException as exc:
                pending = exc
            self.descriptor = -1
        try:
            if self.outer_descriptor >= 0:
                try:
                    _remove_tree(self.outer_descriptor)
                except BaseException as exc:
                    pending = pending or exc
                try:
                    if (
                        self.outer_identity is None
                        or not _same_object(
                            self.outer_identity,
                            os.fstat(self.outer_descriptor),
                        )
                    ):
                        raise InspectionError(
                            "scratch transaction outer changed during cleanup"
                        )
                    if self.outer_name and self.outer_parent_descriptor >= 0:
                        current = os.stat(
                            self.outer_name,
                            dir_fd=self.outer_parent_descriptor,
                            follow_symlinks=False,
                        )
                        if not _same_object(self.outer_identity, current):
                            raise InspectionError(
                                "scratch transaction outer changed during cleanup"
                            )
                        os.rmdir(
                            self.outer_name,
                            dir_fd=self.outer_parent_descriptor,
                        )
                except BaseException as exc:
                    pending = pending or exc
            elif self.outer_name and self.outer_parent_descriptor >= 0:
                os.rmdir(
                    self.outer_name,
                    dir_fd=self.outer_parent_descriptor,
                )
        except BaseException as exc:
            pending = pending or exc
        finally:
            if self.outer_descriptor >= 0:
                try:
                    os.close(self.outer_descriptor)
                except BaseException as exc:
                    pending = pending or exc
                self.outer_descriptor = -1
            self.outer_name = ""
            self.outer_identity = None
            if self.outer_parent_descriptor >= 0:
                try:
                    os.close(self.outer_parent_descriptor)
                except BaseException as exc:
                    pending = pending or exc
                self.outer_parent_descriptor = -1
        if pending is not None:
            raise pending

    def _close_failed_entry(self) -> None:
        try:
            try:
                self._destroy_outer()
            finally:
                if self._committed_name and self.root_descriptor >= 0:
                    self._remove_committed()
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            self.root_descriptor = -1

    def check(self) -> ScratchUsage:
        if (
            self.root_descriptor < 0
            or self.outer_descriptor < 0
        ):
            raise InspectionError("scratch transaction is not active")
        root = _scan_descriptor(
            self.root_descriptor,
            self.requested_limits,
        )
        outer = _scan_descriptor(
            self.outer_descriptor,
            self.requested_limits,
        )
        combined = root.plus(outer)
        _raise_if_over(combined, self.requested_limits)
        return combined

    def remaining_bytes(self) -> int:
        return self.requested_limits.max_bytes - self.check().bytes

    def validate_root_identity(self) -> None:
        if self._lease is None:
            raise InspectionError("scratch transaction is not active")
        self._lease.validate_identity()

    def _output_parts(self, path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or not path:
            raise InspectionError("renderer returned an invalid path")
        prefix = self.proc_path + "/"
        if path.startswith(prefix):
            return validate_logical_path(path[len(prefix):])
        if os.path.isabs(path):
            if os.path.normpath(path) != path:
                raise InspectionError("derived path must be normalized")
            raise InspectionError("derived path is outside scratch transaction")
        return validate_logical_path(path)

    def open_output(self, path: str) -> ScratchFile:
        parts = self._output_parts(path)
        parent = _open_relative_parent(self.descriptor, parts[:-1])
        try:
            descriptor = os.open(
                parts[-1],
                _FILE_FLAGS,
                dir_fd=parent,
            )
        except OSError as exc:
            raise InspectionError(
                f"rendered output could not be opened: {exc}"
            ) from exc
        finally:
            os.close(parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise InspectionError("rendered output must be a regular file")
        return ScratchFile("/".join(parts), descriptor)

    def open_outputs(self, paths: list[str]) -> list[ScratchFile]:
        if len(set(paths)) != len(paths):
            raise InspectionError("renderer returned duplicate paths")
        opened: list[ScratchFile] = []
        try:
            for path in paths:
                opened.append(self.open_output(path))
            return opened
        except BaseException:
            for output in opened:
                output.close()
            raise

    def commit_outputs(self, paths: list[str]) -> list[str]:
        if self._committed_name:
            raise InspectionError("scratch transaction already committed outputs")
        self.validate_root_identity()
        self.check()
        outputs = self.open_outputs(paths)
        if not outputs:
            return []
        output_name = f".skillopt-output-{secrets.token_hex(12)}"
        output_descriptor = -1
        output_identity: os.stat_result | None = None
        output_created = False
        try:
            os.mkdir(output_name, 0o700, dir_fd=self.root_descriptor)
            output_created = True
            output_descriptor = os.open(
                output_name,
                _DIRECTORY_FLAGS,
                dir_fd=self.root_descriptor,
            )
            output_identity = os.fstat(output_descriptor)
            self.check()
            committed: list[str] = []
            for index, output in enumerate(outputs):
                parts = PurePosixPath(output.logical_path).parts
                parent = _open_relative_parent(
                    self.descriptor,
                    parts[:-1],
                )
                destination = -1
                try:
                    named = os.stat(
                        parts[-1],
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                    source = os.fstat(output.descriptor)
                    if not _same_object(named, source):
                        raise InspectionError(
                            "rendered output changed before commit"
                        )
                    destination_name = f"{index:03d}-{parts[-1]}"
                    renamed = True
                    try:
                        os.rename(
                            parts[-1],
                            destination_name,
                            src_dir_fd=parent,
                            dst_dir_fd=output_descriptor,
                        )
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise InspectionError(
                                "rendered output could not be committed: "
                                f"{exc}"
                            ) from exc
                        renamed = False
                        usage = self.check().plus(
                            ScratchUsage(
                                bytes=source.st_size,
                                entries=1,
                                depth=2,
                            )
                        )
                        _raise_if_over(usage, self.requested_limits)
                        destination = os.open(
                            destination_name,
                            _CREATE_FLAGS,
                            0o600,
                            dir_fd=output_descriptor,
                        )
                        _copy_stable_file(
                            output.descriptor,
                            destination,
                            source,
                        )
                        os.close(destination)
                        destination = -1
                        current = os.stat(
                            parts[-1],
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                        if not _same_object(source, current):
                            raise InspectionError(
                                "rendered output changed during commit"
                            )
                        os.unlink(parts[-1], dir_fd=parent)
                    committed_info = os.stat(
                        destination_name,
                        dir_fd=output_descriptor,
                        follow_symlinks=False,
                    )
                    current_source = os.fstat(output.descriptor)
                    if (
                        not stat.S_ISREG(committed_info.st_mode)
                        or committed_info.st_size != source.st_size
                        or current_source.st_size != source.st_size
                        or current_source.st_mtime_ns != source.st_mtime_ns
                        or (
                            renamed
                            and not _same_object(
                                committed_info,
                                current_source,
                            )
                        )
                    ):
                        raise InspectionError(
                            "rendered output changed during commit"
                        )
                finally:
                    if destination >= 0:
                        os.close(destination)
                    os.close(parent)
                committed.append(
                    os.path.join(
                        self.root_path,
                        output_name,
                        destination_name,
                    )
                )
                self.check()
            self.validate_root_identity()
            self.check()
            self._committed_name = output_name
            self._committed_identity = output_identity
            return committed
        except BaseException:
            if output_descriptor >= 0:
                _remove_tree(output_descriptor)
            if output_created:
                if output_descriptor >= 0:
                    current = os.fstat(output_descriptor)
                    if (
                        output_identity is None
                        or not _same_object(output_identity, current)
                    ):
                        raise InspectionError(
                            "committed output changed during cleanup"
                        )
                os.rmdir(output_name, dir_fd=self.root_descriptor)
            raise
        finally:
            if output_descriptor >= 0:
                os.close(output_descriptor)
            for output in outputs:
                output.close()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._token is not None:
            _current_transaction.reset(self._token)
            self._token = None
        pending: BaseException | None = None
        if exc_type is None:
            try:
                self.check()
                self.validate_root_identity()
            except BaseException as check_error:
                pending = check_error
        try:
            self._destroy_outer()
            if exc_type is None and pending is None:
                try:
                    self.validate_root_identity()
                    _scan_descriptor(
                        self.root_descriptor,
                        self.requested_limits,
                    )
                except BaseException as check_error:
                    pending = check_error
        except BaseException as cleanup_error:
            pending = pending or cleanup_error
        try:
            if (exc_type is not None or pending is not None) and (
                self._committed_name
            ):
                self._remove_committed()
        except BaseException as rollback_error:
            pending = pending or rollback_error
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            self.root_descriptor = -1
        if pending is not None:
            raise pending
        return False


def current_scratch_transaction() -> ScratchTransaction | None:
    """Return the active transaction for safe_run supervision."""
    return _current_transaction.get()


def scratch_transaction(
    scratch_dir: str,
    *,
    max_bytes: int,
    max_entries: int,
    max_depth: int,
) -> ScratchTransaction:
    return ScratchTransaction(
        scratch_dir,
        max_bytes=max_bytes,
        max_entries=max_entries,
        max_depth=max_depth,
    )


def enforce_scratch_budget(
    scratch_dir: str,
    maximum: int,
) -> ScratchTransaction:
    """Compatibility wrapper for byte-only callers."""
    return scratch_transaction(
        scratch_dir,
        max_bytes=maximum,
        max_entries=100_000,
        max_depth=64,
    )
