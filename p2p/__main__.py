import argparse
import asyncio
import logging
import sys
from pathlib import Path

from p2p.blocks import BlockStore
from p2p.metadata import DEFAULT_BLOCK_SIZE, Metadata, create_metadata
from p2p.peer import Peer


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"invalid address: {value}")
    return host, int(port)


def meta_create(args: argparse.Namespace) -> int:
    meta = create_metadata(args.file, args.block_size)
    output = args.output or args.file.with_name(args.file.name + ".meta")
    meta.save(output)
    print(f"{output}: {meta.size} bytes, {meta.block_count} blocks of {meta.block_size} bytes, sha256 {meta.sha256}")
    return 0


def run_peer(args: argparse.Namespace) -> int:
    if (args.file is None) != (args.meta is None):
        raise ValueError("--file and --meta must be given together")
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{args.host}:{args.port}] %(message)s")
    store = BlockStore.from_file(Metadata.load(args.meta), args.file) if args.file else None
    try:
        asyncio.run(Peer(args.host, args.port, args.neighbors, store, args.output_dir).run())
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p2p", description="Transferência de arquivos peer-to-peer")
    commands = parser.add_subparsers(dest="command", metavar="<comando>", required=True)

    meta = commands.add_parser("meta", help="gerencia arquivos de metadados")
    meta_commands = meta.add_subparsers(dest="meta_command", metavar="<ação>", required=True)
    create = meta_commands.add_parser("create", help="fragmenta um arquivo e gera seu metadado")
    create.add_argument("file", type=Path)
    create.add_argument("--block-size", type=positive_int, default=DEFAULT_BLOCK_SIZE)
    create.add_argument("-o", "--output", type=Path)
    create.set_defaults(handler=meta_create)

    peer = commands.add_parser("peer", help="inicia um peer")
    peer.add_argument("--host", required=True)
    peer.add_argument("--port", required=True, type=int)
    peer.add_argument("--neighbors", required=True, nargs="+", type=address, metavar="HOST:PORT")
    peer.add_argument("--file", type=Path, help="arquivo completo (seeder)")
    peer.add_argument("--meta", type=Path, help="metadado do arquivo (seeder)")
    peer.add_argument("--output-dir", type=Path, default=Path("."))
    peer.set_defaults(handler=run_peer)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError) as error:
        print(f"p2p: error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
