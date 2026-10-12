import argparse
import sys
from pathlib import Path

from p2p.metadata import DEFAULT_BLOCK_SIZE, create_metadata


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def meta_create(args: argparse.Namespace) -> int:
    meta = create_metadata(args.file, args.block_size)
    output = args.output or args.file.with_name(args.file.name + ".meta")
    meta.save(output)
    print(f"{output}: {meta.size} bytes, {meta.block_count} blocks of {meta.block_size} bytes, sha256 {meta.sha256}")
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
