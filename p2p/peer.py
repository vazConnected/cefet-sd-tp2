import asyncio
import hashlib
import json
import logging
import random
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from p2p import protocol
from p2p.blocks import BlockStore
from p2p.metadata import Metadata
from p2p.protocol import MessageType

RECONNECT_DELAY = 1.0
REQUEST_TIMEOUT = 5.0
PIPELINE = 16
CONTROL_MESSAGES = frozenset({
    MessageType.HELLO, MessageType.GET_META, MessageType.META, MessageType.BITFIELD, MessageType.REJECT,
})

log = logging.getLogger("p2p")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Connection:
    def __init__(self, peer: "Peer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter, outgoing: bool):
        self.peer = peer
        self.reader = reader
        self.writer = writer
        self.outgoing = outgoing
        self.remote_id = "{}:{}".format(*writer.get_extra_info("peername")[:2])
        self.identified = False
        self.duplicate = False
        self.remote_blocks: set[int] = set()
        self.candidates: list[int] = []
        self.pending_bitfield: bytes | None = None
        self.pending: dict[int, asyncio.Future] = {}

    def trace(self, direction: str, msg_type: MessageType, payload: bytes) -> None:
        self.peer.messages[direction][msg_type.name] += 1
        if self.peer.log_messages or msg_type in CONTROL_MESSAGES:
            preposition = "to" if direction == "sent" else "from"
            log.info("%s %s %s %s (%d bytes)", direction, msg_type.name, preposition, self.remote_id, len(payload))

    def send(self, msg_type: MessageType, payload: bytes = b"") -> None:
        protocol.write_message(self.writer, msg_type, payload)
        self.trace("sent", msg_type, payload)

    def send_bitfield(self) -> None:
        self.send(MessageType.BITFIELD, protocol.pack_bitfield(self.peer.store.have))

    def request(self, index: int) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self.pending[index] = future
        self.send(MessageType.REQUEST, protocol.pack_index(index))
        return future

    def close(self) -> None:
        self.peer.connections.discard(self)
        self.writer.close()

    def load_bitfield(self, payload: bytes) -> None:
        have = protocol.unpack_bitfield(payload, self.peer.store.meta.block_count)
        self.remote_blocks.update(index for index, present in enumerate(have) if present)
        self.peer.offer(self, self.remote_blocks)

    async def run(self) -> None:
        self.peer.connections.add(self)
        try:
            if self.outgoing:
                self.send(MessageType.HELLO, self.peer.id.encode())
                await self.writer.drain()
            while not self.duplicate:
                msg_type, payload = await protocol.read_message(self.reader)
                if msg_type is MessageType.HELLO:
                    self.remote_id = payload.decode(errors="replace")
                self.trace("received", msg_type, payload)
                self.handle(msg_type, payload)
                await self.writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            if not self.duplicate:
                log.info("disconnected from %s", self.remote_id)
        except protocol.ProtocolError as error:
            log.warning("protocol error from %s: %s", self.remote_id, error)
        finally:
            self.close()
            for future in self.pending.values():
                if not future.done():
                    future.set_result(None)
            self.peer.request_metadata()
            self.peer.availability.set()

    def handle(self, msg_type: MessageType, payload: bytes) -> None:
        store = self.peer.store
        if msg_type is MessageType.HELLO:
            self.identified = True
            if not self.outgoing:
                self.send(MessageType.HELLO, self.peer.id.encode())
            self.peer.register(self)
            if store and not self.duplicate:
                self.send_bitfield()
        elif msg_type is MessageType.GET_META:
            if store:
                self.send(MessageType.META, protocol.pack_meta(store.meta))
        elif msg_type is MessageType.META:
            if not store:
                self.peer.set_metadata(protocol.unpack_meta(payload), self.remote_id)
        elif msg_type is MessageType.BITFIELD:
            if store:
                self.load_bitfield(payload)
            else:
                self.pending_bitfield = payload
                self.peer.request_metadata()
        elif msg_type is MessageType.HAVE:
            index = protocol.unpack_index(payload)
            if store and index >= store.meta.block_count:
                raise protocol.ProtocolError(f"invalid block index: {index}")
            self.remote_blocks.add(index)
            if store:
                self.peer.offer(self, [index])
        elif msg_type is MessageType.REQUEST:
            index = protocol.unpack_index(payload)
            data = store.get(index) if store else None
            if data is None:
                self.send(MessageType.REJECT, payload)
            else:
                self.send(MessageType.PIECE, protocol.pack_piece(index, data))
        elif msg_type is MessageType.PIECE:
            index, data = protocol.unpack_piece(payload)
            self.resolve(index, data)
        elif msg_type is MessageType.REJECT:
            self.resolve(protocol.unpack_index(payload), None)

    @property
    def initiator(self) -> str:
        return self.peer.id if self.outgoing else self.remote_id

    def resolve(self, index: int, data: bytes | None) -> None:
        future = self.pending.pop(index, None)
        if future and not future.done():
            future.set_result(data)


class Peer:
    def __init__(self, host: str, port: int, neighbors: list[tuple[str, int]],
                 store: BlockStore | None, output_dir: Path, log_messages: bool = False):
        self.host = host
        self.port = port
        self.id = f"{host}:{port}"
        self.neighbors = neighbors
        self.store = store
        self.output_dir = Path(output_dir)
        self.log_messages = log_messages
        self.connections: set[Connection] = set()
        self.metadata_source: Connection | None = None
        self.wanted: set[int] = set()
        self.sources: Counter[str] = Counter()
        self.messages = {"sent": Counter(), "received": Counter()}
        self.reported_decile = 0
        self.availability = asyncio.Event()
        self.metadata_ready = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.started_at = now()
        self.started = time.monotonic()
        self.metadata_received = self.started
        if store:
            self.metadata_ready.set()

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.task_done)

    def task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("task failed", exc_info=task.exception())

    async def run(self) -> None:
        server = await asyncio.start_server(self.accept, self.host, self.port)
        log.info("listening on %s", self.id)
        if self.store:
            log.info("seeding %s: %d/%d blocks", self.store.meta.name,
                     self.store.meta.block_count - self.store.remaining, self.store.meta.block_count)
        for host, port in self.neighbors:
            self.spawn(self.connect(host, port))
        if not self.store:
            self.spawn(self.download())
        async with server:
            await server.serve_forever()

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await Connection(self, reader, writer, outgoing=False).run()

    async def connect(self, host: str, port: int) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError:
                await asyncio.sleep(RECONNECT_DELAY)
                continue
            connection = Connection(self, reader, writer, outgoing=True)
            await connection.run()
            if connection.duplicate:
                return
            await asyncio.sleep(RECONNECT_DELAY)

    def register(self, connection: Connection) -> None:
        duplicate = next((c for c in self.connections
                          if c is not connection and c.identified and c.remote_id == connection.remote_id), None)
        if duplicate is None:
            log.info("connected to %s", connection.remote_id)
            return
        keep_initiator = min(self.id, connection.remote_id)
        drop = connection if connection.initiator != keep_initiator else duplicate
        log.info("closing duplicate connection to %s", connection.remote_id)
        drop.duplicate = True
        drop.close()

    def request_metadata(self) -> None:
        if self.store or self.metadata_source in self.connections:
            return
        self.metadata_source = next((c for c in self.connections if c.pending_bitfield is not None), None)
        if self.metadata_source:
            self.metadata_source.send(MessageType.GET_META)
            self.spawn(self.metadata_timeout(self.metadata_source))

    async def metadata_timeout(self, connection: Connection) -> None:
        await asyncio.sleep(REQUEST_TIMEOUT)
        if not self.store and self.metadata_source is connection and connection in self.connections:
            log.warning("metadata request to %s timed out, closing connection", connection.remote_id)
            connection.close()

    def set_metadata(self, meta: Metadata, source: str) -> None:
        self.store = BlockStore(meta)
        self.wanted = set(range(meta.block_count))
        self.metadata_received = time.monotonic()
        log.info("metadata received from %s: %s, %d bytes, %d blocks of %d bytes",
                 source, meta.name, meta.size, meta.block_count, meta.block_size)
        for connection in list(self.connections):
            payload, connection.pending_bitfield = connection.pending_bitfield, None
            try:
                if payload is None:
                    self.offer(connection, connection.remote_blocks)
                else:
                    connection.load_bitfield(payload)
            except protocol.ProtocolError as error:
                log.warning("protocol error from %s: %s", connection.remote_id, error)
                connection.close()
                continue
            if connection.identified:
                connection.send_bitfield()
        self.metadata_ready.set()

    def offer(self, connection: Connection, indexes) -> None:
        new = [index for index in indexes if index in self.wanted]
        random.shuffle(new)
        connection.candidates.extend(new)
        self.availability.set()

    def release(self, index: int) -> None:
        self.wanted.add(index)
        for connection in self.connections:
            if index in connection.remote_blocks:
                connection.candidates.append(index)

    def next_block(self, connection: Connection) -> int | None:
        while connection.candidates:
            index = connection.candidates.pop()
            if index in self.wanted:
                self.wanted.discard(index)
                return index
        return None

    def schedule(self) -> None:
        for connection in list(self.connections):
            while len(connection.pending) < PIPELINE:
                index = self.next_block(connection)
                if index is None:
                    break
                self.spawn(self.fetch(connection, index, connection.request(index)))

    async def fetch(self, connection: Connection, index: int, future: asyncio.Future) -> None:
        try:
            data = await asyncio.wait_for(future, REQUEST_TIMEOUT)
        except TimeoutError:
            connection.pending.pop(index, None)
            if connection in self.connections:
                log.warning("request for block %d to %s timed out, closing connection", index, connection.remote_id)
                connection.close()
            self.release(index)
        else:
            if data is not None and self.store.add(index, data):
                self.block_received(index, connection.remote_id)
            else:
                if data is not None:
                    log.warning("block %d from %s failed verification", index, connection.remote_id)
                connection.remote_blocks.discard(index)
                self.release(index)
        self.availability.set()

    def block_received(self, index: int, source: str) -> None:
        self.sources[source] += 1
        log.info("block %d received from %s", index, source)
        payload = protocol.pack_index(index)
        for connection in self.connections:
            if connection.identified:
                connection.send(MessageType.HAVE, payload)
        total = self.store.meta.block_count
        done = total - self.store.remaining
        if done * 10 // total > self.reported_decile:
            self.reported_decile = done * 10 // total
            log.info("progress: %d/%d blocks (%d%%)", done, total, done * 100 // total)

    async def download(self) -> None:
        await self.metadata_ready.wait()
        while not self.store.complete:
            self.availability.clear()
            self.schedule()
            await self.availability.wait()
        path = self.store.assemble(self.output_dir)
        summary = self.summary(path)
        (self.output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        log.info("download complete: %s, %d bytes, sha256 %s (%s), blocks by source: %s",
                 path, summary["size"], summary["sha256"], "ok" if summary["integrity_ok"] else "MISMATCH",
                 ", ".join(f"{source}={count}" for source, count in summary["blocks_by_source"].items()))

    def summary(self, path: Path) -> dict:
        meta = self.store.meta
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        finished = time.monotonic()
        return {
            "peer": self.id,
            "neighbors": [f"{host}:{port}" for host, port in self.neighbors],
            "file": str(path),
            "size": len(data),
            "expected_size": meta.size,
            "sha256": sha256,
            "expected_sha256": meta.sha256,
            "integrity_ok": len(data) == meta.size and sha256 == meta.sha256,
            "block_size": meta.block_size,
            "block_count": meta.block_count,
            "started_at": self.started_at,
            "completed_at": now(),
            "elapsed_seconds": round(finished - self.started, 3),
            "download_seconds": round(finished - self.metadata_received, 3),
            "blocks_by_source": dict(sorted(self.sources.items())),
            "messages": {direction: dict(sorted(counts.items())) for direction, counts in self.messages.items()},
        }
