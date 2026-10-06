"""Local service identity and exclusive ownership of a writable workspace."""

from __future__ import annotations

import fcntl
import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .errors import ConfigurationError

SERVICE_PROTOCOL_VERSION = 1


def service_identity(
    state_dir: Path, embedding_dir: Path, workspace_dir: Path
) -> dict[str, object]:
    paths = (state_dir, embedding_dir, workspace_dir)
    identity = "\0".join(str(path.expanduser().resolve()) for path in paths)
    return {
        "status": "ok",
        "service": "latent",
        "protocol_version": SERVICE_PROTOCOL_VERSION,
        "service_version": __version__,
        "instance_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "data_id": hashlib.sha256(identity.encode()).hexdigest(),
        "cloud_access": False,
    }


@contextmanager
def workspace_service_lease(workspace_dir: Path) -> Iterator[None]:
    """Hold a kernel lock for the process lifetime; never trust or signal a saved PID."""
    directory = workspace_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the file after close. Unlinking a lock file creates an inode race.
    descriptor = os.open(directory / ".service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ConfigurationError(
                "Another Latent service is using this workspace. Connect to that service "
                "or choose a separate workspace; no existing process was stopped."
            ) from error
        yield
    finally:
        os.close(descriptor)
