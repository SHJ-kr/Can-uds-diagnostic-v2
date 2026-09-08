import sys
import can
import time
import csv
import os
import struct  # [신규] 바이트 패킹(pack)을 위해 import
from PyQt5 import QtWidgets, QtCore, QtGui


# ==================== [신규] ISO 14229-1 표준 NRC 코드 테이블 ====================
# TC375(서버)는 현재 0x11 / 0x13 / 0x31 만 실제로 보내지만,
# 클라이언트(Python)는 향후 서버가 다른 NRC를 보내도 바로 해석할 수 있도록
# 표준 NRC 전체를 미리 갖춰둔다.
NRC_CODES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
    0x92: "voltageTooHigh",
    0x93: "voltageTooLow",
}


def decode_nrc(nrc_code):
    """NRC 헥스 코드 -> 사람이 읽을 수 있는 설명. 순수 함수라 GUI 없이도 테스트 가능."""
    if nrc_code is None:
        return "Unknown(NRC 없음)"
    return NRC_CODES.get(nrc_code, f"Unknown/ManufacturerSpecific (0x{nrc_code:02X})")
# ================================================================================


class ECUInfoDialog(QtWidgets.QDialog):
    """
    DID 0x0005 (ECU Info) 쓰기 전용 다이얼로그 (이전과 동일)
    """
    def __init__(self, current_info, parent=None):
        super().__init__(parent)
        self.setWindowTitle("✏️ Edit ECU Info (WriteDataByIdentifier 0x2E)")
        self.setFixedSize(450, 360)
        
        layout = QtWidgets.QGridLayout(self)
        layout.setSpacing(10)

        self.fields = {
            'VIN': (18, current_info.get('vin', '')),
            'Hardware PN': (20, current_info.get('hw', '')),
            'Software PN': (20, current_info.get('sw', '')),
            'Serial Number': (20, current_info.get('sn', '')),
            'Supplier': (20, current_info.get('supplier', ''))
        }
        
        self.editors = {}
        row = 0

        for label_text, (max_len, default_val) in self.fields.items():
            label = QtWidgets.QLabel(f"{label_text}:")
            layout.addWidget(label, row, 0, QtCore.Qt.AlignRight)

            editor = QtWidgets.QLineEdit(default_val)
            layout.addWidget(editor, row, 1)
            
            self.editors[label_text] = editor 
            row += 1

            hint_label = QtWidgets.QLabel(f"Max {max_len} bytes (ASCII)")
            hint_label.setStyleSheet("font-size: 8pt; color: #555; padding-left: 5px;")
            layout.addWidget(hint_label, row, 1, QtCore.Qt.AlignLeft)
            row += 1

        self.btn_send = QtWidgets.QPushButton("🚀 Send to ECU (0x2E)")
        self.btn_send.clicked.connect(self.validate_and_accept)
        layout.addWidget(self.btn_send, row, 0, 1, 2, QtCore.Qt.AlignCenter)
        layout.setRowStretch(row + 1, 1)

    def validate_and_accept(self):
        errors = []
        for label_text, (max_len, _) in self.fields.items():
            editor = self.editors[label_text]
            text = editor.text()
            
            try:
                byte_len = len(text.encode('ascii', errors='ignore'))
            except Exception:
                byte_len = len(text.encode('ascii', errors='replace'))

            if byte_len > max_len:
                errors.append(f"❌ '{label_text}' is too long: {byte_len} bytes (Max {max_len})")

        if errors:
            error_msg = "Please correct the following errors:\n\n" + "\n".join(errors)
            QtWidgets.QMessageBox.critical(self, "Validation Error", error_msg)
            return
        
        self.accept()

    def get_info_bytes(self):
        def to_fixed_bytes(s, length):
            b = s.encode('ascii', errors='ignore')[:length]
            return b + b'\x00' * (length - len(b))

        vin = to_fixed_bytes(self.editors['VIN'].text(), 18)
        pad = b'\x00' * 2
        hw = to_fixed_bytes(self.editors['Hardware PN'].text(), 20)
        sw = to_fixed_bytes(self.editors['Software PN'].text(), 20)
        sn = to_fixed_bytes(self.editors['Serial Number'].text(), 20)
        supplier = to_fixed_bytes(self.editors['Supplier'].text(), 20)
        return vin + pad + hw + sw + sn + supplier


class SensorConfigDialog(QtWidgets.QDialog):
    """
    [신규] DID 0x0006 (센서 설정) 쓰기 전용 다이얼로그
    """
    def __init__(self, current_config, parent=None):
        super().__init__(parent)
        # [수정] 창 제목의 "Error" -> "Pass"
        self.setWindowTitle("🔧 Edit Sensor Pass Conditions (DID 0x0006)")
        self.setFixedSize(450, 300)

        layout = QtWidgets.QVBoxLayout(self)
        
        # [수정] 메인 라벨 "ERROR" -> "PASS" (이모지 변경)
        title_label = QtWidgets.QLabel("✅ PASS CONDITION")
        title_label.setStyleSheet("font-size: 14pt; font-weight: 600; margin-bottom: 10px;")
        layout.addWidget(title_label, alignment=QtCore.Qt.AlignCenter)

        # 0~65535 (unsigned short)
        validator = QtGui.QIntValidator(0, 65535, self)

        form_layout = QtWidgets.QFormLayout()
        
        # [수정] 라벨 텍스트 변경 (Min/Max 순서가 PASS 로직과 일치함)
        # [수정] 기본값을 current_config에서 가져오도록 수정 (C코드 기본값 반영)
        self.ultra_min_edit = QtWidgets.QLineEdit(str(current_config.get('ultra_min', 0)))
        self.ultra_min_edit.setValidator(validator)
        self.ultra_max_edit = QtWidgets.QLineEdit(str(current_config.get('ultra_max', 400)))
        self.ultra_max_edit.setValidator(validator)
        form_layout.addRow("Ultrasonic Min (mm):", self.ultra_min_edit)
        form_layout.addRow("Ultrasonic Max (mm):", self.ultra_max_edit)

        self.tof_min_edit = QtWidgets.QLineEdit(str(current_config.get('tof_min', 0)))
        self.tof_min_edit.setValidator(validator)
        self.tof_max_edit = QtWidgets.QLineEdit(str(current_config.get('tof_max', 5000)))
        self.tof_max_edit.setValidator(validator)
        form_layout.addRow("ToF Min (mm):", self.tof_min_edit)
        form_layout.addRow("ToF Max (mm):", self.tof_max_edit)
        
        layout.addLayout(form_layout)
        layout.addStretch()

        self.btn_send = QtWidgets.QPushButton("🚀 Send to ECU (0x2E)")
        self.btn_send.clicked.connect(self.validate_and_accept)
        layout.addWidget(self.btn_send, alignment=QtCore.Qt.AlignCenter)

    def validate_and_accept(self):
        try:
            u_min = int(self.ultra_min_edit.text())
            u_max = int(self.ultra_max_edit.text())
            t_min = int(self.tof_min_edit.text())
            t_max = int(self.tof_max_edit.text())
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "Validation Error", "❌ All fields must be valid numbers.")
            return

        errors = []
        if u_min >= u_max:
            # Min/Max가 같을 수는 있으므로 (예: 0/0) < 로 수정
            errors.append("❌ Ultrasonic Min must be strictly less than Max.")
        if t_min >= t_max:
            errors.append("❌ ToF Min must be strictly less than Max.")

        if errors:
            QtWidgets.QMessageBox.critical(self, "Validation Error", "\n".join(errors))
            return
        
        self.accept()

    def get_config_bytes(self):
        u_min = int(self.ultra_min_edit.text())
        u_max = int(self.ultra_max_edit.text())
        t_min = int(self.tof_min_edit.text())
        t_max = int(self.tof_max_edit.text())
        
        # C 구조체: unsigned short (2B) 4개
        # Little-endian ('<'), Unsigned Short ('H') 4개 (TC375는 Little Endian)
        return struct.pack('<HHHH', u_min, u_max, t_min, t_max)  


class CANUDSGui(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CAN/UDS Diagnostic GUI")
        self.resize(1150, 820)

        self.setStyleSheet("""
            QWidget { background-color: #FAFAFF; color: #111; font-family: 'Segoe UI'; font-size: 10pt; }
            QPushButton { background-color: #E9F1FF; border: 1px solid #BBD1FF; border-radius: 6px; padding: 6px; }
            QPushButton:hover { background-color: #D9E9FF; }
            QTextEdit { background-color: #FFFFFF; border: 1px solid #CFCFCF; border-radius: 4px; font-family: Consolas; }
            QTableWidget { background-color: #FFFFFF; border: 1px solid #CFCFCF; border-radius: 4px; }
            QGroupBox { font-weight: 600; }
            QLabel.title { font-size: 12pt; font-weight: 600; }
        """)

        # [수정] 환경변수로 override 가능하게 변경 - 실물 하드웨어 기본값은 그대로 유지하면서
        # 가상 ECU 테스트 시엔 CAN_CHANNEL=vcan_test CAN_BUSTYPE=virtual 로 실행하면 됨
        self.channel = os.environ.get("CAN_CHANNEL", "PCAN_USBBUS1")
        self.bustype = os.environ.get("CAN_BUSTYPE", "pcan")
        self.bitrate = 500000
        self.req_id = 0x7E0
        self.res_id = 0x7E8

        # [신규] N_Cr 타임아웃 (ISO 15765-2 기준 N_Cr 표준값은 통상 150ms지만,
        # USB-CAN 어댑터/OS 스케줄링 지연을 감안해 여유있게 설정. 환경에 맞게 조정 필요)
        self.N_CR_TIMEOUT = 1.0
        # [신규] 매 진단 요청의 결과를 기록 - 이후 NRC 패턴 분석(K-means 등)에 사용할 로그
        # event_type: "POS" / "NRC" / "NO_RESPONSE" / "N_CR_TIMEOUT" / "INCOMPLETE_REASSEMBLY"
        self.diagnostic_log = []
        self._last_recv_status = "OK"

        # [신규] 센서 설정값 (임계값)을 GUI 내부에 저장
        # (원래는 0x22 0006으로 읽어와야 하지만, 현재는 쓰기만 구현하므로 기본값 저장)
        
        # [수정] C 코드(g_sensorThresholds)의 초기 기본값과 일치시킴
        self.current_sensor_config = {
            'ultra_min': 0,    # 2000 -> 0
            'ultra_max': 400,  # 4000 -> 400
            'tof_min': 0,      # 1000 -> 0
            'tof_max': 5000
        }

        root = QtWidgets.QVBoxLayout(self)

        # UDS 버튼
        uds_row = QtWidgets.QHBoxLayout()
        self.btn_read_ecu_info = QtWidgets.QPushButton("ℹ️ Read ECU Info (DID 0x0005)")
        self.btn_write_ecu_info = QtWidgets.QPushButton("✏️ Write (0x2E...)") # 버튼 텍스트 수정
        self.btn_read_dtc = QtWidgets.QPushButton("📖 Read DTCs (0x19)")
        self.btn_clear_dtc = QtWidgets.QPushButton("🧹 Clear DTCs (0x14)")
        self.btn_export_log = QtWidgets.QPushButton("💾 Export Diagnostic Log (CSV)")
        uds_row.addWidget(self.btn_read_ecu_info)
        uds_row.addWidget(self.btn_write_ecu_info)
        uds_row.addWidget(self.btn_read_dtc)
        uds_row.addWidget(self.btn_clear_dtc)
        uds_row.addWidget(self.btn_export_log)
        root.addLayout(uds_row)

        # ... (기존 센서 버튼, 프레임 모니터, ECU Info 카드 등 UI 정의) ...
        # (이하 UI 정의 코드는 변경 없음)
        sensor_row = QtWidgets.QHBoxLayout()
        self.btn_ultra1 = QtWidgets.QPushButton("🔹 Ultra Sensor 1 (0x0001)")
        self.btn_ultra2 = QtWidgets.QPushButton("🔹 Ultra Sensor 2 (0x0002)")
        self.btn_ultra3 = QtWidgets.QPushButton("🔹 Ultra Sensor 3 (0x0003)")
        self.btn_tof = QtWidgets.QPushButton("🔹 ToF Sensor (0x0004)")
        sensor_row.addWidget(self.btn_ultra1)
        sensor_row.addWidget(self.btn_ultra2)
        sensor_row.addWidget(self.btn_ultra3)
        sensor_row.addWidget(self.btn_tof)
        root.addLayout(sensor_row)
        frame_layout = QtWidgets.QHBoxLayout()
        self.sent_box = QtWidgets.QTextEdit(); self.sent_box.setReadOnly(True)
        self.recv_box = QtWidgets.QTextEdit(); self.recv_box.setReadOnly(True)
        left = QtWidgets.QVBoxLayout(); left.addWidget(QtWidgets.QLabel("📤 Sent Frames")); left.addWidget(self.sent_box)
        right = QtWidgets.QVBoxLayout(); right.addWidget(QtWidgets.QLabel("📥 Received Frames")); right.addWidget(self.recv_box)
        frame_layout.addLayout(left); frame_layout.addLayout(right)
        root.addLayout(frame_layout)
        info_card = QtWidgets.QGroupBox("🧾 ECU Information")
        info_layout = QtWidgets.QFormLayout()
        self.lbl_vin = QtWidgets.QLabel("-")
        self.lbl_hw = QtWidgets.QLabel("-")
        self.lbl_sw = QtWidgets.QLabel("-")
        self.lbl_sn = QtWidgets.QLabel("-")
        self.lbl_supplier = QtWidgets.QLabel("-")
        info_layout.addRow("VIN:", self.lbl_vin)
        info_layout.addRow("HW:", self.lbl_hw)
        info_layout.addRow("SW:", self.lbl_sw)
        info_layout.addRow("SN:", self.lbl_sn)
        info_layout.addRow("Supplier:", self.lbl_supplier)
        info_card.setLayout(info_layout)
        root.addWidget(info_card)
        sensor_card = QtWidgets.QGroupBox("📡 Sensor Values")
        self.sensor_result = QtWidgets.QTextEdit(); self.sensor_result.setReadOnly(True)
        scv = QtWidgets.QVBoxLayout(); scv.addWidget(self.sensor_result); sensor_card.setLayout(scv)
        root.addWidget(sensor_card)
        
        # --- [DTC 테이블 수정] ---
        dtc_card = QtWidgets.QGroupBox("⚙️ Diagnostic Trouble Codes (DTC)")
        dtc_layout = QtWidgets.QVBoxLayout()
        # [수정] 열 개수 4 -> 5
        self.dtc_table = QtWidgets.QTableWidget(0, 5) 
        # [수정] 헤더에 "DTC 설명" 추가
        self.dtc_table.setHorizontalHeaderLabels(["#", "DTC 코드", "DTC 설명 (발생 센서)", "상태값", "상태 설명"])
        
        # [수정] 설명 열(2)이 가장 넓도록 설정
        self.dtc_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.dtc_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.dtc_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.dtc_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch) # 설명 열
        self.dtc_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        self.dtc_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        
        dtc_layout.addWidget(self.dtc_table)
        # --- [DTC 테이블 수정 끝] ---
        
        dtc_card.setLayout(dtc_layout)
        root.addWidget(dtc_card)
        self.log_box = QtWidgets.QTextEdit(); self.log_box.setReadOnly(True)
        self.result_box = QtWidgets.QTextEdit(); self.result_box.setReadOnly(True)
        root.addWidget(QtWidgets.QLabel("🪶 Log Output"))
        root.addWidget(self.log_box)
        root.addWidget(QtWidgets.QLabel("📊 Diagnostic Results"))
        root.addWidget(self.result_box)
        # (여기까지 UI 정의 코드)

        try:
            self.bus = can.interface.Bus(channel=self.channel, bustype=self.bustype, bitrate=self.bitrate)
            self.log("✅ PCAN USB connected successfully.")
        except Exception as e:
            self.bus = None
            self.log(f"❌ CAN init failed: {e}")

        # 버튼 연결
        self.btn_ultra1.clicked.connect(lambda: self.read_by_did(0x0001))
        self.btn_ultra2.clicked.connect(lambda: self.read_by_did(0x0002))
        self.btn_ultra3.clicked.connect(lambda: self.read_by_did(0x0003))
        self.btn_tof.clicked.connect(lambda: self.read_by_did(0x0004))
        self.btn_read_ecu_info.clicked.connect(lambda: self.read_by_did(0x0005))
        
        # [수정] Write 버튼은 선택창을 띄우는 함수(start_write_process)에 연결
        self.btn_write_ecu_info.clicked.connect(self.start_write_process)
        
        self.btn_read_dtc.clicked.connect(self.read_dtc)
        self.btn_clear_dtc.clicked.connect(self.clear_dtc)
        self.btn_export_log.clicked.connect(self.export_diagnostic_log)

    # ---------------- 유틸 ----------------
    def log(self, text):
        self.log_box.append(text)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())

    def recv_all(self, timeout=2.0):
        """
        [수정] 기존에는 res_id로 오는 프레임을 timeout 동안 그냥 모으기만 했음.
        아래 3가지를 추가로 감시한다:
          1) N_Cr 타임아웃: FF 수신 후 다음 CF가 self.N_CR_TIMEOUT 안에 안 오면 즉시 중단
          2) NRC 0x78 (ResponsePending): ECU가 "처리 중이니 더 기다려달라"고 하면
             표준(P2*Client)대로 데드라인을 연장하고 계속 대기
          3) 응답이 아예 없으면 NO_RESPONSE로 상태를 남김 (NRC와 명확히 구분하기 위함)
        self._last_recv_status 에 최종 상태를 남겨서, 호출한 쪽에서 로그로 기록할 수 있게 한다.
        """
        frames = []
        start = time.time()
        deadline = start + timeout
        got_ff = False
        total_len = 0
        received_len = 0
        last_frame_time = start
        self._last_recv_status = "OK"

        while time.time() < deadline:
            msg = self.bus.recv(timeout=0.05) if self.bus else None

            if msg and msg.arbitration_id == self.res_id:
                pci_type = msg.data[0] >> 4

                # [신규] NRC 0x78 (ResponsePending) - 아직 실패가 아니라 "더 기다려라"는 뜻
                if pci_type == 0 and len(msg.data) >= 4 and msg.data[1] == 0x7F and msg.data[3] == 0x78:
                    self.log("⏳ NRC 0x78 (ResponsePending) 수신 - 표준에 따라 대기시간 연장")
                    deadline = time.time() + timeout  # P2*Client 만큼 재연장
                    last_frame_time = time.time()
                    continue

                frames.append(msg)
                last_frame_time = time.time()

                if pci_type == 1:  # First Frame
                    got_ff = True
                    total_len = ((msg.data[0] & 0x0F) << 8) | msg.data[1]
                    received_len = 6
                    # [신규] FF를 받았으면 표준대로 즉시 FlowControl(CTS)을 보내줘야
                    # 상대(ECU)가 CF를 이어서 보낼 수 있음 (기존엔 이게 없었음)
                    self._send_flow_control_to_ecu()
                elif pci_type == 2 and got_ff:  # Consecutive Frame
                    bytes_left = total_len - received_len
                    received_len += min(7, bytes_left) if bytes_left > 0 else 0
                    if received_len >= total_len:
                        got_ff = False  # 재조립 완료로 판단, N_Cr 감시 종료
            else:
                # [신규] N_Cr 타임아웃 감시: FF는 받았는데 다음 CF가 안 오는 경우
                if got_ff and (time.time() - last_frame_time) > self.N_CR_TIMEOUT:
                    self.log(
                        f"❌ N_Cr Timeout! 마지막 프레임 이후 {self.N_CR_TIMEOUT:.1f}s 동안 "
                        f"다음 CF 없음 ({received_len}/{total_len} bytes만 수신) → 수신 중단"
                    )
                    self._last_recv_status = "N_CR_TIMEOUT"
                    return frames

        if not frames:
            self._last_recv_status = "NO_RESPONSE"

        return frames

    def _record_diagnostic_event(self, req_sid, did, event_type, code=None, response_time_ms=None):
        """[신규] 진단 이벤트 1건을 구조화된 형태로 기록 (추후 NRC 클러스터링 등에 사용)"""
        self.diagnostic_log.append({
            "timestamp": time.time(),
            "req_sid": f"0x{req_sid:02X}" if req_sid is not None else "",
            "did": f"0x{did:04X}" if did else "",
            "event_type": event_type,
            "code": (f"0x{code:02X}" if isinstance(code, int) else (code or "")),
            "response_time_ms": round(response_time_ms, 1) if response_time_ms is not None else "",
        })

    def export_diagnostic_log(self):
        """[신규] 지금까지 쌓인 진단 로그를 CSV로 저장"""
        if not self.diagnostic_log:
            self.log("⚠️ 저장할 진단 로그가 없습니다.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Diagnostic Log", "diagnostic_log.csv", "CSV Files (*.csv)"
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["timestamp", "req_sid", "did", "event_type", "code", "response_time_ms"]
            )
            writer.writeheader()
            writer.writerows(self.diagnostic_log)
        self.log(f"💾 진단 로그 {len(self.diagnostic_log)}건을 {path}에 저장했습니다.")

    def _send_flow_control_to_ecu(self):
        """
        [신규] First Frame을 받았을 때 ECU에게 보내는 FlowControl(CTS).
        req_id로 보낸다 - 클라이언트가 평소 요청을 보내는 그 ID로, 이번엔 제어 프레임을 보내는 것.
        FS=0(ContinueToSend), BS=0(끝까지 한번에), STmin=0(간격 제한 없음)
        """
        if not self.bus:
            return
        fc = can.Message(arbitration_id=self.req_id, data=[0x30, 0x00, 0x00, 0, 0, 0, 0, 0], is_extended_id=False)
        self.bus.send(fc)
        self.sent_box.append("30 00 00 00 00 00 00 00  (FlowControl)")

    def show_frames(self, frames):
        for f in frames:
            self.recv_box.append(f"ID:{hex(f.arbitration_id)} DATA: {' '.join(f'{b:02X}' for b in f.data)}")
        self.recv_box.verticalScrollBar().setValue(self.recv_box.verticalScrollBar().maximum())

    # ---------------- 기능: 0x22 (Read) ----------------
    def read_by_did(self, did):
        if not self.bus:
            self.log("⚠️ CAN bus not ready")
            return

        did_h = (did >> 8) & 0xFF
        did_l = did & 0xFF
        tx = [0x03, 0x22, did_h, did_l, 0, 0, 0, 0]
        msg = can.Message(arbitration_id=self.req_id, data=tx, is_extended_id=False)
        self.bus.send(msg)
        self.sent_box.append(" ".join(f"{b:02X}" for b in tx))
        self.log(f"▶ Sent ReadDataByIdentifier DID=0x{did:04X}")

        t_send = time.time()
        frames = self.recv_all(timeout=3.0)
        response_time_ms = (time.time() - t_send) * 1000

        if frames:
            self.show_frames(frames)
            self.parse_uds_response(frames, 0x22, did, response_time_ms=response_time_ms)
        else:
            # [신규] 무응답/N_Cr 타임아웃도 NRC와 구분되는 별도 이벤트로 기록
            self.log(f"⚠️ No response for DID=0x{did:04X} ({self._last_recv_status})")
            self._record_diagnostic_event(0x22, did, self._last_recv_status, response_time_ms=response_time_ms)

    # ---------------- 기능: 0x2E (Write) - 로직 분리 ----------------

    def start_write_process(self):
        """
        [신규] 'Write' 버튼 클릭 시 어떤 작업을 할지 선택하는 팝업창 표시
        """
        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("Select Write Operation")
        msg_box.setText("Which information would you like to write?")
        btn_ecu = msg_box.addButton("✏️ ECU Info (DID 0x0005)", QtWidgets.QMessageBox.ActionRole)
        btn_sensor = msg_box.addButton("🔧 Sensor Conditions (DID 0x0006)", QtWidgets.QMessageBox.ActionRole)
        msg_box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        msg_box.exec_()

        clicked_button = msg_box.clickedButton()

        if clicked_button == btn_ecu:
            self.write_ecu_info_dialog()
        elif clicked_button == btn_sensor:
            self.write_sensor_config_dialog()

    def write_ecu_info_dialog(self):
        """
        [수정] 기존 write_ecu_info 함수 -> ECU Info 전용 다이얼로그 호출
        """
        current_info = {
            'vin': self.lbl_vin.text() if self.lbl_vin.text() != '-' else '',
            'hw': self.lbl_hw.text() if self.lbl_hw.text() != '-' else '',
            'sw': self.lbl_sw.text() if self.lbl_sw.text() != '-' else '',
            'sn': self.lbl_sn.text() if self.lbl_sn.text() != '-' else '',
            'supplier': self.lbl_supplier.text() if self.lbl_supplier.text() != '-' else ''
        }
        
        dialog = ECUInfoDialog(current_info, self)
        
        if dialog.exec_():
            data = dialog.get_info_bytes()
            # ECU Info는 데이터가 길어서 TP(Transport Protocol) 전송 필요
            self.send_uds_tp_write(0x0005, data)

    def write_sensor_config_dialog(self):
        """
        [신규] 센서 설정 다이얼로그를 띄우고 전송하는 함수
        """
        dialog = SensorConfigDialog(self.current_sensor_config, self)
        
        if dialog.exec_():
            data = dialog.get_config_bytes() # 8 bytes
            
            # [수정] 전송 성공 시, GUI 내부 변수도 업데이트
            # (사용자가 다음에 창을 열 때 이 값이 기본값이 됨)
            self.current_sensor_config['ultra_min'] = int(dialog.ultra_min_edit.text())
            self.current_sensor_config['ultra_max'] = int(dialog.ultra_max_edit.text())
            self.current_sensor_config['tof_min'] = int(dialog.tof_min_edit.text())
            self.current_sensor_config['tof_max'] = int(dialog.tof_max_edit.text())
            
            # [주석 수정] UDS 페이로드(SID+DID+Data = 3+8=11 bytes)가 
            # 7바이트를 초과하므로 TP 전송이 필요함.
            self.send_uds_tp_write(0x0006, data)

    def send_uds_sf_write(self, did, data):
        """
        [신규] Single Frame Write 전송 함수 (짧은 데이터, 8바이트 미만)
        """
        did_h = (did >> 8) & 0xFF
        did_l = did & 0xFF
        
        # PCI(len+3) | SID | DID H | DID L | data...
        payload_len = 3 + len(data)
        if payload_len > 7:
            self.log("❌ SF Error: Data too long for Single Frame.")
            return

        tx = bytearray(8)
        tx[0] = payload_len
        tx[1] = 0x2E
        tx[2] = did_h
        tx[3] = did_l
        tx[4:4+len(data)] = data
        
        self.bus.send(can.Message(arbitration_id=self.req_id, data=tx, is_extended_id=False))
        self.sent_box.append(" ".join(f"{b:02X}" for b in tx))
        self.log(f"🚀 Sent Write (SF) DID=0x{did:04X}, len={len(data)}")
        
        self.wait_for_write_ack(did) # ACK 대기 및 확인

    def send_uds_tp_write(self, did, data):
        """
        [신규] Transport Protocol Write 전송 함수 (긴 데이터 - 기존 로직 재활용)
        """
        did_h, did_l = (did >> 8) & 0xFF, did & 0xFF
        uds_data = bytearray([0x2E, did_h, did_l]) + data
        total_len = len(uds_data)
        self.log(f"🚀 Sending {total_len} bytes (DID 0x{did:04X}) via TP...")

        # First Frame
        ff = bytearray(8)
        ff[0] = 0x10 | ((total_len >> 8) & 0x0F)
        ff[1] = total_len & 0xFF
        ff[2:8] = uds_data[:6]
        self.bus.send(can.Message(arbitration_id=self.req_id, data=ff, is_extended_id=False))
        self.sent_box.append(" ".join(f"{b:02X}" for b in ff))

        # ▶ FlowControl(0x30) 기다리기
        fc = None
        t0 = time.time()
        while time.time() - t0 < 2.0:
            m = self.bus.recv(timeout=0.1)
            if m and m.arbitration_id == self.res_id and (m.data[0] >> 4) == 0x3:
                fc = m.data
                break
        if not fc:
            self.log("⚠️ FlowControl(0x30) 미수신 → 전송 중단")
            return

        fs, bs, stmin = fc[0] & 0x0F, fc[1], fc[2]
        if fs != 0x0: # 0x0 = CTS
            self.log(f"⚠️ FC FS!=CTS (FS=0x{fs:02X})")
            return
        gap_sec = (stmin/1000.0) if stmin <= 0x7F else ((stmin-0xF0)/10000.0 if 0xF1 <= stmin <= 0xF9 else 0.0)

        # ▶ CF 전송
        sent, sn, blk = 6, 1, 0
        while sent < total_len:
            cf = bytearray(8); cf[0] = 0x20 | (sn & 0x0F)
            # 마지막 프레임이 7바이트 미만일 수 있으므로 정확히 채움
            payload_size = min(7, total_len - sent)
            cf[1:1+payload_size] = uds_data[sent:sent+payload_size]
            sent += payload_size
                
            self.bus.send(can.Message(arbitration_id=self.req_id, data=cf, is_extended_id=False))
            self.sent_box.append(" ".join(f"{b:02X}" for b in cf))
            sn = (sn + 1) % 16; blk += 1
            time.sleep(gap_sec if gap_sec > 0 else 0.0)

            if bs and blk >= bs and sent < total_len:
                blk = 0
                fc = None; t1 = time.time()
                while time.time() - t1 < 1.0:
                    m = self.bus.recv(timeout=0.1)
                    if m and m.arbitration_id == self.res_id and (m.data[0] >> 4) == 0x3:
                        fc = m.data; break
                if not fc: 
                    self.log("⚠️ 다음 블록 FC 미수신"); return
        
        self.wait_for_write_ack(did) # ACK 대기 및 확인

    def wait_for_write_ack(self, did):
        """
        [신규] Write Positive Response(0x6E)를 기다리는 공통 함수
        """
        self.log(f"⏳ Waiting for 0x6E (Write ACK for DID 0x{did:04X})...")
        did_h, did_l = (did >> 8) & 0xFF, did & 0xFF

        t_send = time.time()
        frames = self.recv_all(timeout=3.0)
        response_time_ms = (time.time() - t_send) * 1000

        if frames:
            self.show_frames(frames)
            # SF 응답: [0x03, 0x6E, DID_H, DID_L]
            ack_ok = any(
                (len(f.data) >= 4 and f.data[0] == 0x03 and f.data[1] == 0x6E and f.data[2] == did_h and f.data[3] == did_l)
                for f in frames
            )
            if ack_ok:
                self.log(f"✅ Write ACK OK for DID 0x{did:04X}.")
                self._record_diagnostic_event(0x2E, did, "POS", 0x6E, response_time_ms)
                # ECU Info (0x0005)를 쓴 경우에만 화면 갱신을 위해 Read 실행
                if did == 0x0005:
                    self.log("... Verify by reading...")
                    self.read_by_did(0x0005)
            else:
                # [신규] ACK가 아니라 Negative Response(0x7F)가 왔는지 확인해서 디코딩
                neg = next((f for f in frames if len(f.data) >= 4 and f.data[0] == 0x03 and f.data[1] == 0x7F), None)
                if neg:
                    nrc_code = neg.data[3]
                    desc = decode_nrc(nrc_code)
                    self.log(f"⛔ Negative Response for Write DID=0x{did:04X}: NRC=0x{nrc_code:02X} ({desc})")
                    self.result_box.append(f"⛔ NRC 0x{nrc_code:02X}: {desc}")
                    self._record_diagnostic_event(0x2E, did, "NRC", nrc_code, response_time_ms)
                else:
                    self.result_box.append(f"⚠️ 0x6E ACK for DID 0x{did:04X} not found.")
                    self._record_diagnostic_event(0x2E, did, "UNEXPECTED", None, response_time_ms)
        else:
            self.result_box.append(f"⚠️ No response received. ({self._last_recv_status})")
            self._record_diagnostic_event(0x2E, did, self._last_recv_status, response_time_ms=response_time_ms)

            
    # ---------------- 기능: 0x19 (Read DTC) ----------------
    def read_dtc(self):
        tx = [0x03, 0x19, 0x02, 0xFF, 0, 0, 0, 0]
        msg = can.Message(arbitration_id=self.req_id, data=tx, is_extended_id=False)
        self.bus.send(msg)
        self.sent_box.append(" ".join(f"{b:02X}" for b in tx))
        self.log("▶ Sent ReadDTCInformation")

        t_send = time.time()
        frames = self.recv_all(timeout=3.0)
        response_time_ms = (time.time() - t_send) * 1000

        if frames:
            self.show_frames(frames)
            self.parse_uds_response(frames, 0x19, 0, response_time_ms=response_time_ms)
        else:
            self.log(f"⚠️ No response for ReadDTC ({self._last_recv_status})")
            self._record_diagnostic_event(0x19, None, self._last_recv_status, response_time_ms=response_time_ms)

    # ---------------- 기능: 0x14 (Clear DTC) ----------------
    def clear_dtc(self):
        tx = [0x02, 0x14, 0xFF, 0, 0, 0, 0, 0]
        msg = can.Message(arbitration_id=self.req_id, data=tx, is_extended_id=False)
        self.bus.send(msg)
        self.sent_box.append(" ".join(f"{b:02X}" for b in tx))
        self.log("▶ Sent ClearDiagnosticInformation")
        frames = self.recv_all(timeout=2.0)
        if frames:
            self.show_frames(frames)
            # 0x54 (Positive) 응답이 있는지 명시적으로 확인
            is_cleared = any(f.data[0] == 0x02 and f.data[1] == 0x54 for f in frames)
            if is_cleared:
                self.log("✅ DTC Clear acknowledged (0x54). Reading DTCs again...")
                # 확인을 위해 즉시 DTC 재조회
                self.read_dtc() 
            else:
                self.log("⚠️ DTC Clear response 0x54 not found.")
        else:
            self.log("⚠️ No response for ClearDTC.")


    # ---------------- TP 파서 ----------------
    def parse_uds_response(self, frames, req_sid, did, response_time_ms=None):
        payload = bytearray()
        total_len = 0
        sn_expected = 1
        pos_sid = (req_sid + 0x40) & 0xFF
        got_ff = False

        for f in frames:
            d = f.data
            pci_type = d[0] >> 4

            if pci_type == 3:  # FC
                continue # FC는 수신 시 파싱에서는 무시

            if pci_type == 0:  # SF
                sf_len = d[0] & 0x0F
                # SF는 0x6E(Write Ack)일 수도, 0x62(Read Ack)일 수도, 0x7F(Negative Response)일 수도 있음
                # [수정] 기존엔 d[1]==pos_sid 인 경우만 담아서, Negative Response(0x7F)가
                # 여기서 그냥 버려지고 뒤에서 디코딩할 기회조차 없었음 → 0x7F도 통과시키도록 수정
                if d[1] == pos_sid or d[1] == 0x7F:
                    payload.extend(d[1:1+sf_len])
                    self.log(f"📥 Single Frame received ({sf_len} bytes).")
                continue

            if pci_type == 1:  # FF
                total_len = ((d[0] & 0x0F) << 8) | d[1]
                payload.extend(d[2:8])
                sn_expected = 1
                got_ff = True
                self.log(f"📦 First Frame (len={total_len}) received.")
                continue

            if pci_type == 2 and got_ff:  # CF
                sn = d[0] & 0x0F
                if sn != (sn_expected & 0x0F):
                    self.log(f"⚠️ SeqErr: expected SN={sn_expected}, got={sn}")
                sn_expected = (sn_expected + 1) % 16
                
                # 마지막 프레임 처리: total_len 기준으로 정확히 파싱
                bytes_left = total_len - len(payload)
                bytes_to_copy = min(7, bytes_left)
                if bytes_to_copy > 0:
                    payload.extend(d[1:1+bytes_to_copy])
                
                if len(payload) >= total_len:
                    got_ff = False # 수신 완료
                
                continue

        if not payload:
            self.log("⚠️ No valid UDS Read payload collected.")
            self._record_diagnostic_event(req_sid, did, "NO_RESPONSE", response_time_ms=response_time_ms)
            return

        # [신규] 재조립 미완료 체크: FF가 예고한 total_len 만큼 CF가 다 안 모인 경우
        # (타임아웃 또는 프레임 누락으로 recv_all이 중간에 끊긴 경우) 조용히 넘어가지 않고 명시적으로 경고
        if total_len and len(payload) < total_len:
            self.log(
                f"⚠️ Incomplete reassembly: {len(payload)}/{total_len} bytes only "
                f"(타임아웃 또는 프레임 누락으로 재조립 미완료 – 이 결과는 신뢰할 수 없음)"
            )
            self._record_diagnostic_event(req_sid, did, "INCOMPLETE_REASSEMBLY", response_time_ms=response_time_ms)
            return

        self.log(f"ℹ️ Reconstructed payload ({len(payload)} bytes): "
                 f"{' '.join(f'{b:02X}' for b in payload[:40])}{' ...' if len(payload) > 40 else ''}")

        uds_sid = payload[0]

        # [신규] Negative Response(0x7F) 디코딩 - 기존엔 그냥 "Unexpected SID"로만 찍고 끝났음
        if uds_sid == 0x7F:
            neg_sid = payload[1] if len(payload) > 1 else None
            nrc_code = payload[2] if len(payload) > 2 else None
            desc = decode_nrc(nrc_code)
            nrc_str = f"0x{nrc_code:02X}" if nrc_code is not None else "?"
            neg_sid_str = f"0x{neg_sid:02X}" if neg_sid is not None else "?"
            self.log(f"⛔ Negative Response: 요청 SID={neg_sid_str} NRC={nrc_str} ({desc})")
            self.result_box.append(f"⛔ NRC {nrc_str}: {desc}")
            self._record_diagnostic_event(req_sid, did, "NRC", nrc_code, response_time_ms)
            return

        if uds_sid != pos_sid:
            self.log(f"⚠️ Unexpected SID=0x{uds_sid:02X} (expected 0x{pos_sid:02X})")
            self._record_diagnostic_event(req_sid, did, "UNEXPECTED_SID", uds_sid, response_time_ms)
            return

        # [신규] 정상 응답도 로그에 남김 (POS 이벤트 - NRC/무응답과 비교 기준이 됨)
        self._record_diagnostic_event(req_sid, did, "POS", pos_sid, response_time_ms)

        # -------- 서비스별 헤더 분리 --------
        if req_sid == 0x22: # Read
            if len(payload) < 3:
                self.log("⚠️ Payload too short for DID-based response.")
                return
            uds_did = (payload[1] << 8) | payload[2]
            data = payload[3:]
            data_len = len(data)

            # ECU Info (DID 0x0005)
            if did == 0x0005: 
                if data_len < 100:
                    self.log(f"⚠️ ECU Info payload too short ({data_len} bytes), expected 100.")
                    return

                def safe_decode(segment):
                    try:
                        return segment.split(b'\x00')[0].decode('ascii', errors='ignore')
                    except Exception:
                        return "<DecodeError>"

                vin = safe_decode(data[0:18])
                hw = safe_decode(data[20:40])
                sw = safe_decode(data[40:60])
                sn = safe_decode(data[60:80])
                supplier = safe_decode(data[80:100])

                self.lbl_vin.setText(vin)
                self.lbl_hw.setText(hw)
                self.lbl_sw.setText(sw)
                self.lbl_sn.setText(sn)
                self.lbl_supplier.setText(supplier)
                
                self.log("📗 ECU Info successfully parsed (100 bytes).")
                return

            # 센서 (DID 0x0001 ~ 0x0004)
            if did in (0x0001, 0x0002, 0x0003, 0x0004):
                if data_len >= 3:
                    val = (data[0] << 8) | data[1]
                    status_char = chr(data[2]) if 32 <= data[2] <= 126 else '?'
                    self.sensor_result.append(
                        f"✅ Sensor DID 0x{did:04X}: {val} mm (0x{val:04X}) '{status_char}'"
                    )
                    self.log(f"📘 Sensor data decoded (Result={status_char}).")
                else:
                    self.log(f"⚠️ Invalid sensor payload length ({data_len}).")
                return
            
            # TODO: DID 0x0006 (센서 설정값 읽기) 파싱 로직 추가
            
            return

        elif req_sid == 0x19: # Read DTC
            if len(payload) < 2:
                self.log("⚠️ DTC payload too short.")
                return
            subfunc = payload[1]
            data = payload[2:]  # status mask + entries...
            data_len = len(data)

            # DTC 없음
            if data_len <= 1:
                self.result_box.append("✅ DTC 없음 (No Diagnostic Trouble Code Stored)")
                self.update_dtc_table([])
                self.log("📙 DTC information decoded (empty).")
                return

            # DTC 있음
            if data_len >= 5:
                status_mask = data[0]
                dtcs = []
                off = 1
                while off + 3 < data_len: # 4바이트(DTC 3 + Status 1)가 남아있어야 함
                    code = (data[off] << 16) | (data[off+1] << 8) | data[off+2]
                    status = data[off+3]
                    dtcs.append((code, status))
                    off += 4

                self.result_box.append(f"✅ DTC Count: {len(dtcs)}")
                for i, (code, status) in enumerate(dtcs, 1):
                    desc = self.get_dtc_status_desc(status)
                    self.result_box.append(f" DTC {i}: 0x{code:06X}, Status: 0x{status:02X} ({desc})")

                self.update_dtc_table(dtcs)
                self.log("📙 DTC information decoded.")
                return

            self.log("⚠️ DTC payload format not recognized.")
            return

        elif req_sid == 0x14: # Clear DTC
            # 0x54 응답은 보통 SF (pci_type == 0)이며,
            # 여기서는 TP로 조립된 페이로드가 아니므로 parse_uds_response에 들어오지 않음
            # (clear_dtc 함수 내에서 직접 처리함)
            pass

    # ---------------- DTC 테이블 업데이트 (수정됨) ----------------
    def update_dtc_table(self, dtcs):
        self.dtc_table.setRowCount(0)
        if not dtcs:
            self.dtc_table.insertRow(0)
            self.dtc_table.setItem(0, 0, QtWidgets.QTableWidgetItem("1"))
            self.dtc_table.setItem(0, 1, QtWidgets.QTableWidgetItem("없음"))
            # [수정] 5열에 맞게 수정
            self.dtc_table.setItem(0, 2, QtWidgets.QTableWidgetItem("DTC 없음")) 
            self.dtc_table.setItem(0, 3, QtWidgets.QTableWidgetItem("-"))
            self.dtc_table.setItem(0, 4, QtWidgets.QTableWidgetItem("정상 (No Fault)"))
            return

        for i, (code, status) in enumerate(dtcs, 1):
            row = self.dtc_table.rowCount()
            self.dtc_table.insertRow(row)
            self.dtc_table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(i)))
            self.dtc_table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"0x{code:06X}"))
            # [신규] 2번 열에 DTC 설명 추가
            self.dtc_table.setItem(row, 2, QtWidgets.QTableWidgetItem(self.get_dtc_description(code)))
            # [수정] 기존 열 인덱스 2->3, 3->4
            self.dtc_table.setItem(row, 3, QtWidgets.QTableWidgetItem(f"0x{status:02X}"))
            self.dtc_table.setItem(row, 4, QtWidgets.QTableWidgetItem(self.get_dtc_status_desc(status)))

    # ---------------- DTC 설명 변환 (신규) ----------------
    def get_dtc_description(self, code):
        """
        [신규] C 펌웨어(can.c)에서 정의한 DTC 코드를
        사람이 읽을 수 있는 설명으로 변환합니다.
        """
        
        # 펌웨어(can.c)의 DTC 생성 로직:
        # 0x010100 + (side_index * 0x10) + 0x0 (Timeout)
        # 0x010100 + (side_index * 0x10) + 0x1 (Range Error)
        # 0x010200 (ToF Timeout)
        # 0x010201 (ToF Range Error)
        
        mapping = {
            # 초음파 센서 1 (Left)
            0x010100: "Ultrasonic 1 (Left) - Timeout/No Data",
            0x010101: "Ultrasonic 1 (Left) - Out of Range",
            
            # 초음파 센서 2 (Right)
            0x010110: "Ultrasonic 2 (Right) - Timeout/No Data",
            0x010111: "Ultrasonic 2 (Right) - Out of Range",
            
            # 초음파 센서 3 (Rear)
            0x010120: "Ultrasonic 3 (Rear) - Timeout/No Data",
            0x010121: "Ultrasonic 3 (Rear) - Out of Range",
            
            # ToF 센서
            0x010200: "ToF Sensor - Timeout/No Data",
            0x010201: "ToF Sensor - Out of Range",
        }
        return mapping.get(code, f"알 수 없는 코드 (0x{code:06X})")

    # ---------------- 상태 설명 변환 ----------------
    def get_dtc_status_desc(self, status):
        mapping = {
            0x00: "정상 (No Fault)",
            0x01: "보류 (Pending)",
            0x08: "이번 주기 실패",
            0x10: "현재 실패 (Test Failed)",
            0x40: "확정 (Confirmed)",
        }
        return mapping.get(status, f"알 수 없음 (0x{status:02X})")


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    gui = CANUDSGui()
    gui.show()
    sys.exit(app.exec_())
