"""
실제 진단 로그(logs/real_diagnostic_log.csv) 기반 NRC/이벤트 클러스터링
-------------------------------------------------------------------------
batch_log_generator.py가 실제로 VirtualECU를 구동해서 만든 CSV만 입력으로 받는다.
합성 데이터나 임의로 지어낸 값은 쓰지 않는다 — response_time_ms가 아예 측정되지 않은
이벤트(NO_RESPONSE/N_CR_TIMEOUT)는 평균값 등으로 채워 넣지 않고 클러스터링에서 제외한다
(ECU가 낸 적 없는 숫자를 만들어내는 것이므로).

K(클러스터 개수)는 --k로 넘겨준다. 기본값이 없다 — 먼저 --k 없이 실행해서
logs/elbow_plot.png를 만들고, 그 그래프를 실제로 눈으로 보고 K를 정한 뒤 다시
--k <값>으로 실행해야 한다. 미리 하드코딩하지 않는다.
"""
import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 헤드리스 환경에서도 안전하게 그림만 파일로 저장
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.decomposition import PCA
from sklearn.preprocessing import OneHotEncoder, StandardScaler

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
CSV_PATH = LOGS_DIR / "real_diagnostic_log.csv"
ELBOW_PLOT_PATH = LOGS_DIR / "elbow_plot.png"
SCATTER_PLOT_PATH = LOGS_DIR / "cluster_scatter.png"
K_RANGE = range(1, 11)


def load_data():
    if not CSV_PATH.exists():
        sys.exit(f"진단 로그가 없습니다: {CSV_PATH}\n먼저 batch_log_generator.py를 실행하세요.")
    return pd.read_csv(CSV_PATH)


def drop_unmeasured_rows(df):
    """response_time_ms가 비어있는 행(응답 자체가 없었던 이벤트)은 클러스터링에서 제외.
    빈칸을 평균/0 등으로 채우면 ECU가 실제로 낸 적 없는 값을 지어내는 것이므로 하지 않는다.
    """
    mask = df["response_time_ms"].notna()
    dropped = int((~mask).sum())
    print(f"response_time_ms가 없는 이벤트 {dropped}건을 클러스터링에서 제외합니다"
          f"(NO_RESPONSE/N_CR_TIMEOUT 등 - 애초에 응답시간이 측정된 적 없는 이벤트).")
    if dropped:
        print("  제외된 이벤트의 event_type 분포:")
        print(df.loc[~mask, "event_type"].value_counts().to_string())
    return df.loc[mask].reset_index(drop=True)


def build_pipeline():
    # code 컬럼은 의도적으로 제외한다: NRC 이벤트의 code는 사실상 event_type="NRC"의
    # 세부값이라 event_type과 의미가 겹친다. 둘 다 넣으면 같은 신호("이 요청이 실패했다")를
    # 두 번 반영하게 되어 거리 계산이 왜곡될 수 있다. event_type만으로 그 신호를 대표시킨다.
    return ColumnTransformer(
        transformers=[
            ("response_time", StandardScaler(), ["response_time_ms"]),
            ("categorical", OneHotEncoder(handle_unknown="ignore"), ["req_sid", "event_type"]),
        ]
    )


def compute_elbow(X):
    inertias = []
    for k in K_RANGE:
        km = KMeans(n_clusters=k, n_init=10, random_state=42)
        km.fit(X)
        inertias.append(km.inertia_)
    return list(K_RANGE), inertias


def plot_elbow(ks, inertias, path):
    # matplotlib 기본 폰트(DejaVu Sans)에 한글 글리프가 없어서 한글로 쓰면 PNG에
    # 네모(tofu)로 깨져 저장된다 - 그래프 텍스트는 영문으로 고정.
    plt.figure(figsize=(7, 5))
    plt.plot(ks, inertias, marker="o")
    plt.xlabel("K (number of clusters)")
    plt.ylabel("Inertia")
    plt.title("Elbow Method (from real simulated diagnostic log)")
    plt.xticks(list(ks))
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def summarize_clusters(df, labels):
    df = df.copy()
    df["cluster"] = labels
    for c in sorted(df["cluster"].unique()):
        sub = df[df["cluster"] == c]
        print(f"\n--- Cluster {c} ({len(sub)}건) ---")
        print(f"  평균 response_time_ms: {sub['response_time_ms'].mean():.2f}")
        print("  event_type 분포 (%):")
        print(sub["event_type"].value_counts(normalize=True).mul(100).round(1).to_string())
        print("  req_sid 분포 (%):")
        print(sub["req_sid"].value_counts(normalize=True).mul(100).round(1).to_string())


def plot_scatter(coords, labels, path, explained_variance):
    plt.figure(figsize=(8, 6))
    scatter = plt.scatter(coords[:, 0], coords[:, 1], c=labels, cmap="tab10", alpha=0.7, s=20)
    plt.xlabel("PCA 1")
    plt.ylabel("PCA 2")
    plt.title(f"KMeans Clusters (PCA 2D projection, explained variance={explained_variance:.1%})")
    plt.legend(*scatter.legend_elements(), title="Cluster")
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="실제 진단 로그(logs/real_diagnostic_log.csv) 기반 KMeans 클러스터링"
    )
    parser.add_argument(
        "--k",
        type=int,
        default=None,
        help="KMeans 클러스터 개수. 생략하면 elbow_plot.png만 생성하고 종료한다 - "
        "그래프를 실제로 열어서 꺾이는 지점을 보고 K를 정한 뒤 다시 --k로 실행하라.",
    )
    args = parser.parse_args()

    df = load_data()
    print(f"로드된 이벤트: {len(df)}건 ({CSV_PATH})")
    df_clustered = drop_unmeasured_rows(df)
    print(f"클러스터링에 실제 사용되는 이벤트: {len(df_clustered)}건")

    if df_clustered.empty:
        sys.exit("클러스터링할 이벤트가 없습니다 (모든 행에 response_time_ms가 없음).")

    ct = build_pipeline()
    X = ct.fit_transform(df_clustered)

    ks, inertias = compute_elbow(X)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    plot_elbow(ks, inertias, ELBOW_PLOT_PATH)
    print(f"\nElbow plot 저장: {ELBOW_PLOT_PATH}")
    for k, inertia in zip(ks, inertias):
        print(f"  K={k}: inertia={inertia:.2f}")

    if args.k is None:
        print("\n--k 가 지정되지 않았습니다. 위 elbow_plot.png를 실제로 열어서 꺾이는 지점을 보고,")
        print("python nrc_clustering.py --k <선택한 K> 로 다시 실행하세요.")
        return

    model = KMeans(n_clusters=args.k, n_init=10, random_state=42)
    labels = model.fit_predict(X)
    summarize_clusters(df_clustered, labels)

    X_dense = X.toarray() if hasattr(X, "toarray") else X
    pca = PCA(n_components=2, random_state=42)
    coords = pca.fit_transform(X_dense)
    explained = pca.explained_variance_ratio_.sum()
    print(f"\nPCA 2D 설명분산비 합계: {explained:.1%}")
    plot_scatter(coords, labels, SCATTER_PLOT_PATH, explained)
    print(f"Cluster scatter plot 저장: {SCATTER_PLOT_PATH}")


if __name__ == "__main__":
    main()
