"""
bus_load_sweep.py의 결과(logs/bus_load_sweep.csv)를 표 이미지(PNG)로 렌더링한다.
그래프(선그래프)와 별개로, 포트폴리오/보고서에 바로 넣을 수 있는 "표" 형태 산출물이
필요해서 만들었다. 실제로 붕괴가 시작된 행(정상률이 100%보다 떨어지는 지점)을
빨갛게 강조한다 - 새로 지어낸 숫자가 아니라 bus_load_sweep.py가 실제로 측정한
CSV를 그대로 표로 옮기는 것뿐이다.
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
CSV_PATH = LOGS_DIR / "bus_load_sweep.csv"
TABLE_PATH = LOGS_DIR / "bus_load_sweep_table.png"

COLUMN_LABELS = [
    "Background\nECUs", "Requests", "Avg Response\nTime (ms)", "Success\nRate (%)",
    "NRC\n(%)", "Incomplete\nReassembly (%)", "N_Cr\nTimeout (%)", "No\nResponse (%)",
]


def main():
    if not CSV_PATH.exists():
        sys.exit(f"결과 CSV가 없습니다: {CSV_PATH}\n먼저 bus_load_sweep.py를 실행하세요.")
    df = pd.read_csv(CSV_PATH)

    # df.values.tolist()는 DataFrame 전체를 하나의 numpy dtype으로 통일해버려서
    # 정수 열(개수, 요청 수)까지 200.0처럼 소수점으로 나온다 - 열마다 원하는
    # 포맷(정수 vs 소수1자리)으로 직접 문자열을 만들어서 그 문제를 피한다.
    int_cols = {"background_ecu_count", "total_requests"}
    cell_text = []
    for _, row in df.iterrows():
        cell_text.append([
            f"{int(row[col])}" if col in int_cols else f"{row[col]:.1f}"
            for col in df.columns
        ])
    # 정상률이 100%가 아닌 행(=실제로 실패가 발생한 부하 수준)만 강조
    row_colors = ["#FFD6D6" if pos < 100.0 else "white" for pos in df["pos_rate_pct"]]

    fig, ax = plt.subplots(figsize=(14, 0.55 * (len(df) + 1) + 0.7))
    ax.axis("off")

    table = ax.table(
        cellText=cell_text,
        colLabels=COLUMN_LABELS,
        cellLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.auto_set_column_width(col=list(range(len(COLUMN_LABELS))))
    table.scale(1, 2.2)

    for col in range(len(COLUMN_LABELS)):
        header_cell = table[0, col]
        header_cell.set_facecolor("#2E5090")
        header_cell.set_text_props(color="white", weight="bold")

    for row_idx, color in enumerate(row_colors, start=1):
        for col in range(len(COLUMN_LABELS)):
            table[row_idx, col].set_facecolor(color)

    plt.title(
        "CAN Bus Load Robustness Sweep — real simulation results\n"
        "(red rows = actual failures observed, not projected)",
        fontsize=12, pad=14,
    )
    plt.tight_layout()
    plt.savefig(TABLE_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"표 이미지 저장: {TABLE_PATH}")


if __name__ == "__main__":
    main()
