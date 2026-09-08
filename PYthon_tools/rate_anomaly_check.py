"""
비율(rate) 기반 결함 탐지 - out_of_range처럼 "개별 행이 아니라 여러 행에 걸친 발생
확률"에만 신호가 있는 결함을 검증하기 위한 스크립트.

nrc_clustering.py(행 단위 K-Means)는 이런 결함을 원리적으로 탐지할 수 없다는 걸
실측(Adjusted Rand Index)으로 확인했다 - baseline에서 나온 result_char='F' 행과
out_of_range에서 나온 'F' 행은 피처값이 완전히 동일해서, "어느 시나리오에서 나왔는지"
행 하나만 봐서는 구분이 안 되기 때문이다. 대신 "이 시나리오에서 F가 나올 확률 자체가
baseline과 통계적으로 다른가"를 2표본 비율 검정(two-proportion z-test)으로 직접 검증한다.
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import norm

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
CSV_PATH = LOGS_DIR / "real_diagnostic_log.csv"
REPORT_PATH = LOGS_DIR / "rate_anomaly_report.csv"
PLOT_PATH = LOGS_DIR / "rate_anomaly_plot.png"

BASELINE_SCENARIO = "baseline"
ALPHA = 0.05  # 유의수준 5%


def two_proportion_z_test(f1, n1, f2, n2):
    """두 그룹의 비율(f1/n1 vs f2/n2)이 통계적으로 다른지 검정.
    반환: (z통계량, 양측검정 p-value)"""
    p1, p2 = f1 / n1, f2 / n2
    pooled = (f1 + f2) / (n1 + n2)
    se = (pooled * (1 - pooled) * (1 / n1 + 1 / n2)) ** 0.5
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    p_value = 2 * (1 - norm.cdf(abs(z)))
    return z, p_value


def main():
    if not CSV_PATH.exists():
        sys.exit(f"진단 로그가 없습니다: {CSV_PATH}\n먼저 batch_log_generator.py를 실행하세요.")
    df = pd.read_csv(CSV_PATH)

    if "scenario" not in df.columns or "result_char" not in df.columns:
        sys.exit("이 CSV에는 scenario/result_char 컬럼이 없습니다 - batch_log_generator.py를 다시 실행하세요.")

    # result_char가 P 또는 F인 행(=실제 센서 읽기 응답)만 대상으로 한다.
    sensor_rows = df[df["result_char"].isin(["P", "F"])]
    if sensor_rows.empty:
        sys.exit("센서 읽기 응답(result_char가 P/F인 행)이 없습니다.")

    grouped = sensor_rows.groupby("scenario")["result_char"].apply(
        lambda s: pd.Series({"n": len(s), "f_count": (s == "F").sum()})
    ).unstack()
    grouped["f_rate_pct"] = (grouped["f_count"] / grouped["n"] * 100).round(1)

    if BASELINE_SCENARIO not in grouped.index:
        sys.exit(f"'{BASELINE_SCENARIO}' 시나리오가 로그에 없어서 기준으로 삼을 수 없습니다.")

    base_n = int(grouped.loc[BASELINE_SCENARIO, "n"])
    base_f = int(grouped.loc[BASELINE_SCENARIO, "f_count"])
    base_rate = grouped.loc[BASELINE_SCENARIO, "f_rate_pct"]

    # 다중비교 보정: baseline을 뺀 나머지 시나리오 개수만큼 검정을 반복하므로,
    # 그냥 α=0.05를 그대로 쓰면 우연히 유의하게 나오는 것(false positive)이 하나쯤은
    # 나올 확률이 높아진다(실제로 뒤에서 send_pending이 그런 경우로 확인됨). 가장
    # 단순하고 보수적인 Bonferroni 보정(α/검정개수)을 같이 적용해서, "우연"과
    # "진짜 신호"를 구분한다.
    n_tests = (grouped.index != BASELINE_SCENARIO).sum()
    alpha_bonferroni = ALPHA / n_tests if n_tests > 0 else ALPHA

    print(f"기준(baseline) F 비율: {base_f}/{base_n} = {base_rate}%")
    print(f"다중비교 보정: 검정 {n_tests}개 -> Bonferroni 보정 유의수준 = {alpha_bonferroni:.4f}\n")
    print(f"{'시나리오':<16}{'N':>6}{'F건수':>8}{'F비율(%)':>10}{'z':>8}{'p-value':>10}  유의(α=0.05)  보정후 유의")

    rows = []
    for scenario, row in grouped.iterrows():
        n, f_count, rate = int(row["n"]), int(row["f_count"]), row["f_rate_pct"]
        if scenario == BASELINE_SCENARIO:
            z, p = 0.0, 1.0
        else:
            z, p = two_proportion_z_test(f_count, n, base_f, base_n)
        is_baseline = scenario == BASELINE_SCENARIO
        significant = p < ALPHA and not is_baseline
        significant_corrected = p < alpha_bonferroni and not is_baseline
        print(
            f"{scenario:<16}{n:>6}{f_count:>8}{rate:>10}{z:>8.2f}{p:>10.4f}  "
            f"{'예' if significant else '아니오':<12}  {'예 ***' if significant_corrected else '아니오'}"
        )
        rows.append({
            "scenario": scenario, "n": n, "f_count": f_count, "f_rate_pct": rate,
            "baseline_f_rate_pct": base_rate, "z": round(z, 3), "p_value": round(p, 4),
            "significant_at_0.05": significant,
            "significant_after_bonferroni": significant_corrected,
        })

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    report_df = pd.DataFrame(rows)
    report_df.to_csv(REPORT_PATH, index=False, encoding="utf-8")
    print(f"\n리포트 저장: {REPORT_PATH}")

    # 95% 신뢰구간과 함께 막대그래프로 시각화
    fig, ax = plt.subplots(figsize=(9, 5))
    scenarios = report_df["scenario"].tolist()
    rates = report_df["f_rate_pct"].tolist()
    ns = report_df["n"].tolist()
    fs = report_df["f_count"].tolist()
    ci = [1.96 * ((f / n) * (1 - f / n) / n) ** 0.5 * 100 for f, n in zip(fs, ns)]
    colors = ["tab:gray" if s == BASELINE_SCENARIO else ("tab:red" if sig else "tab:blue")
              for s, sig in zip(scenarios, report_df["significant_after_bonferroni"])]

    ax.bar(scenarios, rates, yerr=ci, capsize=5, color=colors)
    ax.axhline(base_rate, color="black", linestyle="--", linewidth=1, label=f"baseline = {base_rate}%")
    ax.set_ylabel("F (out-of-range) rate (%)")
    ax.set_title("Sensor out-of-range rate by scenario (error bars = 95% CI)\n"
                 "red = statistically significant vs baseline after Bonferroni correction")
    ax.legend()
    plt.xticks(rotation=20)
    plt.tight_layout()
    plt.savefig(PLOT_PATH)
    plt.close()
    print(f"그래프 저장: {PLOT_PATH}")


if __name__ == "__main__":
    main()
