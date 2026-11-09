"""Measure diagnostic latency/deadlines under background traffic, without fault injection.

Outputs use a new schema/directory so old POS-only results are not overwritten.
Bus utilization is modeled frame time divided by observed request-window time;
it is not a measurement from a physical CAN interface.
"""
import argparse
import csv
import json
import math
import platform
import statistics
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from batch_log_generator import run_scenario
from virtual_ecu import CAN_BITRATE, DEFAULT_FAULT_INJECTION, frame_tx_time

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "logs" / "bus_load_v2"
LOAD_LEVELS = [0, 10, 20, 30, 45, 55]
FIXED_SENSOR_VALUES = {1: 200, 2: 200, 3: 200, 4: 2000}


def make_presets(n):
    if not 0 <= n <= 110:
        raise ValueError("background ECU count must be between 0 and 110 (IDs below diagnostic ID)")
    return [(f"bg{i}", 0x100 + i * 0x10, (0.01, 0.02)) for i in range(n)]


def percentile(values, q):
    if not values:
        return ""
    values = sorted(values)
    index = (len(values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 3)


def summarize(events):
    if not events:
        raise ValueError("no diagnostic requests to summarize")
    total = len(events)
    counts = Counter(e["event_type"] for e in events)
    complete = [e for e in events if e["response_complete"]]
    latencies = [e["response_time_ms"] for e in complete]
    waits = [e["request_tx_wait_ms"] for e in events if e["request_tx_wait_ms"] != ""]
    observed = sum(e["observation_s"] for e in events)
    wire = sum(e["modeled_wire_busy_s"] for e in events)
    service = sum(e["service_busy_s"] for e in events)
    releases = sum(e["background_releases"] for e in events)
    coalesced = sum(e["background_coalesced"] for e in events)
    rate = lambda count: round(100 * count / total, 2)
    return {
        "background_ecu_count": None,
        "total_requests": total,
        "deadline_ms": events[0]["deadline_ms"],
        "modeled_bus_load_pct": round(100 * wire / observed, 3),
        "service_overhead_pct": round(100 * (service - wire) / observed, 3),
        "observation_s": round(observed, 6),
        "avg_completed_response_ms": round(statistics.mean(latencies), 3) if latencies else "",
        "p50_completed_response_ms": percentile(latencies, 0.50),
        "p95_completed_response_ms": percentile(latencies, 0.95),
        "p99_completed_response_ms": percentile(latencies, 0.99),
        "max_completed_response_ms": round(max(latencies), 3) if latencies else "",
        "completed_sample_count": len(complete),
        "avg_wait_ms_all_requests": round(statistics.mean(e["response_time_ms"] for e in events), 3),
        "avg_request_tx_wait_ms": round(statistics.mean(waits), 3) if waits else "",
        "request_tx_wait_sample_count": len(waits),
        "valid_response_rate_pct": rate(len(complete)),
        "on_time_completion_rate_pct": rate(sum(e["deadline_met"] for e in events)),
        "on_time_pos_rate_pct": rate(sum(e["deadline_met"] and e["event_type"] == "POS" for e in events)),
        "deadline_miss_rate_pct": rate(sum(not e["deadline_met"] for e in events)),
        "late_complete_rate_pct": rate(sum(e["response_complete"] and not e["deadline_met"] for e in events)),
        "pos_rate_pct": rate(counts["POS"]),
        "nrc_rate_pct": rate(counts["NRC"]),
        "transport_failure_rate_pct": rate(total - len(complete)),
        "tx_timeout_rate_pct": rate(counts["TX_TIMEOUT"]),
        "no_response_rate_pct": rate(counts["NO_RESPONSE"]),
        "n_cr_timeout_rate_pct": rate(counts["N_CR_TIMEOUT"]),
        "incomplete_reassembly_rate_pct": rate(counts["INCOMPLETE_REASSEMBLY"]),
        "invalid_response_rate_pct": rate(sum(counts[k] for k in (
            "INVALID_RESPONSE", "SEQUENCE_ERROR", "RESPONSE_MISMATCH",
        ))),
        "ignored_frames": sum(e["ignored_frames"] for e in events),
        "background_releases": releases,
        "background_coalesced": coalesced,
        "background_coalesced_rate_pct": round(100 * coalesced / releases, 3) if releases else 0.0,
    }


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plot_results(rows, path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    x = [r["background_ecu_count"] for r in rows]
    for key, label in (
        ("avg_completed_response_ms", "Completed response mean"),
        ("p95_completed_response_ms", "Completed response P95"),
    ):
        axes[0].plot(x, [r[key] if r[key] != "" else float("nan") for r in rows], marker="o", label=label)
    axes[0].axhline(rows[0]["deadline_ms"], linestyle="--", color="gray", label="Configured deadline")
    for row in rows:
        if row["completed_sample_count"] == 0:
            axes[0].annotate("No completed responses", (row["background_ecu_count"], rows[0]["deadline_ms"]),
                             xytext=(0, 10), textcoords="offset points", ha="center", fontsize=8)
    axes[0].set_ylabel("End-to-end latency (ms), completed responses only")
    axes[0].legend()
    axes[1].plot(x, [r["modeled_bus_load_pct"] for r in rows], marker="o", label="Modeled bus occupancy")
    axes[1].plot(x, [r["on_time_completion_rate_pct"] for r in rows], marker="s", label="On-time valid completion")
    axes[1].plot(x, [r["pos_rate_pct"] for r in rows], linestyle=":", label="POS responses (no deadline test)")
    axes[1].set_ylabel("Percent (%)")
    axes[1].set_ylim(0, 105)
    axes[1].legend()
    for ax in axes:
        ax.set_xlabel("Background ECU count")
        ax.set_xticks(x)
        margin = max(2, (max(x) - min(x)) * 0.15)
        ax.set_xlim(min(x) - margin, max(x) + margin)
        ax.grid(alpha=0.25)
    fig.suptitle("Software CAN timing model — no injected faults; not hardware measurements")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default=",".join(map(str, LOAD_LEVELS)))
    parser.add_argument("--requests", type=int, default=200, help="Requests per level per repeat")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--deadline-ms", type=float, default=100.0, help="Experiment requirement, not a UDS standard value")
    parser.add_argument("--hard-timeout-ms", type=float, default=1000.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    try:
        levels = sorted(set(int(v) for v in args.levels.split(",")))
        for n in levels:
            make_presets(n)
    except ValueError as exc:
        parser.error(str(exc))
    if not levels or args.requests <= 0 or args.repeats <= 0:
        parser.error("levels, requests and repeats must be nonempty/positive")
    if not (0 < args.deadline_ms <= args.hard_timeout_ms):
        parser.error("require 0 < deadline-ms <= hard-timeout-ms")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_events, rows = [], []
    for n in levels:
        events = []
        for repeat in range(args.repeats):
            print(f"Background ECUs={n}, repeat={repeat + 1}/{args.repeats}, requests={args.requests}", flush=True)

            def progress(index, event):
                if index % 20 == 0 or index == args.requests:
                    print(f"  {index}/{args.requests} requests finished", flush=True)

            batch = run_scenario(
                f"busload_v2_{n}", {}, background_presets=make_presets(n),
                request_count=args.requests, deadline_ms=args.deadline_ms,
                hard_timeout=args.hard_timeout_ms / 1000, seed=args.seed + repeat,
                measure_load=True, sensor_values=FIXED_SENSOR_VALUES,
                progress_callback=progress,
            )
            for event in batch:
                event.update(background_ecu_count=n, repeat_index=repeat + 1, seed=args.seed + repeat)
            events.extend(batch)
        row = summarize(events)
        row["background_ecu_count"] = n
        row["nominal_offered_background_load_pct"] = round(n * frame_tx_time() / 0.015 * 100, 3)
        rows.append(row)
        all_events.extend(events)
        print(f"  model occupancy={row['modeled_bus_load_pct']}%, "
              f"completed P95={row['p95_completed_response_ms']} ms, "
              f"on-time={row['on_time_completion_rate_pct']}%, POS={row['pos_rate_pct']}%", flush=True)
    baseline = next((r["avg_completed_response_ms"] for r in rows if r["background_ecu_count"] == 0), "")
    for row in rows:
        average = row["avg_completed_response_ms"]
        row["mean_latency_increase_vs_idle_pct"] = round((average / baseline - 1) * 100, 3) \
            if baseline not in ("", 0) and average != "" else ""
    write_csv(args.output_dir / "summary.csv", rows)
    write_csv(args.output_dir / "requests.csv", all_events)
    metadata = {
        "schema_version": 2, "python": sys.version, "platform": platform.platform(),
        "bitrate_bps": CAN_BITRATE, "requests_per_repeat": args.requests,
        "repeats": args.repeats, "seed": args.seed, "levels": levels,
        "deadline_ms": args.deadline_ms, "hard_timeout_ms": args.hard_timeout_ms,
        "fault_injection": DEFAULT_FAULT_INJECTION, "fixed_sensor_values_mm": FIXED_SENSOR_VALUES,
        "definitions": {
            "response_time_ms": "perf_counter before request submission through valid completion or failure",
            "on_time_completion_rate_pct": "complete, matching POS or final NRC within deadline / all requests",
            "on_time_pos_rate_pct": "matching POS within deadline / all requests",
            "deadline_miss_rate_pct": "late or failed requests / all requests",
            "p95_completed_response_ms": "P95 among complete POS/NRC only; failures excluded and reported separately",
            "modeled_bus_load_pct": "modeled wire time / sum of request observation windows; includes diagnostic frames",
            "service_overhead_pct": "in-service wall time beyond modeled wire time / observation windows",
            "background_coalesced": "scheduled background releases replaced while one frame from that source is pending",
        },
        "limitations": [
            "Real-time Python/OS scheduling affects results; fixed seed does not make timing deterministic.",
            "Hard timeout is a budget checked by software; scheduling can delay timeout detection.",
            "Wire time uses (47 + 8 * DLC) * 1.2 / bitrate, including IFS; stuffing is approximate.",
            "No physical error frames, CAN controller retransmission, bus-off or electrical effects are modeled.",
            "One pending background frame per source; offered load may differ from observed occupancy.",
            "Warm-up/recovery excluded; transport failures reset the virtual session to isolate late responses.",
            "These results do not establish physical CAN saturation thresholds or MCU firmware correctness.",
        ],
    }
    (args.output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    plot_results(rows, args.output_dir / "bus_load_sweep.png")
    print(f"Results: {args.output_dir.resolve()}")
    return rows


if __name__ == "__main__":
    main()
