"""Non-overwriting finished-photo copies with a reviewed destination snapshot."""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from .locations import check_folder, contained_path, safe_relative


def sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def destination(snapshot: dict, relative: str, digest: str) -> str:
    root = check_folder(snapshot["folder"])
    relative_path = Path(snapshot["prefix"]) / str(safe_relative(relative))
    candidate = contained_path(root, relative_path.as_posix())
    if candidate.exists() and (not candidate.is_file() or sha256(candidate) != digest):
        relative_path = relative_path.with_name(
            f"{relative_path.stem}~{digest[:12]}{relative_path.suffix}"
        )
        candidate = contained_path(root, relative_path.as_posix())
        if candidate.exists() and (not candidate.is_file() or sha256(candidate) != digest):
            raise ValueError(f"A conflicting file blocks this output: {candidate}")
    return str(candidate)


@contextmanager
def output_parent(identity: dict, relative: str):
    root = check_folder(identity)
    # The durable identity may span CloudFS remounts; the descriptor must still
    # refer to exactly the directory just verified for this operation.
    current = root.stat()
    parts = safe_relative(relative).parts
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("Output folder changed during the operation")
        for part in parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, dir_fd=descriptor)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def publish_without_replacing(
    parent: int, temporary: str, name: str, *, destination_parent: int | None = None
) -> None:
    destination_parent = parent if destination_parent is None else destination_parent
    if sys.platform == "darwin":
        # macOS supports exclusive rename without requiring hard links on a cloud mount.
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(
            parent, os.fsencode(temporary), destination_parent, os.fsencode(name), 0x00000004
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)
    else:
        os.link(
            temporary, name, src_dir_fd=parent, dst_dir_fd=destination_parent, follow_symlinks=False
        )
        os.unlink(temporary, dir_fd=parent)


def copy_finished_photo(
    local: Path, target: str, snapshot: dict, expected_hash: str, *, checkpoint=None
) -> None:
    root = check_folder(snapshot["folder"])
    relative = Path(target).relative_to(root).as_posix()
    contained_path(root, relative)
    if sha256(local) != expected_hash:
        raise ValueError("A finished photo changed after review. Refresh files before saving")
    with output_parent(snapshot["folder"], relative) as (parent, name):
        temporary = f".latent-{uuid4().hex}.partial"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent
        )
        try:
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as output:
                source_fd = os.open(local, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_fd, "rb") as source:
                    for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                        if checkpoint is not None:
                            checkpoint()
                        output.write(chunk)
                        digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != expected_hash:
                raise ValueError("The finished photo changed while copying; review it again")
            check_folder(snapshot["folder"])
            try:
                publish_without_replacing(parent, temporary, name)
            except FileExistsError:
                existing_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(existing_fd, "rb") as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() != expected_hash:
                        raise ValueError(
                            "The destination changed after review; existing files were kept"
                        ) from None
            if sha256(Path(target)) != expected_hash:
                raise ValueError("The output folder did not retain the complete photo")
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)
