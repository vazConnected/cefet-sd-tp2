import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator

ROOT = Path(__file__).resolve().parent.parent
SEEDER = "peer-a"
CHECKS = ["integrity_ok", "size_ok", "sources_ok", "log_ok", "exit_ok"]
MB = 1024 * 1024

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e6e5e1"
LEECHERS = ["peer-b", "peer-c", "peer-d"]
PEER_COLORS = {"peer-b": "#2a78d6", "peer-c": "#eb6834", "peer-d": "#1baf7a"}
SERIES = {
    (2, 1024): ("#2a78d6", "o", "-"),
    (2, 4096): ("#eb6834", "s", "-"),
    (4, 1024): ("#1baf7a", "^", "--"),
    (4, 4096): ("#eda100", "D", "--"),
}


def size_label(size: int) -> str:
    return f"{size // MB} MB" if size >= MB else f"{size // 1024} KB"


def series_label(peers: int, block_size: int) -> str:
    return f"{peers} peers, bloco {block_size // 1024} KB"


def mean_std(values: list[float]) -> tuple[float, float]:
    return statistics.mean(values), statistics.stdev(values) if len(values) > 1 else 0.0


def number(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def mean_pm(mean: float, std: float, digits: int) -> str:
    return f"{number(mean, digits)} ± {number(std, digits)}"


def parse_sources(text: str) -> dict[str, int]:
    return {source: int(count) for source, count in (item.split("=") for item in text.split(";") if item)}


def load(path: Path) -> list[dict]:
    with path.open() as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for key in ("peers", "block_size", "file_size", "repetition", "block_count", "messages_sent", "messages_received"):
            row[key] = int(row[key])
        for key in ("elapsed_seconds", "download_seconds"):
            row[key] = float(row[key])
        for key in CHECKS:
            row[key] = row[key] == "True"
        row["sources"] = parse_sources(row["blocks_by_source"])
    return rows


def aggregate(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["peers"], row["block_size"], row["file"], row["file_size"])].append(row)
    result = []
    for (peers, block_size, label, size), group in sorted(groups.items()):
        last = defaultdict(float)
        from_seeder = defaultdict(int)
        transferred = defaultdict(int)
        for row in group:
            last[row["repetition"]] = max(last[row["repetition"]], row["elapsed_seconds"])
            from_seeder[row["repetition"]] += row["sources"].get(SEEDER, 0)
            transferred[row["repetition"]] += sum(row["sources"].values())
        by_peer = defaultdict(list)
        for row in group:
            from_leechers = sum(count for source, count in row["sources"].items() if source != SEEDER)
            by_peer[row["peer"]].append(100 * from_leechers / row["block_count"])
        entry = {
            "peers": peers,
            "block_size": block_size,
            "file": label,
            "file_size": size,
            "block_count": group[0]["block_count"],
            "runs": len(group),
            "checks_ok": sum(all(row[key] for key in CHECKS) for row in group),
            "leecher_share": {peer: mean_std(values) for peer, values in sorted(by_peer.items())},
        }
        for peer, (mean, std) in entry["leecher_share"].items():
            entry[f"leecher_share_{peer}_mean"], entry[f"leecher_share_{peer}_std"] = mean, std
        for name, values in (
            ("leecher_time", [row["elapsed_seconds"] for row in group]),
            ("last_time", list(last.values())),
            ("seeder_share", [100 * from_seeder[rep] / transferred[rep] for rep in transferred]),
            ("throughput", [row["file_size"] / MB / row["download_seconds"] for row in group]),
            ("messages_per_block", [(row["messages_sent"] + row["messages_received"]) / row["block_count"]
                                   for row in group]),
        ):
            entry[f"{name}_mean"], entry[f"{name}_std"] = mean_std(values)
        result.append(entry)
    return result


AGGREGATE_FIELDS = [
    "peers", "block_size", "file", "file_size", "block_count", "runs", "checks_ok",
    "leecher_time_mean", "leecher_time_std", "last_time_mean", "last_time_std",
    "throughput_mean", "throughput_std", "messages_per_block_mean", "messages_per_block_std",
    "seeder_share_mean", "seeder_share_std",
    *[f"leecher_share_{peer}_{stat}" for peer in LEECHERS for stat in ("mean", "std")],
]


def write_csv(entries: list[dict], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=AGGREGATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for entry in entries:
            writer.writerow({key: round(value, 4) if isinstance(value, float) else value
                             for key, value in entry.items()})


def write_markdown(entries: list[dict], rows: list[dict], path: Path) -> None:
    repetitions = max(row["repetition"] for row in rows)
    ok = sum(all(row[key] for key in CHECKS) for row in rows)
    lines = [
        "# Resultados dos estudos de caso",
        "",
        f"Matriz de {len(entries)} cenários, combinando 2 ou 4 peers, blocos de 1 KB ou 4 KB e 6 tamanhos de arquivo, "
        f"com {repetitions} repetições cada, executada no Docker. 1 KB equivale a 1024 bytes e 1 MB a 1.048.576 bytes.",
        "",
        f"Dos {len(rows)} downloads, {ok} foram aprovados em todos os critérios: SHA-256 e tamanho do arquivo "
        "remontado, origens restritas aos vizinhos configurados, blocos por origem nos logs iguais aos do resumo e "
        "encerramento sem erro dos containers.",
        "",
        "## Métricas",
        "",
        "| Métrica | Definição |",
        "|---|---|",
        "| Tempo por leecher | Do início do processo até o arquivo remontado e verificado |",
        "| Tempo do último leecher | Maior tempo entre os leechers de uma mesma execução |",
        "| Vazão | Tamanho do arquivo dividido pelo tempo desde o recebimento do metadado até a conclusão |",
        "| Blocos de outros leechers | Percentual de blocos recebidos de outros leechers, e não do seeder |",
        "| Blocos servidos pelo seeder | Percentual de todos os blocos transferidos em uma execução que partiram do seeder |",
        "| Mensagens por bloco | Mensagens enviadas e recebidas pelo leecher divididas pelo número de blocos |",
        "",
        "Valores em média ± desvio padrão.",
        "",
        "| Peers | Bloco | Arquivo | Blocos | Tempo por leecher (s) | Tempo do último leecher (s) "
        "| Vazão (MB/s) | Mensagens por bloco | Verificação |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e['peers']} | {e['block_size'] // 1024} KB | {e['file']} ({size_label(e['file_size'])}) "
            f"| {e['block_count']} | {mean_pm(e['leecher_time_mean'], e['leecher_time_std'], 3)} "
            f"| {mean_pm(e['last_time_mean'], e['last_time_std'], 3)} "
            f"| {mean_pm(e['throughput_mean'], e['throughput_std'], 1)} "
            f"| {mean_pm(e['messages_per_block_mean'], e['messages_per_block_std'], 2)} "
            f"| {e['checks_ok']}/{e['runs']} |")
    lines += [
        "",
        "## Origem dos blocos com 4 peers",
        "",
        "Como peer-d não é vizinho do seeder (peer-a), todos os seus blocos vêm de peer-b e peer-c. "
        "Com 2 peers, o único vizinho do leecher é o seeder, que fornece todos os blocos.",
        "",
        "| Bloco | Arquivo | Blocos de outros leechers: peer-b (%) | peer-c (%) | peer-d (%) "
        "| Blocos servidos pelo seeder (%) |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        if e["peers"] == 4:
            share = e["leecher_share"]
            lines.append(f"| {e['block_size'] // 1024} KB | {e['file']} ({size_label(e['file_size'])}) | "
                         + " | ".join(mean_pm(*share[peer], 1) for peer in LEECHERS)
                         + f" | {mean_pm(e['seeder_share_mean'], e['seeder_share_std'], 1)} |")
    lines += [
        "",
        "## Gráficos",
        "",
        "![Tempo do último leecher](charts/completion_time.png)",
        "",
        "![Vazão por leecher](charts/throughput.png)",
        "",
        "![Blocos recebidos de outros leechers](charts/block_sources.png)",
        "",
        "![Mensagens por bloco](charts/messages_per_block.png)",
        "",
    ]
    path.write_text("\n".join(lines))


def style_axes(ax, title: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=TEXT, fontsize=12, pad=12)
    ax.set_ylabel(ylabel, color=TEXT_SECONDARY)
    ax.tick_params(colors=TEXT_SECONDARY, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def spread_labels(ax, points: list[tuple[float, float, str]], min_gap_points: float = 11) -> list[tuple[float, float, str]]:
    min_gap = min_gap_points * ax.figure.dpi / 72
    to_display = ax.transData.transform
    to_data = ax.transData.inverted().transform
    placed = sorted(((to_display((x, y))[1], x, text) for x, y, text in points))
    adjusted = []
    for display_y, x, text in placed:
        if adjusted and display_y - adjusted[-1][0] < min_gap:
            display_y = adjusted[-1][0] + min_gap
        adjusted.append((display_y, x, text))
    return [(x, to_data((to_display((x, 1))[0], display_y))[1], text) for display_y, x, text in adjusted]


def line_chart(entries: list[dict], metric: str, title: str, ylabel: str, path: Path, log_y: bool) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax, title, ylabel)
    ax.set_xscale("log")
    if log_y:
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(LogLocator(subs=(1.0, 2.0, 5.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}".replace(".", ",")))
    sizes = sorted({e["file_size"] for e in entries})
    ends = []
    for (peers, block_size), (color, marker, linestyle) in SERIES.items():
        points = sorted((e["file_size"], e[f"{metric}_mean"], e[f"{metric}_std"]) for e in entries
                        if e["peers"] == peers and e["block_size"] == block_size)
        xs, ys, errors = zip(*points)
        ax.errorbar(xs, ys, yerr=errors, color=color, marker=marker, linestyle=linestyle, linewidth=1.5,
                    markersize=6, markeredgecolor=SURFACE, markeredgewidth=1, capsize=3, elinewidth=1,
                    label=series_label(peers, block_size))
        ends.append((xs[-1], ys[-1], series_label(peers, block_size)))
    ax.set_xticks(sizes, [size_label(size) for size in sizes], fontsize=8)
    ax.minorticks_off()
    ax.set_xlabel("Tamanho do arquivo", color=TEXT_SECONDARY)
    ax.set_xlim(sizes[0] / 1.6, sizes[-1] * 6)
    ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=TEXT)
    fig.tight_layout()
    fig.canvas.draw()
    for x, y, label in spread_labels(ax, [(x * 1.25, y, label) for x, y, label in ends]):
        ax.annotate(label, (x, y), color=TEXT, fontsize=8, va="center")
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def sources_chart(entries: list[dict], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9, 4.2), dpi=200, sharey=True)
    fig.patch.set_facecolor(SURFACE)
    four = [e for e in entries if e["peers"] == 4]
    sizes = sorted({e["file_size"] for e in four})
    width = 0.27
    for ax, block_size in zip(axes, (1024, 4096)):
        style_axes(ax, f"Bloco {block_size // 1024} KB", "Blocos de outros leechers (%)" if block_size == 1024 else "")
        for offset, peer in zip((-width, 0, width), LEECHERS):
            values = [next(e for e in four if e["file_size"] == size and e["block_size"] == block_size)["leecher_share"][peer]
                      for size in sizes]
            ax.bar([i + offset for i in range(len(sizes))], [mean for mean, _ in values], width,
                   yerr=[std for _, std in values], color=PEER_COLORS[peer], edgecolor=SURFACE, linewidth=2,
                   error_kw={"elinewidth": 1, "capsize": 2, "ecolor": TEXT_SECONDARY}, label=peer)
        ax.set_xticks(range(len(sizes)), [size_label(size) for size in sizes], fontsize=8)
        ax.set_xlabel("Tamanho do arquivo", color=TEXT_SECONDARY)
        ax.set_ylim(0, 105)
    fig.suptitle("Blocos recebidos de outros leechers (4 peers)", x=0.01, ha="left", color=TEXT, fontsize=12)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncols=3, frameon=False, fontsize=8, labelcolor=TEXT)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agrega os resultados dos estudos de caso")
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    args = parser.parse_args()

    rows = load(args.results / "results.csv")
    entries = aggregate(rows)
    charts = args.results / "charts"
    charts.mkdir(exist_ok=True)
    write_csv(entries, args.results / "aggregate.csv")
    write_markdown(entries, rows, args.results / "summary.md")
    line_chart(entries, "last_time", "Tempo até o último leecher concluir", "Tempo (s)",
               charts / "completion_time.png", log_y=True)
    line_chart(entries, "throughput", "Vazão por leecher", "Vazão (MB/s)", charts / "throughput.png", log_y=True)
    line_chart(entries, "messages_per_block", "Mensagens trocadas por bloco", "Mensagens por bloco",
               charts / "messages_per_block.png", log_y=False)
    sources_chart(entries, charts / "block_sources.png")
    print(f"{len(entries)} cenários agregados em {args.results}")


if __name__ == "__main__":
    main()
