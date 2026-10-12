import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FILE_NAME = "file.bin"
COMPOSE = {2: ROOT / "compose" / "two-peers.yml", 4: ROOT / "compose" / "four-peers.yml"}
PEERS = {2: ["peer-a", "peer-b"], 4: ["peer-a", "peer-b", "peer-c", "peer-d"]}
BLOCK_SIZES = [1024, 4096]
FILES = [
    ("A", 10 * 1024), ("A", 20 * 1024),
    ("B", 1024 * 1024), ("B", 5 * 1024 * 1024),
    ("C", 10 * 1024 * 1024), ("C", 20 * 1024 * 1024),
]
COMPLETION_TIMEOUT = 300
BLOCK_LINE = re.compile(r"^(peer-[a-z])-\d+\s+\|.*\] block (\d+) received from (\S+)$")
FIELDS = [
    "scenario", "peers", "block_size", "file", "file_size", "repetition", "peer",
    "elapsed_seconds", "download_seconds", "block_count", "blocks_by_source",
    "messages_sent", "messages_received",
    "integrity_ok", "size_ok", "sources_ok", "log_ok", "exit_ok",
]


def human(size: int) -> str:
    return f"{size // (1024 * 1024)}MB" if size >= 1024 * 1024 else f"{size // 1024}KB"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compose(peers: int, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    env = os.environ | {"HOST_UID": str(os.getuid()), "HOST_GID": str(os.getgid()), "FILE": FILE_NAME}
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE[peers]), *args],
                          env=env, capture_output=True, text=True, timeout=timeout, check=True)


def prepare(peers: int, block_size: int, size: int) -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    for name in PEERS[peers]:
        (DATA / name).mkdir(parents=True)
    seed = DATA / "peer-a" / FILE_NAME
    seed.write_bytes(os.urandom(size))
    subprocess.run([sys.executable, "-m", "p2p", "meta", "create", str(seed), "--block-size", str(block_size)],
                   cwd=ROOT, check=True, capture_output=True)


def completed(name: str) -> bool:
    try:
        return json.loads((DATA / name / "summary.json").read_text())["completion"] is not None
    except (OSError, ValueError):
        return False


def wait_completion(peers: int) -> bool:
    deadline = time.monotonic() + COMPLETION_TIMEOUT
    while time.monotonic() < deadline:
        if all(completed(name) for name in PEERS[peers][1:]):
            return True
        time.sleep(0.1)
    return False


def logged_sources(log: str, names: dict[str, str]) -> dict[str, dict[str, int]]:
    sources: dict[str, dict[str, int]] = {}
    for line in log.splitlines():
        match = BLOCK_LINE.match(line)
        if match:
            peer, source = match.group(1), names.get(match.group(3), match.group(3))
            sources.setdefault(peer, {}).setdefault(source, 0)
            sources[peer][source] += 1
    return sources


def run(peers: int, block_size: int, label: str, size: int, repetition: int, output: Path) -> list[dict]:
    scenario = f"p{peers}_b{block_size}_{label}_{human(size)}_r{repetition}"
    prepare(peers, block_size, size)
    original = sha256(DATA / "peer-a" / FILE_NAME)
    try:
        compose(peers, "up", "-d")
        finished = wait_completion(peers)
        log = compose(peers, "logs", "--no-color").stdout
        compose(peers, "stop", "-t", "10")
        exits = dict(line.split() for line in
                     compose(peers, "ps", "-a", "--format", "{{.Service}} {{.ExitCode}}").stdout.splitlines())
    finally:
        compose(peers, "down")

    target = output / scenario
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with gzip.open(target / "compose.log.gz", "wt") as f:
        f.write(log)
    summaries = {}
    for name in PEERS[peers]:
        path = DATA / name / "summary.json"
        if path.exists():
            shutil.copy(path, target / f"{name}.json")
            summaries[name] = json.loads(path.read_text())
    names = {summary["peer"]: name for name, summary in summaries.items()}
    from_log = logged_sources(log, names)

    rows = []
    for name in PEERS[peers][1:]:
        summary = summaries.get(name, {})
        completion = summary.get("completion") or {}
        meta = summary.get("metadata") or {}
        downloaded = DATA / name / FILE_NAME
        sources = {names.get(source, source): count for source, count in summary.get("blocks_by_source", {}).items()}
        neighbors = {names.get(neighbor, neighbor) for neighbor in summary.get("neighbors", [])}
        rows.append({
            "scenario": scenario,
            "peers": peers,
            "block_size": block_size,
            "file": label,
            "file_size": size,
            "repetition": repetition,
            "peer": name,
            "elapsed_seconds": completion.get("elapsed_seconds"),
            "download_seconds": completion.get("download_seconds"),
            "block_count": meta.get("block_count"),
            "blocks_by_source": ";".join(f"{source}={count}" for source, count in sorted(sources.items())),
            "messages_sent": sum(summary.get("messages", {}).get("sent", {}).values()),
            "messages_received": sum(summary.get("messages", {}).get("received", {}).values()),
            "integrity_ok": finished and downloaded.exists() and sha256(downloaded) == original,
            "size_ok": downloaded.exists() and downloaded.stat().st_size == size,
            "sources_ok": bool(sources) and set(sources) <= neighbors,
            "log_ok": from_log.get(name) == sources and sum(sources.values()) == meta.get("block_count"),
            "exit_ok": exits.get(name) == "0" and exits.get("peer-a") == "0",
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Executa os estudos de caso no Docker")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    args = parser.parse_args()

    subprocess.run(["docker", "compose", "-f", str(COMPOSE[4]), "build"], check=True, capture_output=True)
    args.output.mkdir(parents=True, exist_ok=True)
    scenarios = [(peers, block_size, label, size, repetition)
                 for peers in PEERS for block_size in BLOCK_SIZES for label, size in FILES
                 for repetition in range(1, args.repetitions + 1)]
    rows = []
    for number, scenario in enumerate(scenarios, 1):
        result = run(*scenario, args.output)
        rows.extend(result)
        ok = all(row[key] for row in result for key in FIELDS[-5:])
        times = " ".join(f"{row['peer']}={row['elapsed_seconds']}s" for row in result)
        print(f"[{number}/{len(scenarios)}] {result[0]['scenario']}: {'OK' if ok else 'FAIL'} {times}", flush=True)
    shutil.rmtree(DATA, ignore_errors=True)

    with (args.output / "results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    failures = [row for row in rows if not all(row[key] for key in FIELDS[-5:])]
    print(f"{len(rows) - len(failures)}/{len(rows)} leecher runs OK, results in {args.output}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
