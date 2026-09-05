"""
가상 ECU (TC375 can.c 펌웨어 로직을 Python으로 재현)
-----------------------------------------------------
실물 CAN 버스/TC375 보드가 없을 때, python-can의 virtual 인터페이스로
canuds_gui.py를 실제로 테스트할 수 있게 해주는 SIL(Software-in-the-Loop) ECU 시뮬레이터.

주의(python-can virtual 버스의 제약):
  virtual 버스는 "같은 프로세스" 안에서 채널명이 같은 Bus 인스턴스끼리만 통신됨.
  즉, 이 ECU를 별도 터미널(별도 프로세스)에서 띄우면 GUI와 서로 못 본다.
  -> run_with_virtual_ecu.py 처럼 같은 프로세스 안에서 스레드로 띄워서 써야 함.

C 펌웨어(can.c)와의 대응 관계를 최대한 그대로 재현했고,
버그처럼 보이는 부분(예: DID 불일치인데 NRC 0x11)도 "실제 동작"에 맞춰 일부러 그대로 재현했다.
(TC375 쪽을 나중에 고치면 이 시뮬레이터의 해당 값도 같이 바꿔야 함)

[예외] FlowControl(0x30) 처리만은 실제 펌웨어(Can_TpSend, FC 안 기다리고 그냥 전송)와
다르게, ISO 15765-2 표준대로 FF 전송 후 FC를 기다렸다가 BS/STmin을 지켜서 전송하도록
만들었다. TC375 C 코드는 그대로 두고 이 가상 ECU에서만 표준을 지키도록 한 것 - 나중에
정식 진단 툴(CANoe 등)과 연동할 걸 대비한 요청에 따른 것.
"""
import can
import threading
import time
import random
import struct


DEFAULT_ECU_INFO = {
    "vin": "MY_TC375_VIN_001",
    "hw": "TC375_HW_V1.0.0",
    "sw": "MY_APP_SW_V1.2.3",
    "sn": "SN_ECU_1234567890",
    "supplier": "MyProjectSupplier",
}

DEFAULT_FAULT_INJECTION = {
    "sensor_fail_prob": 0.0,        # 센서 자체 무응답(No Data) 확률 -> NRC 0x11 + DTC
    "out_of_range_prob": 0.0,       # 센서값이 임계값을 벗어날 확률 -> 정상응답이지만 result_char='F' + DTC
    "response_delay_range": (0.0, 0.0),  # 응답 전 인위적 지연 (초) - Bus Load 높은 상황 흉내
    "drop_cf_prob": 0.0,            # 멀티프레임 응답 중 CF를 고의로 누락 -> 클라이언트 N_Cr 타임아웃 유발
    "send_pending_prob": 0.0,       # 최종 응답 전에 NRC 0x78(ResponsePending)을 먼저 보낼 확률
}


class VirtualECU(threading.Thread):
    def __init__(self, channel="vcan_test", bustype="virtual",
                 req_id=0x7E0, res_id=0x7E8, fault_injection=None, verbose=True):
        super().__init__(daemon=True)
        self.channel = channel
        self.bustype = bustype
        self.req_id = req_id
        self.res_id = res_id
        self.verbose = verbose
        self._stop_flag = threading.Event()
        self.bus = None

        self.ecu_info = dict(DEFAULT_ECU_INFO)
        self.sensor_thresholds = {"ultra_min": 0, "ultra_max": 400, "tof_min": 0, "tof_max": 5000}
        self.dtc_list = {}  # code(int) -> {"detect_cnt": int, "status": int}

        self.fault_injection = dict(DEFAULT_FAULT_INJECTION)
        if fault_injection:
            self.fault_injection.update(fault_injection)

        # TP 수신 상태 (Can_TpRx 대응 - 0x2E Write 요청 재조립용)
        self._tp_rx_buf = bytearray()
        self._tp_rx_expected = 0
        self._tp_rx_active = False
        self._tp_rx_next_sn = 1

    def log(self, msg):
        if self.verbose:
            print(f"[VirtualECU] {msg}")

    def stop(self):
        self._stop_flag.set()

    def run(self):
        self.bus = can.interface.Bus(channel=self.channel, interface=self.bustype)
        self.log(f"시작 (channel={self.channel}, req_id=0x{self.req_id:03X} -> res_id=0x{self.res_id:03X})")
        while not self._stop_flag.is_set():
            try:
                msg = self.bus.recv(timeout=0.1)
            except Exception as e:
                self.log(f"recv 에러: {e}")
                break
            if msg is None or msg.arbitration_id != self.req_id:
                continue
            try:
                self._handle_frame(bytes(msg.data))
            except Exception as e:
                self.log(f"프레임 처리 중 에러: {e}")
        if self.bus:
            self.bus.shutdown()
        self.log("종료")

    # ---------------- 프레임 라우팅 (Can_RxIsrHandler 대응) ----------------
    def _handle_frame(self, data):
        pci = data[0] >> 4
        if pci in (1, 2):  # FF(1)/CF(2) -> TP 재조립 경로 (0x2E Write 전용)
            self._tp_rx(data)
        else:  # SF -> 즉시 처리 (0x22 Read / 0x19 / 0x14)
            self._handle_sf(data)

    # ---------------- TP 수신 (Can_TpRx 대응) ----------------
    def _tp_rx(self, data):
        pci = data[0] >> 4
        if pci == 1:  # First Frame
            self._tp_rx_expected = ((data[0] & 0x0F) << 8) | data[1]
            self._tp_rx_buf = bytearray(data[2:8])
            self._tp_rx_active = True
            self._tp_rx_next_sn = 1
            self._send_flow_control()
        elif pci == 2 and self._tp_rx_active:  # Consecutive Frame
            sn = data[0] & 0x0F
            if sn == self._tp_rx_next_sn:
                self._tp_rx_buf.extend(data[1:8])
                self._tp_rx_next_sn = (self._tp_rx_next_sn + 1) & 0x0F
            if len(self._tp_rx_buf) >= self._tp_rx_expected:
                self._tp_rx_active = False
                self._dispatch_completed_pdu(bytes(self._tp_rx_buf[: self._tp_rx_expected]))

    def _send_flow_control(self):
        self._send_raw([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])

    def _dispatch_completed_pdu(self, uds):
        """UDS_Dispatch_CompletedPdu 대응 - TP로 재조립된 요청(현재는 0x14/0x19/0x2E)"""
        sid = uds[0]
        if sid == 0x14:
            self._handle_14()
        elif sid == 0x19:
            self._handle_19()
        elif sid == 0x2E:
            if len(uds) >= 3 and uds[1] == 0x00 and uds[2] == 0x05:
                self._handle_2e_0005(uds)
            elif len(uds) >= 3 and uds[1] == 0x00 and uds[2] == 0x06:
                self._handle_2e_0006(uds)
            else:
                self._send_negative(0x2E, 0x31)  # requestOutOfRange (실제 펌웨어와 동일)
        else:
            self._send_negative(sid, 0x11)  # serviceNotSupported (실제 펌웨어와 동일)

    # ---------------- SF 처리 (Can_RxIsrHandler의 인라인 처리 대응) ----------------
    def _handle_sf(self, data):
        if len(data) < 2:
            return
        sid = data[1]
        if sid == 0x22:
            did = (data[2] << 8) | data[3]
            self._handle_22(did, data)
        elif sid == 0x19:
            self._handle_19()
        elif sid == 0x14:
            self._handle_14()
        elif sid == 0x2E:
            # 실제 펌웨어에도 있는, 사실상 도달 불가능한 인라인 SF 0x2E 분기.
            # (클라이언트는 항상 TP(FF+CF)로 0x2E를 보내므로 이 경로는 재현만 해둠)
            self._send_negative(0x2E, 0x31)
        else:
            self._send_negative(sid, 0x11)

    def _maybe_delay(self):
        lo, hi = self.fault_injection["response_delay_range"]
        if hi > 0:
            time.sleep(random.uniform(lo, hi))

    def _maybe_send_pending(self, requested_sid):
        if random.random() < self.fault_injection["send_pending_prob"]:
            self.log(f"⏳ (고의) NRC 0x78 ResponsePending 먼저 전송 (SID=0x{requested_sid:02X})")
            self._send_negative(requested_sid, 0x78)
            time.sleep(0.3)

    def _report_dtc(self, code):
        entry = self.dtc_list.get(code)
        if entry is None:
            self.dtc_list[code] = {"detect_cnt": 1, "status": 0x01}  # Pending
        else:
            entry["detect_cnt"] += 1
            entry["status"] = 0x40 if entry["detect_cnt"] >= 2 else 0x01

    def _handle_22(self, did, raw):
        self._maybe_send_pending(0x22)
        self._maybe_delay()

        side_map = {0x0001: 0, 0x0002: 1, 0x0003: 2, 0x0004: 3, 0x0005: 4}
        if did not in side_map:
            # 실제 펌웨어와 동일하게 NRC 0x11 (의미상으론 0x31이 더 맞지만 실제 동작 그대로 재현)
            self._send_negative(0x22, 0x11)
            return
        side = side_map[did]

        if side <= 2:  # 초음파 센서 1~3
            ok = random.random() >= self.fault_injection["sensor_fail_prob"]
            if not ok:
                self._report_dtc(0x010100 + side * 0x10 + 0x0)
                self._send_negative(0x22, 0x11)  # 실제 펌웨어와 동일한 NRC
                return
            val = random.randint(50, 450)
            forced_out = random.random() < self.fault_injection["out_of_range_prob"]
            in_range = self.sensor_thresholds["ultra_min"] <= val <= self.sensor_thresholds["ultra_max"]
            if forced_out or not in_range:
                self._report_dtc(0x010100 + side * 0x10 + 0x1)
                result_char = ord("F")
            else:
                result_char = ord("P")
            self._send_raw([0x06, 0x62, raw[2], raw[3], (val >> 8) & 0xFF, val & 0xFF, result_char, 0])

        elif side == 3:  # ToF
            ok = random.random() >= self.fault_injection["sensor_fail_prob"]
            if not ok:
                self._report_dtc(0x010200)
                self._send_negative(0x22, 0x11)
                return
            val = random.randint(100, 5500)
            forced_out = random.random() < self.fault_injection["out_of_range_prob"]
            in_range = self.sensor_thresholds["tof_min"] <= val <= self.sensor_thresholds["tof_max"]
            if forced_out or not in_range:
                self._report_dtc(0x010201)
                result_char = ord("F")
            else:
                result_char = ord("P")
            self._send_raw([0x06, 0x62, raw[2], raw[3], (val >> 8) & 0xFF, val & 0xFF, result_char, 0])

        elif side == 4:  # ECU Info - 멀티프레임 응답 (Can_TpSend 대응)
            info_bytes = self._pack_ecu_info()
            tx = bytes([0x62, raw[2], raw[3]]) + info_bytes
            self._tp_send(tx)

    def _pack_ecu_info(self):
        def fixed(s, length):
            b = s.encode("ascii", errors="ignore")[:length]
            return b + b"\x00" * (length - len(b))
        return (
            fixed(self.ecu_info["vin"], 18) + b"\x00\x00"
            + fixed(self.ecu_info["hw"], 20) + fixed(self.ecu_info["sw"], 20)
            + fixed(self.ecu_info["sn"], 20) + fixed(self.ecu_info["supplier"], 20)
        )

    def _handle_19(self):
        self._maybe_send_pending(0x19)
        self._maybe_delay()
        if not self.dtc_list:
            self._send_raw([0x03, 0x59, 0x02, 0x00, 0, 0, 0, 0])
            return
        tx = bytearray([0x59, 0x02, 0xFF])
        for code, entry in self.dtc_list.items():
            tx += bytes([(code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF, entry["status"]])
        self._tp_send(bytes(tx))

    def _handle_14(self):
        self.dtc_list.clear()
        self._send_raw([0x02, 0x54, 0xFF, 0, 0, 0, 0, 0])

    def _handle_2e_0005(self, uds):
        if len(uds) < 3 + 100:
            self._send_negative(0x2E, 0x13)  # incorrectMessageLengthOrInvalidFormat
            return
        payload = uds[3:103]

        def unpack(seg):
            return seg.split(b"\x00")[0].decode("ascii", errors="ignore")

        self.ecu_info = {
            "vin": unpack(payload[0:18]), "hw": unpack(payload[20:40]),
            "sw": unpack(payload[40:60]), "sn": unpack(payload[60:80]),
            "supplier": unpack(payload[80:100]),
        }
        self._send_raw([0x03, 0x6E, 0x00, 0x05, 0, 0, 0, 0])

    def _handle_2e_0006(self, uds):
        if len(uds) < 3 + 8:
            self._send_negative(0x2E, 0x13)
            return
        u_min, u_max, t_min, t_max = struct.unpack("<HHHH", bytes(uds[3:11]))
        self.sensor_thresholds = {"ultra_min": u_min, "ultra_max": u_max, "tof_min": t_min, "tof_max": t_max}
        self._send_raw([0x03, 0x6E, 0x00, 0x06, 0, 0, 0, 0])

    # ---------------- 전송 유틸 ----------------
    def _send_raw(self, data8):
        data8 = (list(data8) + [0] * 8)[:8]
        self.bus.send(can.Message(arbitration_id=self.res_id, data=data8, is_extended_id=False))

    def _send_negative(self, req_sid, nrc):
        self._send_raw([0x03, 0x7F, req_sid, nrc, 0, 0, 0, 0])

    def _tp_send(self, data):
        """
        [수정] 기존엔 실제 TC375 펌웨어(Can_TpSend)처럼 FC를 기다리지 않고 그냥 쏘기만 했음.
        요청에 따라 TC375 C 코드는 그대로 두고, 가상 ECU만 ISO 15765-2 표준대로
        FF 전송 후 클라이언트의 FlowControl(0x30)을 기다렸다가 그 안의 BS/STmin을 지켜서
        CF를 전송하도록 수정.
        """
        length = len(data)
        if length <= 7:
            self._send_raw([length] + list(data))
            return

        ff = [0x10 | ((length >> 8) & 0x0F), length & 0xFF] + list(data[0:6])
        self._send_raw(ff)

        # [신규] FlowControl 대기 (req_id로 클라이언트가 보내줌)
        fc = self._wait_for_flow_control(timeout=2.0)
        if fc is None:
            self.log("⚠️ FlowControl(0x30) 미수신 → 전송 중단")
            return

        fs, bs, stmin = fc[0] & 0x0F, fc[1], fc[2]
        if fs != 0x0:  # 0x0 = ContinueToSend(CTS)
            self.log(f"⚠️ FC FS != CTS (FS=0x{fs:02X}) → 전송 중단")
            return
        gap_sec = (stmin / 1000.0) if stmin <= 0x7F else (
            (stmin - 0xF0) / 10000.0 if 0xF1 <= stmin <= 0xF9 else 0.0
        )

        sent, sn, blk = 6, 1, 0
        while sent < length:
            if self.fault_injection["drop_cf_prob"] > 0 and random.random() < self.fault_injection["drop_cf_prob"]:
                self.log(f"💥 (고의) CF 프레임 드롭 (SN={sn}) → 전송 중단 (N_Cr 타임아웃 유발용)")
                return
            chunk = list(data[sent: sent + 7])
            cf = [0x20 | (sn & 0x0F)] + chunk
            self._send_raw(cf)
            sent += len(chunk)
            sn = (sn + 1) % 16
            blk += 1
            time.sleep(gap_sec if gap_sec > 0 else 0.001)

            # [신규] Block Size(BS)만큼 보냈으면 다음 블록 전 다시 FC를 기다림 (표준 동작)
            if bs and blk >= bs and sent < length:
                blk = 0
                fc = self._wait_for_flow_control(timeout=1.0)
                if fc is None:
                    self.log("⚠️ 다음 블록 FC 미수신 → 전송 중단")
                    return

    def _wait_for_flow_control(self, timeout=2.0):
        """req_id로 들어오는 FlowControl(PCI=0x3) 프레임을 기다린다."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = self.bus.recv(timeout=0.05)
            if msg and msg.arbitration_id == self.req_id and (msg.data[0] >> 4) == 0x3:
                return msg.data
        return None
