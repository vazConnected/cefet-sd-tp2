import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_BLOCK_SIZE = 1024


@dataclass(frozen=True)
class Metadata:
    name: str
    size: int
    block_size: int
    block_count: int
    sha256: str
    blocks: list[str]

    def __post_init__(self):
        if self.size <= 0:
            raise ValueError("size must be positive")
        if self.block_size <= 0:
            raise ValueError("block_size must be positive")
        if self.block_count != math.ceil(self.size / self.block_size):
            raise ValueError("block_count does not match size and block_size")
        if len(self.blocks) != self.block_count:
            raise ValueError("number of block hashes does not match block_count")

    def block_length(self, index: int) -> int:
        if not 0 <= index < self.block_count:
            raise IndexError(f"block {index} out of range")
        return min(self.block_size, self.size - index * self.block_size)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> "Metadata":
        return cls(**json.loads(Path(path).read_text()))


def create_metadata(path: Path, block_size: int = DEFAULT_BLOCK_SIZE) -> Metadata:
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    path = Path(path)
    if path.stat().st_size == 0:
        raise ValueError(f"{path}: empty file")
    file_hash = hashlib.sha256()
    blocks = []
    with path.open("rb") as f:
        while chunk := f.read(block_size):
            file_hash.update(chunk)
            blocks.append(hashlib.sha256(chunk).hexdigest())
    return Metadata(
        name=path.name,
        size=path.stat().st_size,
        block_size=block_size,
        block_count=len(blocks),
        sha256=file_hash.hexdigest(),
        blocks=blocks,
    )
