"""Shared naming contract for internal Run-output ownership metadata."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import tempfile


OUTPUT_CLAIM_FILENAME = ".omicsclaw-run-claim.json"
OutputClaimIdentity = tuple[int, int]
_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x00000400,
)
_WINDOWS_NAME_SURROGATE_TAG_BIT = 0x20000000


def _is_windows_reparse_point(entry_stat: os.stat_result) -> bool:
    """Return whether one ``lstat`` result is a Windows filesystem alias.

    Junctions and other name-surrogate entries are not reliably reported by
    :func:`Path.is_symlink` on every supported Python/Windows combination.
    ``st_file_attributes`` is the primary signal; the tag bit is retained as a
    fail-closed fallback for name-surrogate reparse tags.
    """

    attributes = int(getattr(entry_stat, "st_file_attributes", 0) or 0)
    reparse_tag = int(getattr(entry_stat, "st_reparse_tag", 0) or 0)
    return bool(
        attributes & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        or reparse_tag & _WINDOWS_NAME_SURROGATE_TAG_BIT
    )


def stat_is_filesystem_alias(entry_stat: os.stat_result) -> bool:
    """Return whether one ``lstat`` result represents a filesystem alias."""

    return stat.S_ISLNK(entry_stat.st_mode) or _is_windows_reparse_point(entry_stat)


def is_filesystem_alias(path: str | Path) -> bool:
    """Return whether one existing entry is a symlink or Windows reparse alias."""

    try:
        return stat_is_filesystem_alias(os.lstat(Path(path)))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def first_filesystem_alias_component(path: str | Path) -> Path | None:
    """Return the first alias in a lexical path without erasing ``..`` evidence.

    Both POSIX symbolic links and Windows reparse-point/name-surrogate aliases
    (including directory junctions) are rejected through this single boundary.
    Unexpected inspection errors propagate so mutating callers can fail closed.
    """

    raw_path = Path(path)
    if raw_path.is_absolute():
        current = Path(raw_path.anchor)
        parts = raw_path.parts[1:]
    else:
        current = Path.cwd()
        parts = raw_path.parts

    for part in parts:
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat_is_filesystem_alias(entry_stat):
            return current
    return None


def is_output_claim_path(path: Path) -> bool:
    """Return whether ``path`` is the internal ownership marker."""
    normalized = str(path).replace("\\", "/")
    return PurePosixPath(normalized).name == OUTPUT_CLAIM_FILENAME


def _file_identity(path: Path) -> OutputClaimIdentity:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino)


def collect_output_claim_identities(
    output_root: Path,
) -> frozenset[OutputClaimIdentity]:
    """Index claim markers without descending filesystem aliases."""

    root = Path(output_root)
    identities: set[OutputClaimIdentity] = set()
    directories = [root]
    while directories:
        directory = directories.pop()
        try:
            if first_filesystem_alias_component(directory) is not None:
                continue
            directory_stat = os.lstat(directory)
            if stat_is_filesystem_alias(directory_stat) or not stat.S_ISDIR(
                directory_stat.st_mode
            ):
                continue
            entries = tuple(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                entry_stat = os.lstat(entry)
            except OSError:
                continue
            if stat_is_filesystem_alias(entry_stat):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                directories.append(entry)
            elif (
                stat.S_ISREG(entry_stat.st_mode) and entry.name == OUTPUT_CLAIM_FILENAME
            ):
                identities.add((entry_stat.st_dev, entry_stat.st_ino))
    return frozenset(identities)


def is_output_claim_artifact(
    path: Path,
    *,
    output_root: Path,
    claim_identities: frozenset[OutputClaimIdentity] | None = None,
) -> bool:
    """Return whether a runtime path is the claim marker or an alias to it.

    ``is_output_claim_path`` deliberately stays a deterministic lexical check
    for manifests and configuration.  Runtime output discovery also has to
    reject symbolic-link and hard-link aliases, so it compares the candidate's
    resolved name and inode with the marker owned by this Run directory.
    """

    candidate = Path(path)
    if is_output_claim_path(candidate):
        return True
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if is_output_claim_path(resolved):
        return True

    try:
        identity = _file_identity(candidate)
    except OSError:
        return False
    identities = (
        collect_output_claim_identities(output_root)
        if claim_identities is None
        else claim_identities
    )
    return identity in identities


def is_scientific_output_file(
    path: Path,
    *,
    output_root: Path,
    claim_identities: frozenset[OutputClaimIdentity] | None = None,
) -> bool:
    """Return whether a runtime artifact is a contained, non-internal file."""

    candidate = Path(path)
    root = Path(output_root)
    # Inspect the caller's path before ``abspath``/``resolve`` normalises
    # parent references.  Otherwise ``alias/../artifact`` can erase the
    # symbolic-link component while still reaching a contained regular file.
    try:
        contains_alias = (
            first_filesystem_alias_component(candidate) is not None
            or first_filesystem_alias_component(root) is not None
        )
    except OSError:
        return False
    if contains_alias:
        return False
    try:
        lexical_candidate = Path(os.path.abspath(candidate))
        lexical_root = Path(os.path.abspath(root))
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError:
        return False
    current = lexical_root
    if is_filesystem_alias(current):
        return False
    for part in relative.parts:
        current = current / part
        if is_filesystem_alias(current):
            return False

    try:
        stat = candidate.stat()
    except OSError:
        return False
    if (
        is_filesystem_alias(candidate)
        or not candidate.is_file()
        or stat.st_nlink != 1
        or is_output_claim_artifact(
            candidate,
            output_root=root,
            claim_identities=claim_identities,
        )
    ):
        return False
    return is_contained_output_path(candidate, output_root=root)


def is_contained_output_path(path: Path, *, output_root: Path) -> bool:
    """Return whether a filesystem entry resolves inside its output tree."""

    try:
        Path(path).resolve(strict=True).relative_to(
            Path(output_root).resolve(strict=True)
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _fsync_directory(path: Path) -> None:
    """Persist a directory-entry update on POSIX filesystems."""

    if os.name == "nt":
        # Python cannot portably open Windows directories for ``fsync``.
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_owned_output_text(
    path: Path,
    *,
    output_root: Path,
    text: str,
    encoding: str = "utf-8",
    label: str = "output file",
) -> Path:
    """Atomically write one backend-owned file without following aliases.

    Existing regular single-link files may be replaced.  Symbolic links,
    hard links, claim aliases, and locations outside ``output_root`` are
    rejected before a temporary file is created in the destination directory.
    The final ``os.replace`` never opens the old destination, so an alias cannot
    redirect the write to its target.
    """

    candidate = Path(path)
    root = Path(output_root)
    if is_filesystem_alias(root):
        raise RuntimeError(
            f"refusing to write {label} through a symbolic-link output root "
            f"or Windows reparse point: {root}"
        )
    root_alias = first_filesystem_alias_component(root)
    if root_alias is not None:
        raise RuntimeError(
            f"refusing to write {label} through a symbolic-link output ancestor "
            f"or Windows reparse point: {root_alias}"
        )

    try:
        lexical_root = Path(os.path.abspath(root))
        lexical_candidate = Path(os.path.abspath(candidate))
        relative_parent = lexical_candidate.parent.relative_to(lexical_root)
    except ValueError as exc:
        raise RuntimeError(
            f"refusing to write {label} outside output root: {candidate}"
        ) from exc

    candidate_parent_alias = first_filesystem_alias_component(candidate.parent)
    if candidate_parent_alias is not None:
        raise RuntimeError(
            f"refusing to write {label} through a symbolic-link output parent "
            f"or Windows reparse point: {candidate_parent_alias}"
        )

    # A final-entry check alone misses an ordinary directory reached through an
    # aliased ancestor such as ``/safe/link/out``.  Walk the absolute ancestor
    # chain without resolving it so that alias remains observable.
    for ancestor in reversed(lexical_root.parents):
        if is_filesystem_alias(ancestor):
            raise RuntimeError(
                f"refusing to write {label} through a symbolic-link output ancestor "
                f"or Windows reparse point: {ancestor}"
            )

    current = lexical_root
    for part in relative_parent.parts:
        current = current / part
        if is_filesystem_alias(current):
            raise RuntimeError(
                f"refusing to write {label} through a symbolic-link output parent "
                f"or Windows reparse point: {current}"
            )

    try:
        candidate.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"refusing to write {label} outside output root: {candidate}"
        ) from exc

    if is_filesystem_alias(candidate) or (
        candidate.exists()
        and not is_scientific_output_file(candidate, output_root=root)
    ):
        raise RuntimeError(f"refusing to replace unowned {label}: {candidate}")

    fd, temporary_name = tempfile.mkstemp(
        dir=candidate.parent,
        prefix=f".{candidate.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, candidate)
        _fsync_directory(candidate.parent)
    finally:
        temporary_path.unlink(missing_ok=True)
    return candidate


def _secure_directory_open_flags() -> int:
    """Return flags required for descriptor-anchored directory traversal."""

    if os.name != "posix" or any(
        not hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise RuntimeError("secure directory handles are unavailable")
    if any(
        function not in os.supports_dir_fd
        for function in (os.link, os.mkdir, os.stat, os.unlink)
    ):
        raise RuntimeError("secure directory handles are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_absolute_plain_directory(path: Path, *, flags: int) -> int:
    """Open an absolute directory by walking every component without aliases."""

    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or not absolute.anchor:
        raise RuntimeError(f"owned output root is not absolute: {path}")
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            if part in {"", ".", ".."}:
                raise RuntimeError("owned output root has an unsafe path component")
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def atomic_write_owned_output_text_beneath(
    output_root: Path,
    *,
    relative_parent: tuple[str, ...],
    filename: str,
    text: str,
    encoding: str = "utf-8",
    label: str = "output file",
) -> Path:
    """Create directories and atomically write beneath a held root handle.

    Unlike the path-oriented compatibility helper above, this primitive keeps
    ``O_NOFOLLOW`` directory descriptors from the filesystem root through the
    destination directory.  A rename or symlink substitution after validation
    therefore cannot redirect the temporary write or final ``replace``.

    Platforms without the required descriptor-relative APIs fail closed.  The
    returned path is presentation metadata; mutation is performed exclusively
    through the proven destination descriptor.
    """

    if (
        not isinstance(filename, str)
        or filename in {"", ".", ".."}
        or Path(filename).name != filename
        or "\x00" in filename
    ):
        raise RuntimeError(f"unsafe {label} filename")
    for part in relative_parent:
        if (
            not isinstance(part, str)
            or part in {"", ".", ".."}
            or Path(part).name != part
            or "\x00" in part
        ):
            raise RuntimeError(f"unsafe {label} directory component")

    if os.name == "nt":
        from omicsclaw.common.windows_directory_guard import (
            hold_windows_plain_directory_authority,
        )

        with hold_windows_plain_directory_authority(
            output_root,
            *relative_parent,
        ) as guarded_parent:
            parent = Path(guarded_parent)
            return atomic_write_owned_output_text(
                parent / filename,
                output_root=parent,
                text=text,
                encoding=encoding,
                label=label,
            )

    root = Path(output_root)
    flags = _secure_directory_open_flags()
    destination_fd = -1
    transaction_id = secrets.token_hex(16)
    temp_name = f".{filename}.{transaction_id}.tmp"
    backup_name = f".{filename}.{transaction_id}.bak"
    backup_created = False
    published = False
    rollback_needed = False
    lexical_parent = root.joinpath(*relative_parent)
    try:
        destination_fd = _open_absolute_plain_directory(root, flags=flags)
        for part in relative_parent:
            try:
                next_fd = os.open(part, flags, dir_fd=destination_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=destination_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=destination_fd)
            os.close(destination_fd)
            destination_fd = next_fd

        try:
            destination_stat = os.stat(
                filename,
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and (
            stat_is_filesystem_alias(destination_stat)
            or not stat.S_ISREG(destination_stat.st_mode)
            or destination_stat.st_nlink != 1
        ):
            raise RuntimeError(f"refusing to replace unowned {label}: {filename}")
        if destination_stat is not None:
            os.link(
                filename,
                backup_name,
                src_dir_fd=destination_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            backup_created = True
            backup_stat = os.stat(
                backup_name,
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
            current_destination_stat = os.stat(
                filename,
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
            expected_identity = (destination_stat.st_dev, destination_stat.st_ino)
            if (
                stat_is_filesystem_alias(backup_stat)
                or not stat.S_ISREG(backup_stat.st_mode)
                or (backup_stat.st_dev, backup_stat.st_ino) != expected_identity
                or (current_destination_stat.st_dev, current_destination_stat.st_ino)
                != expected_identity
                or backup_stat.st_nlink != 2
                or current_destination_stat.st_nlink != 2
            ):
                raise RuntimeError(f"{label} changed before replacement")

        temp_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        temp_fd = os.open(temp_name, temp_flags, 0o600, dir_fd=destination_fd)
        try:
            with os.fdopen(temp_fd, "w", encoding=encoding, closefd=True) as handle:
                temp_fd = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)

        temp_stat = os.stat(
            temp_name,
            dir_fd=destination_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(temp_stat.st_mode) or temp_stat.st_nlink != 1:
            raise RuntimeError(f"unsafe temporary {label}")
        os.replace(
            temp_name,
            filename,
            src_dir_fd=destination_fd,
            dst_dir_fd=destination_fd,
        )
        published = True
        rollback_needed = True
        os.fsync(destination_fd)

        verification_fd = _open_absolute_plain_directory(
            lexical_parent,
            flags=flags,
        )
        try:
            held = os.fstat(destination_fd)
            current = os.fstat(verification_fd)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError(f"{label} directory authority changed during write")
        finally:
            os.close(verification_fd)
        if backup_created:
            os.unlink(backup_name, dir_fd=destination_fd)
            backup_created = False
        rollback_needed = False
        os.fsync(destination_fd)
    except Exception:
        if published and rollback_needed and destination_fd >= 0:
            try:
                if backup_created:
                    os.replace(
                        backup_name,
                        filename,
                        src_dir_fd=destination_fd,
                        dst_dir_fd=destination_fd,
                    )
                    backup_created = False
                else:
                    os.unlink(filename, dir_fd=destination_fd)
                os.fsync(destination_fd)
            except OSError:
                pass
        raise
    finally:
        if destination_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=destination_fd)
            except OSError:
                pass
            if backup_created:
                try:
                    os.unlink(backup_name, dir_fd=destination_fd)
                except OSError:
                    pass
            os.close(destination_fd)
    return lexical_parent / filename


__all__ = [
    "OUTPUT_CLAIM_FILENAME",
    "OutputClaimIdentity",
    "atomic_write_owned_output_text",
    "atomic_write_owned_output_text_beneath",
    "collect_output_claim_identities",
    "first_filesystem_alias_component",
    "is_contained_output_path",
    "is_filesystem_alias",
    "is_output_claim_artifact",
    "is_output_claim_path",
    "is_scientific_output_file",
    "stat_is_filesystem_alias",
]
