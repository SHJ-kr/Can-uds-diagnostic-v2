"""
버스 부하(배경 ECU 개수) 파라미터 스윕
----------------------------------------
"클러스터링은 한 번 실행한 결과 안에서 패턴을 찾는 도구일 뿐, 조건을 바꾸면
결과가 어떻게 달라지는지는 답해주지 못한다"는 한계를 보완하기 위한 스크립트다.

배경 ECU 개수(=버스 부하 수준)만 다르게 설정해서 각각 독립적으로 시뮬레이션을
돌리고, 그 결과(평균 응답시간, 이벤트 유형별 비율)를 서로 비교한다. 결함 주입
(fault_injection)은 전부 꺼서(baseline) 돌린다 - 그래야 관찰되는 지연/오류가
결함 주입이 아니라 순수하게 버스 부하 때문이라고 말할 수 있다.
"""
import csv
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_log_generator import run_scenario  # noqa: E402

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
CSV_PATH = LOGS_DIR / "bus_load_sweep.csv"
PLOT_PATH = LOGS_DIR / "bus_load_sweep.png"

# 배경 ECU 개수별로 비교. ID는 전부 진단 ID(0x7E0/0x7E8)보다 낮게(우선순위 높게),
# 주기는 10~20ms로 고정해서 "개수"만 변수가 되게 한다.
LOAD_LEVELS = [0, 2, 4, 6, 8, 10, 15, 20, 30]


def make_presets(n):
    return [(f"bg{i}", 0x100 + i * 0x10, (0.01, 0.02)) for i in range(n)]


def summarize(events):
    total = len(events)
    counts = Counter(e["event_type"] for e in events)
    measured = [e["response_time_ms"] for e in events if e["response_time_ms"] != ""]
    avg_rt = sum(measured) / len(measured) if measured else 0.0
    return {
        "background_ecu_count": None,  # 호출부에서 채움
        "total_requests": total,
        "avg_response_time_ms": round(avg_rt, 2),
        "pos_rate_pct": round(counts.get("POS", 0) / total * 100, 1),
        "nrc_rate_pct": round(counts.get("NRC", 0) / total * 100, 1),
        "incomplete_reassembly_rate_pct": round(counts.get("INCOMPLETE_REASSEMBLY", 0) / total * 100, 1),
        "n_cr_timeout_rate_pct": round(counts.get("N_CR_TIMEOUT", 0) / total * 100, 1),
        "no_response_rate_pct": round(counts.get("NO_RESPONSE", 0) / total * 100, 1),
    }


def main():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for n in LOAD_LEVELS:
        print(f"=== 배경 ECU {n}개로 실행 (baseline, 결함 주입 없음) ===")
        presets = make_presets(n)
        events = run_scenario(f"busload_{n}", {}, background_presets=presets)
        row = summarize(events)
        row["background_ecu_count"] = n
        results.append(row)
        print(
            f"  평균응답시간={row['avg_response_time_ms']}ms  "
            f"POS={row['pos_rate_pct']}%  재조립실패={row['incomplete_reassembly_rate_pct']}%  "
            f"N_Cr타임아웃={row['n_cr_timeout_rate_pct']}%  무응답={row['no_response_rate_pct']}%"
        )

    fieldnames = list(results[0].keys())
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\n결과 CSV 저장: {CSV_PATH}")

    xs = [r["background_ecu_count"] for r in results]
    avg_rt = [r["avg_response_time_ms"] for r in results]
    fail_rate = [
        r["incomplete_reassembly_rate_pct"] + r["n_cr_timeout_rate_pct"] + r["no_response_rate_pct"]
        for r in results
    ]

    fig, ax1 = plt.subplots(figsize=(8, 5))
    ax1.plot(xs, avg_rt, marker="o", color="tab:blue", label="Avg response time (ms)")
    ax1.set_xlabel("Background ECU count (bus load level)")
    ax1.set_ylabel("Avg response time (ms)", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")

    ax2 = ax1.twinx()
    ax2.plot(xs, fail_rate, marker="s", color="tab:red", label="Failure rate (%)")
    ax2.set_ylabel("Failure rate (%) [incomplete/timeout/no-response]", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    plt.title("Bus Load Sweep: background ECU count vs. response time / failure rate")
    fig.tight_layout()
    plt.savefig(PLOT_PATH)
    plt.close()
    print(f"그래프 저장: {PLOT_PATH}")


if __name__ == "__main__":
    main()
