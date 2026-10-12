import hashlib
from pathlib import Path

from p2p.metadata import Metadata


class BlockStore:
    def __init__(self, meta: Metadata):
        self.meta = meta
        self.have = [False] * meta.block_count
        self.blocks: dict[int, bytes] = {}

    @classmethod
    def from_file(cls, meta: Metadata, path: Path) -> "BlockStore":
        store = cls(meta)
        with Path(path).open("rb") as f:
            for index in range(meta.block_count):
                if not store.add(index, f.read(meta.block_size)):
                    raise ValueError(f"{path}: block {index} does not match metadata")
            if f.read(1):
                raise ValueError(f"{path}: file is larger than metadata size")
        return store

    @property
    def complete(self) -> bool:
        return all(self.have)

    def add(self, index: int, data: bytes) -> bool:
        if not 0 <= index < self.meta.block_count:
            return False
        if len(data) != self.meta.block_length(index):
            return False
        if hashlib.sha256(data).hexdigest() != self.meta.blocks[index]:
            return False
        self.blocks[index] = data
        self.have[index] = True
        return True

    def get(self, index: int) -> bytes | None:
        return self.blocks.get(index)

    def assemble(self, directory: Path) -> Path:
        if not self.complete:
            raise ValueError("cannot assemble an incomplete file")
        data = b"".join(self.blocks[index] for index in range(self.meta.block_count))
        if len(data) != self.meta.size:
            raise ValueError("assembled file has wrong size")
        if hashlib.sha256(data).hexdigest() != self.meta.sha256:
            raise ValueError("assembled file has wrong checksum")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / Path(self.meta.name).name
        path.write_bytes(data)
        return path
