"""File storage behind a narrow interface.

The interface exists because the hosting decision is expected to change. The
testing phase runs on an Oracle VM's local disk; moving to S3-compatible
storage such as Cloudflare R2 later should be a new subclass and a config
change, not a rewrite. Nothing outside this module may touch a storage path
directly.
"""
from __future__ import annotations

import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)


class Storage(ABC):
    @abstractmethod
    def path_for(self, key: str) -> Path:
        """Local filesystem path for a key.

        ffmpeg needs a real path to read and write. A remote backend would
        implement this by materialising the object locally first; that is
        exactly why the cutting code asks for a path rather than a stream.
        """

    @abstractmethod
    def open_write(self, key: str, *, append: bool = False) -> BinaryIO: ...

    @abstractmethod
    def size(self, key: str) -> int: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove an object. Returns whether anything was removed."""

    @abstractmethod
    def move(self, source_key: str, destination_key: str) -> None: ...



class LocalDiskStorage(Storage):
    """Files on the machine's own disk, under a single root."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        # Keys come from job ids and generated filenames, never directly from
        # user input, but a traversal check is cheap and this is the one place
        # that turns a string into a filesystem path.
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"key {key!r} escapes the storage root")
        return candidate

    def open_write(self, key: str, *, append: bool = False) -> BinaryIO:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab" if append else "wb")

    def size(self, key: str) -> int:
        path = self.path_for(key)
        return path.stat().st_size if path.exists() else 0

    def exists(self, key: str) -> bool:
        return self.path_for(key).exists()

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
        if path.exists():
            path.unlink()
            return True
        return False

    def move(self, source_key: str, destination_key: str) -> None:
        destination = self.path_for(destination_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.path_for(source_key)), str(destination))

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free
