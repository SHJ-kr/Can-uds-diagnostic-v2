"""
배경 ECU 트래픽 설정 GUI
--------------------------
batch_log_generator.py가 쓰는 "배경 트래픽 ECU 목록"(can_bus_arbiter.DEFAULT_BACKGROUND_NODES)을
코드를 직접 고치지 않고 화면에서 추가/삭제/수정한 뒤, 그 설정 그대로 전체 시뮬레이션
(6개 결함 시나리오 x 200건)을 실행하고 elbow plot까지 만들어 보여준다.

K(클러스터 개수) 선택과 최종 클러스터링 실행은 여기 범위 밖이다 - nrc_clustering.py의
설계 원칙대로, elbow plot을 실제로 본 뒤 사람이 --k 값을 정해서 터미널에서 따로
실행해야 한다(GUI가 K를 대신 정해버리면 "실제 그래프를 보고 정한다"는 원칙이 깨짐).
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

import batch_log_generator as batch_gen  # noqa: E402
import nrc_clustering  # noqa: E402
from can_bus_arbiter import DEFAULT_BACKGROUND_NODES  # noqa: E402

COLUMNS = ["이름", "Arbitration ID (hex)", "주기 최소(ms)", "주기 최대(ms)"]


class SimConfigGui(QtWidgets.QWidget):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("배경 ECU 트래픽 설정 & 시뮬레이션 실행")
        self.resize(760, 720)

        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel(
            "배경 ECU 목록 - 진단 통신과 무관하게 항상 트래픽을 내는 다른 ECU들.\n"
            "Arbitration ID가 낮을수록(=0에 가까울수록) 진단 프레임(요청 0x7E0 / 응답 0x7E8)보다\n"
            "우선순위가 높아서, 버스가 붐빌 때 진단 프레임을 더 자주 밀어냅니다."
        ))

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.table)

        for name, arb_id, (lo, hi) in DEFAULT_BACKGROUND_NODES:
            self._add_row(name, arb_id, lo * 1000, hi * 1000)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("+ 노드 추가")
        self.btn_remove = QtWidgets.QPushButton("선택 행 삭제")
        self.btn_reset = QtWidgets.QPushButton("기본값으로 초기화")
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addWidget(self.btn_reset)
        layout.addLayout(btn_row)

        self.btn_run = QtWidgets.QPushButton("🚀 이 배경 트래픽 설정으로 시뮬레이션 실행")
        self.btn_run.setStyleSheet("font-weight: 600; padding: 8px;")
        layout.addWidget(self.btn_run)

        layout.addWidget(QtWidgets.QLabel("진행 로그"))
        self.log_box = QtWidgets.QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(180)
        layout.addWidget(self.log_box)

        layout.addWidget(QtWidgets.QLabel(
            "Elbow plot (K는 이걸 직접 보고 정하세요 - GUI가 대신 정하지 않습니다)"
        ))
        self.elbow_label = QtWidgets.QLabel("시뮬레이션을 실행하면 여기에 표시됩니다.")
        self.elbow_label.setAlignment(QtCore.Qt.AlignCenter)
        self.elbow_label.setMinimumHeight(320)
        self.elbow_label.setStyleSheet("border: 1px solid #CFCFCF; background: white;")
        layout.addWidget(self.elbow_label)

        self.next_step_label = QtWidgets.QLabel("")
        self.next_step_label.setStyleSheet("color: #555;")
        self.next_step_label.setWordWrap(True)
        layout.addWidget(self.next_step_label)

        self.btn_add.clicked.connect(self._add_row_with_next_id)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_reset.clicked.connect(self._reset_defaults)
        self.btn_run.clicked.connect(self._on_run_clicked)
        self.log_signal.connect(self._append_log)
        self.finished_signal.connect(self._on_finished)

    # ---------------- 테이블 조작 ----------------
    def _add_row(self, name, arb_id, lo_ms, hi_ms):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
        self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"0x{arb_id:03X}"))
        self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(lo_ms)))
        self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(str(hi_ms)))

    def _used_ids(self):
        used = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if not item:
                continue
            text = item.text().strip()
            try:
                used.add(int(text, 16))
            except ValueError:
                continue
        return used

    def _add_row_with_next_id(self):
        """ID를 1(0x001)부터 순서대로 자동으로 채워서 행을 추가한다. 표준 CAN ID
        체계상 정해진 규칙은 아니고, 여러 개를 빠르게 추가할 때 손으로 16진수를
        일일이 안 쳐도 되게 하려는 편의 기능이다. 0xFF를 넘어가면(=255개 넘게
        추가하면) 표준 11비트 ID 범위(~0x7FF) 안에서 계속 이어서 매긴다."""
        used = self._used_ids()
        next_id = 1
        while next_id in used:
            next_id += 1
        next_id = min(next_id, 0x7FF)
        self._add_row(f"ECU_{next_id:03X}", next_id, 10, 20)

    def _remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _reset_defaults(self):
        self.table.setRowCount(0)
        for name, arb_id, (lo, hi) in DEFAULT_BACKGROUND_NODES:
            self._add_row(name, arb_id, lo * 1000, hi * 1000)

    # ---------------- 입력값 -> presets 변환 (검증 포함) ----------------
    def _collect_presets(self):
        presets, errors = [], []
        for row in range(self.table.rowCount()):
            def cell(col):
                item = self.table.item(row, col)
                return item.text().strip() if item else ""

            name = cell(0) or f"node{row}"
            id_text, lo_text, hi_text = cell(1), cell(2), cell(3)

            try:
                arb_id = int(id_text, 16) if id_text.lower().startswith("0x") else int(id_text, 16)
            except ValueError:
                errors.append(f"{row + 1}행: Arbitration ID '{id_text}'를 16진수로 해석할 수 없습니다.")
                continue
            if not (0 <= arb_id <= 0x7FF):
                errors.append(f"{row + 1}행: 표준 CAN ID 범위(0~0x7FF)를 벗어났습니다: 0x{arb_id:X}")
                continue

            try:
                lo_ms, hi_ms = float(lo_text), float(hi_text)
            except ValueError:
                errors.append(f"{row + 1}행: 주기 값이 숫자가 아닙니다 ('{lo_text}', '{hi_text}').")
                continue
            if lo_ms < 0 or hi_ms < lo_ms:
                errors.append(f"{row + 1}행: 주기 범위가 올바르지 않습니다 (0 <= 최소 <= 최대).")
                continue

            presets.append((name, arb_id, (lo_ms / 1000.0, hi_ms / 1000.0)))

        ids = [p[1] for p in presets]
        if len(ids) != len(set(ids)):
            errors.append("같은 Arbitration ID를 가진 노드가 두 개 이상 있습니다 - 서로 구분이 안 됩니다.")

        return presets, errors

    # ---------------- 실행 ----------------
    def _on_run_clicked(self):
        presets, errors = self._collect_presets()
        if errors:
            QtWidgets.QMessageBox.critical(self, "입력 오류", "\n".join(errors))
            return

        self.btn_run.setEnabled(False)
        self.log_box.clear()
        self.next_step_label.setText("")
        self.elbow_label.setPixmap(QtGui.QPixmap())
        self.elbow_label.setText("실행 중...")

        threading.Thread(target=self._run_pipeline, args=(presets,), daemon=True).start()

    def _run_pipeline(self, presets):
        try:
            self.log_signal.emit(f"배경 ECU {len(presets)}개 설정으로 시뮬레이션 시작...\n")
            batch_gen.main(background_presets=presets, progress_callback=self.log_signal.emit)

            self.log_signal.emit("\nElbow 계산 중 (K 결정은 여기서 안 함 - 그래프 보고 직접 정하세요)...")
            df = nrc_clustering.load_data()
            df_clustered = nrc_clustering.drop_unmeasured_rows(df)
            ct = nrc_clustering.build_pipeline()
            X = ct.fit_transform(df_clustered)
            ks, inertias = nrc_clustering.compute_elbow(X)
            nrc_clustering.LOGS_DIR.mkdir(parents=True, exist_ok=True)
            nrc_clustering.plot_elbow(ks, inertias, nrc_clustering.ELBOW_PLOT_PATH)

            self.finished_signal.emit(True, str(nrc_clustering.ELBOW_PLOT_PATH))
        except Exception as e:  # noqa: BLE001 - GUI 스레드에 무조건 에러를 보고해야 함
            self.finished_signal.emit(False, f"{type(e).__name__}: {e}")

    # ---------------- UI 갱신 (메인 스레드에서만) ----------------
    def _append_log(self, msg):
        self.log_box.append(msg)

    def _on_finished(self, success, message):
        self.btn_run.setEnabled(True)
        if not success:
            self.elbow_label.setText("실행 중 오류가 발생했습니다.")
            QtWidgets.QMessageBox.critical(self, "오류", message)
            return

        self.log_signal.emit(f"\n완료. Elbow plot: {message}")
        pixmap = QtGui.QPixmap(message)
        self.elbow_label.setPixmap(
            pixmap.scaledToWidth(700, QtCore.Qt.SmoothTransformation)
        )
        self.next_step_label.setText(
            "다음 단계: 위 그래프에서 꺾이는 지점(K)을 직접 확인한 뒤, 터미널에서\n"
            "python nrc_clustering.py --k <선택한 K>  를 실행하면 최종 클러스터 결과와 "
            "산점도(logs/cluster_scatter.png)가 만들어집니다."
        )


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    gui = SimConfigGui()
    gui.show()
    sys.exit(app.exec_())
