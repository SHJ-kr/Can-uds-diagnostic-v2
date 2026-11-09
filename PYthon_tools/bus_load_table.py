"""Render the version-2 latency/deadline summary, retaining explicit metric names."""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "logs" / "bus_load_v2"
COLUMNS = {
    "background_ecu_count": "Background\nECUs",
    "modeled_bus_load_pct": "Modeled bus\noccupancy (%)",
    "total_requests": "Requests",
    "avg_completed_response_ms": "Completed mean\nlatency (ms)",
    "p95_completed_response_ms": "Completed P95\nlatency (ms)",
    "on_time_completion_rate_pct": "On-time valid\ncompletion (%)",
    "deadline_miss_rate_pct": "Deadline miss /\nfailed (%)",
    "pos_rate_pct": "POS responses\n(no deadline) (%)",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    source = args.output_dir / "summary.csv"
    if not source.exists():
        parser.error(f"Missing {source}; run bus_load_sweep.py first")
    df = pd.read_csv(source)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        parser.error(f"Not a version-2 summary; missing columns: {sorted(missing)}")
    integers = {"background_ecu_count", "total_requests"}
    cells = [
        ["N/A" if pd.isna(row[key]) else
         (f"{int(row[key])}" if key in integers else f"{row[key]:.2f}")
         for key in COLUMNS]
        for _, row in df.iterrows()
    ]
    fig, ax = plt.subplots(figsize=(15, 0.6 * len(df) + 1.8))
    ax.axis("off")
    table = ax.table(cellText=cells, colLabels=list(COLUMNS.values()), cellLoc="center", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.auto_set_column_width(list(range(len(COLUMNS))))
    table.scale(1, 2.2)
    for col in range(len(COLUMNS)):
        table[0, col].set_facecolor("#2E5090")
        table[0, col].set_text_props(color="white", weight="bold")
    for index, (_, row) in enumerate(df.iterrows(), 1):
        if row["deadline_miss_rate_pct"] > 0:
            for col in range(len(COLUMNS)):
                table[index, col].set_facecolor("#FFD6D6")
    deadline = df["deadline_ms"].iloc[0]
    ax.set_title(f"Software CAN timing model — configured deadline: {deadline:g} ms\n"
                 "Red: late or failed requests. Occupancy is modeled; not a hardware measurement.", pad=20)
    fig.tight_layout()
    target = args.output_dir / "bus_load_sweep_table.png"
    fig.savefig(target, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(target)


if __name__ == "__main__":
    main()
