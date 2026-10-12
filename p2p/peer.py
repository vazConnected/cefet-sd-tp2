import asyncio
import logging
from pathlib import Path

from p2p import protocol
from p2p.blocks import BlockStore
from p2p.metadata import Metadata
from p2p.protocol import MessageType

RECONNECT_DELAY = 1.0

log = logging.getLogger("p2p")


class Connection:
    def __init__(self, peer: "Peer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter, outgoing: bool):
        self.peer = peer
        self.reader = reader
        self.writer = writer
        self.outgoing = outgoing
        self.remote_id = "{}:{}".format(*writer.get_extra_info("peername")[:2])
        self.identified = False
        self.duplicate = False
        self.remote_have: list[bool] | None = None
        self.pending_bitfield: bytes | None = None
        self.pending: dict[int, asyncio.Future] = {}

    def send(self, msg_type: MessageType, payload: bytes = b"") -> None:
        protocol.write_message(self.writer, msg_type, payload)

    def send_bitfield(self) -> None:
        self.send(MessageType.BITFIELD, protocol.pack_bitfield(self.peer.store.have))

    async def request(self, index: int) -> bytes | None:
        future = asyncio.get_running_loop().create_future()
        self.pending[index] = future
        self.send(MessageType.REQUEST, protocol.pack_index(index))
        await self.writer.drain()
        return await future

    async def run(self) -> None:
        self.peer.connections.add(self)
        try:
            self.send(MessageType.HELLO, self.peer.id.encode())
            if self.peer.store:
                self.send_bitfield()
            await self.writer.drain()
            while not self.duplicate:
                msg_type, payload = await protocol.read_message(self.reader)
                self.handle(msg_type, payload)
                await self.writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            if not self.duplicate:
                log.info("disconnected from %s", self.remote_id)
        except protocol.ProtocolError as error:
            log.warning("protocol error from %s: %s", self.remote_id, error)
        finally:
            self.peer.connections.discard(self)
            for future in self.pending.values():
                if not future.done():
                    future.set_result(None)
            self.writer.close()
            self.peer.availability.set()

    def handle(self, msg_type: MessageType, payload: bytes) -> None:
        store = self.peer.store
        if msg_type is MessageType.HELLO:
            self.remote_id = payload.decode(errors="replace")
            self.identified = True
            self.peer.register(self)
        elif msg_type is MessageType.GET_META:
            if store:
                self.send(MessageType.META, protocol.pack_meta(store.meta))
        elif msg_type is MessageType.META:
            if not store:
                self.peer.set_metadata(protocol.unpack_meta(payload), self.remote_id)
        elif msg_type is MessageType.BITFIELD:
            if store:
                self.remote_have = protocol.unpack_bitfield(payload, store.meta.block_count)
                self.peer.availability.set()
            else:
                self.pending_bitfield = payload
                self.send(MessageType.GET_META)
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
                 store: BlockStore | None, output_dir: Path):
        self.host = host
        self.port = port
        self.id = f"{host}:{port}"
        self.neighbors = neighbors
        self.store = store
        self.output_dir = Path(output_dir)
        self.connections: set[Connection] = set()
        self.availability = asyncio.Event()
        self.metadata_ready = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
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
                break
            except OSError:
                await asyncio.sleep(RECONNECT_DELAY)
        await Connection(self, reader, writer, outgoing=True).run()

    def register(self, connection: Connection) -> None:
        duplicate = next((c for c in self.connections
                          if c is not connection and c.identified and c.remote_id == connection.remote_id), None)
        if duplicate is None:
            log.info("connected to %s", connection.remote_id)
            return
        keep_initiator = min(self.id, connection.remote_id)
        drop = connection if connection.initiator != keep_initiator else duplicate
        drop.duplicate = True
        drop.writer.close()

    def set_metadata(self, meta: Metadata, source: str) -> None:
        self.store = BlockStore(meta)
        log.info("metadata received from %s: %s, %d bytes, %d blocks of %d bytes",
                 source, meta.name, meta.size, meta.block_count, meta.block_size)
        for connection in self.connections:
            if connection.pending_bitfield is not None:
                connection.remote_have = protocol.unpack_bitfield(connection.pending_bitfield, meta.block_count)
                connection.pending_bitfield = None
            connection.send_bitfield()
        self.metadata_ready.set()
        self.availability.set()

    def find_source(self, index: int) -> Connection | None:
        for connection in self.connections:
            if connection.remote_have and connection.remote_have[index]:
                return connection
        return None

    async def download(self) -> None:
        await self.metadata_ready.wait()
        store = self.store
        for index in range(store.meta.block_count):
            while not store.have[index]:
                self.availability.clear()
                source = self.find_source(index)
                if source is None:
                    await self.availability.wait()
                    continue
                data = await source.request(index)
                if data is None or not store.add(index, data):
                    if source.remote_have:
                        source.remote_have[index] = False
        path = store.assemble(self.output_dir)
        log.info("download complete: %s, %d bytes, sha256 %s", path, store.meta.size, store.meta.sha256)
