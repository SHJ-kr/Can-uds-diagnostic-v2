"""
가상 ECU 배치 시뮬레이션 로그 생성기
------------------------------------
virtual_ecu.VirtualECU를 여러 결함 주입(fault_injection) 시나리오로 반복 구동해서,
canuds_gui.py와 동일한 스키마의 진단 로그를 logs/real_diagnostic_log.csv에 쌓는다.

canuds_gui.py의 recv_all()/parse_uds_response()는 QWidget 인스턴스 메서드(self.bus,
self.log() 등 Qt 상태에 강하게 묶여있음)라서 그대로 재사용할 수 없다. 그래서 이 파일의
UdsClient는 그 로직(ISO-TP 재조립, N_Cr 타임아웃, NRC 0x78 pending 처리, 이벤트 스키마)을
Qt 의존 없이 그대로 미러링한다. CSV에는 NRC 코드를 hex 그대로만 저장하고(설명 텍스트는
불필요) canuds_gui.py를 아예 import하지 않으므로, 이 스크립트는 PyQt5 없이도 동작한다.

주의: python-can의 virtual 버스는 "같은 프로세스" 안에서 채널명이 같은 Bus끼리만
통신되므로(run_with_virtual_ecu.py와 동일한 제약), 이 스크립트도 VirtualECU를 같은
프로세스 안에서 스레드로 띄우고 그 안에서 클라이언트 Bus를 연다.
"""
import csv
import random
import sys
import time
from collections import Counter
from pathlib import Path

import can

sys.path.insert(0, str(Path(__file__).resolve().parent))
from virtual_ecu import VirtualECU, CAN_BITRATE, frame_tx_time, precise_wait  # noqa: E402
from can_bus_arbiter import ArbitratedBus, start_background_traffic  # noqa: E402

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
CSV_PATH = LOGS_DIR / "real_diagnostic_log.csv"
# scenario는 canuds_gui.py의 원래 스키마엔 없던 컬럼이다. 클러스터링 입력 피처로는
# 절대 쓰지 않고(nrc_clustering.py의 build_pipeline 참고), 클러스터링이 끝난 뒤에만
# "진짜 어느 결함 시나리오에서 나온 건지" 사후 대조하는 용도로만 쓴다 - 클러스터링에게
# 정답을 미리 알려주면 검증 자체가 무의미해지기 때문에 반드시 이 용도로만 한정한다.
FIELDNAMES = ["timestamp", "req_sid", "did", "event_type", "code", "response_time_ms", "scenario", "result_char"]

REQ_ID = 0x7E0
RES_ID = 0x7E8
REQUESTS_PER_SCENARIO = 200

# 4개 센서 DID + ECU Info DID + ReadDTC + ClearDTC = 7가지 요청 종류
REQUEST_KINDS = [
    ("did", 0x0001),
    ("did", 0x0002),
    ("did", 0x0003),
    ("did", 0x0004),
    ("did", 0x0005),
    ("read_dtc", None),
    ("clear_dtc", None),
]

SCENARIOS = [
    ("baseline", {}),
    ("sensor_fail", {"sensor_fail_prob": 0.3}),
    ("out_of_range", {"out_of_range_prob": 0.3}),
    ("response_delay", {"response_delay_range": (0.05, 0.5)}),
    ("drop_cf", {"drop_cf_prob": 0.2}),
    ("send_pending", {"send_pending_prob": 0.2}),
]


def record_event(req_sid, did, event_type, code=None, response_time_ms=None, result_char=None):
    """canuds_gui.py의 _record_diagnostic_event와 동일한 스키마(quirk 포함)로 이벤트 dict 생성.

    result_char: 센서 읽기(DID 0x0001~0x0004) 응답 페이로드에 실려오는 P(정상)/F(임계값
    초과) 문자. out_of_range_prob 결함은 event_type/req_sid/response_time_ms만 봐서는
    baseline과 전혀 구분이 안 된다는 게 클러스터링 사후 검증(ARI≈0.075)으로 확인됐다 -
    ECU가 여전히 정상(POS) 응답을 보내고, 다만 페이로드 안의 이 한 글자만 'F'로 바뀌는
    결함이기 때문. 그 신호를 실제로 뽑아서 저장해야 클러스터링이 이걸 구분할 기회라도
    생긴다."""
    return {
        "timestamp": time.time(),
        "req_sid": f"0x{req_sid:02X}" if req_sid is not None else "",
        "did": f"0x{did:04X}" if did else "",  # did=0/None 모두 "" (원본 동작 그대로 재현)
        "event_type": event_type,
        "code": (f"0x{code:02X}" if isinstance(code, int) else (code or "")),
        "response_time_ms": round(response_time_ms, 1) if response_time_ms is not None else "",
        "result_char": result_char or "",
    }


class UdsClient:
    """canuds_gui.CANUDSGui의 recv_all/parse_uds_response/read_by_did/read_dtc/clear_dtc를
    Qt 없이 그대로 미러링한 헤드리스 클라이언트."""

    N_CR_TIMEOUT = 1.0

    def __init__(self, bus, req_id=REQ_ID, res_id=RES_ID, bitrate=CAN_BITRATE, arbiter=None):
        self.bus = bus
        self.req_id = req_id
        self.res_id = res_id
        self.bitrate = bitrate
        # arbiter가 있으면 배경 트래픽 노드들과 실제 ID 우선순위로 경쟁해서 버스를
        # 점유한다. 없으면(기본) 예전처럼 즉시 보낸다.
        self.arbiter = arbiter

    def _send(self, data8):
        msg = can.Message(arbitration_id=self.req_id, data=data8, is_extended_id=False)
        if self.arbiter is not None:
            self.arbiter.transmit(msg)
            return
        # virtual_ecu.VirtualECU._send_raw와 동일하게, 500kbps 기준 프레임 전송시간을
        # 직접 반영한 뒤(virtual 버스는 이 시간을 흉내내지 않으므로) 전송한다.
        precise_wait(frame_tx_time(len(data8), self.bitrate))
        self.bus.send(msg)

    def _send_flow_control(self):
        self._send([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])

    def recv_all(self, timeout):
        """(frames, status) 반환. status: "OK" / "N_CR_TIMEOUT" / "NO_RESPONSE"."""
        frames = []
        start = time.time()
        deadline = start + timeout
        got_ff = False
        total_len = 0
        received_len = 0
        last_frame_time = start

        while time.time() < deadline:
            msg = self.bus.recv(timeout=0.05)

            if msg and msg.arbitration_id == self.res_id:
                pci_type = msg.data[0] >> 4

                if pci_type == 0 and len(msg.data) >= 4 and msg.data[1] == 0x7F and msg.data[3] == 0x78:
                    # NRC 0x78 (ResponsePending) - 실패가 아니라 표준대로 대기시간 연장
                    deadline = time.time() + timeout
                    last_frame_time = time.time()
                    continue

                frames.append(msg)
                last_frame_time = time.time()

                if pci_type == 1:  # First Frame
                    got_ff = True
                    total_len = ((msg.data[0] & 0x0F) << 8) | msg.data[1]
                    received_len = 6
                    self._send_flow_control()
                elif pci_type == 2 and got_ff:  # Consecutive Frame
                    bytes_left = total_len - received_len
                    received_len += min(7, bytes_left) if bytes_left > 0 else 0
                    if received_len >= total_len:
                        got_ff = False
                        return frames, "OK"  # 멀티프레임 재조립 완료 - 더 기다릴 이유 없음
                elif pci_type == 0:  # 단일 프레임 최종 응답 (SF) - 즉시 반환
                    # canuds_gui.py의 recv_all은 이 시점에도 timeout까지 계속 폴링하는데,
                    # GUI에서 사람이 버튼을 가끔 누를 때는 안 보이던 문제가 배치 반복
                    # 수천 건에서는 매 요청이 timeout(2~3초)만큼 늘어지는 성능 문제로
                    # 드러난다. 완성된 응답을 받았으면 즉시 반환 - 이후 프레임 분류
                    # 결과(POS/NRC/...)는 동일, 대기 시간만 없앤다.
                    return frames, "OK"
            else:
                if got_ff and (time.time() - last_frame_time) > self.N_CR_TIMEOUT:
                    return frames, "N_CR_TIMEOUT"

        if not frames:
            return frames, "NO_RESPONSE"
        return frames, "OK"

    def parse_response(self, frames, req_sid, did, response_time_ms=None):
        payload = bytearray()
        total_len = 0
        pos_sid = (req_sid + 0x40) & 0xFF
        got_ff = False

        for f in frames:
            d = f.data
            pci_type = d[0] >> 4

            if pci_type == 3:  # FlowControl - 파싱 대상 아님
                continue

            if pci_type == 0:  # SF
                sf_len = d[0] & 0x0F
                if d[1] == pos_sid or d[1] == 0x7F:
                    payload.extend(d[1:1 + sf_len])
                continue

            if pci_type == 1:  # FF
                total_len = ((d[0] & 0x0F) << 8) | d[1]
                payload.extend(d[2:8])
                got_ff = True
                continue

            if pci_type == 2 and got_ff:  # CF
                bytes_left = total_len - len(payload)
                bytes_to_copy = min(7, bytes_left)
                if bytes_to_copy > 0:
                    payload.extend(d[1:1 + bytes_to_copy])
                if len(payload) >= total_len:
                    got_ff = False
                continue

        if not payload:
            return record_event(req_sid, did, "NO_RESPONSE", response_time_ms=response_time_ms)

        if total_len and len(payload) < total_len:
            return record_event(req_sid, did, "INCOMPLETE_REASSEMBLY", response_time_ms=response_time_ms)

        uds_sid = payload[0]

        if uds_sid == 0x7F:
            nrc_code = payload[2] if len(payload) > 2 else None
            return record_event(req_sid, did, "NRC", nrc_code, response_time_ms)

        if uds_sid != pos_sid:
            return record_event(req_sid, did, "UNEXPECTED_SID", uds_sid, response_time_ms)

        result_char = None
        # 센서 읽기(0x22, DID 0x0001~0x0004) 응답은 [pos_sid, did_h, did_l, val_h, val_l,
        # result_char] 6바이트 - virtual_ecu.py의 _handle_22가 만드는 형식과 동일하게 파싱.
        if req_sid == 0x22 and did in (0x0001, 0x0002, 0x0003, 0x0004) and len(payload) >= 6:
            c = payload[5]
            if 32 <= c <= 126:
                result_char = chr(c)
        return record_event(req_sid, did, "POS", pos_sid, response_time_ms, result_char=result_char)

    def read_by_did(self, did):
        did_h, did_l = (did >> 8) & 0xFF, did & 0xFF
        tx = [0x03, 0x22, did_h, did_l, 0, 0, 0, 0]
        self._send(tx)
        t0 = time.time()
        frames, status = self.recv_all(timeout=3.0)
        rt_ms = (time.time() - t0) * 1000
        if frames:
            return self.parse_response(frames, 0x22, did, response_time_ms=rt_ms)
        return record_event(0x22, did, status, response_time_ms=rt_ms)

    def read_dtc(self):
        tx = [0x03, 0x19, 0x02, 0xFF, 0, 0, 0, 0]
        self._send(tx)
        t0 = time.time()
        frames, status = self.recv_all(timeout=3.0)
        rt_ms = (time.time() - t0) * 1000
        if frames:
            return self.parse_response(frames, 0x19, 0, response_time_ms=rt_ms)
        return record_event(0x19, None, status, response_time_ms=rt_ms)

    def clear_dtc(self):
        tx = [0x02, 0x14, 0xFF, 0, 0, 0, 0, 0]
        self._send(tx)
        t0 = time.time()
        frames, status = self.recv_all(timeout=2.0)
        rt_ms = (time.time() - t0) * 1000
        if frames:
            # GUI의 clear_dtc()는 이벤트를 아예 기록 안 하는 구멍이 있는데(0x54 SF는
            # parse_response 하나로 POS로 정확히 분류됨), 배치 스크립트는 그 구멍을
            # 따라가지 않고 모든 요청을 빠짐없이 기록한다.
            return self.parse_response(frames, 0x14, None, response_time_ms=rt_ms)
        return record_event(0x14, None, status, response_time_ms=rt_ms)


def build_request_plan(n):
    reps = (n // len(REQUEST_KINDS)) + 1
    plan = (REQUEST_KINDS * reps)[:n]
    random.shuffle(plan)
    return plan


def run_scenario(name, fault_overrides, background_presets=None):
    """background_presets: [(이름, arbitration_id, (주기최소초, 주기최대초)), ...].
    None이면 can_bus_arbiter.DEFAULT_BACKGROUND_NODES를 그대로 쓴다(기존 동작)."""
    channel = f"vcan_{name}"
    # 이 시나리오의 버스를 하나 만든다 - 진단 클라이언트/ECU/배경 트래픽 노드들이
    # 전부 이 arbiter를 통해서만 실제로 프레임을 내보내고, ID 우선순위로 경쟁한다.
    arbiter = ArbitratedBus(channel=channel)
    if background_presets is not None:
        bg_nodes = start_background_traffic(arbiter, presets=background_presets)
    else:
        bg_nodes = start_background_traffic(arbiter)

    ecu = VirtualECU(
        channel=channel, bustype="virtual", fault_injection=fault_overrides,
        verbose=False, arbiter=arbiter,
    )
    ecu.start()
    time.sleep(0.3)  # ECU가 버스를 먼저 열 시간을 줌 (run_with_virtual_ecu.py와 동일한 패턴)

    bus = can.interface.Bus(channel=channel, interface="virtual")
    client = UdsClient(bus, req_id=REQ_ID, res_id=RES_ID, arbiter=arbiter)
    events = []
    try:
        for kind, arg in build_request_plan(REQUESTS_PER_SCENARIO):
            if kind == "did":
                events.append(client.read_by_did(arg))
            elif kind == "read_dtc":
                events.append(client.read_dtc())
            elif kind == "clear_dtc":
                events.append(client.clear_dtc())
    finally:
        for node in bg_nodes:
            node.stop()
        bus.shutdown()
        ecu.stop()
        ecu.join(timeout=2.0)
        arbiter.stop()

    return events


def append_events_to_csv(path, events, write_header):
    mode = "w" if write_header else "a"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(events)


def main(background_presets=None, progress_callback=None):
    """background_presets를 넘기면 그 배경 ECU 구성으로 전체 시나리오를 돌린다
    (sim_config_gui.py처럼 외부에서 호출할 때 사용). progress_callback(str)이 있으면
    print()로 나가는 진행 메시지를 그 콜백에도 그대로 전달한다(GUI 로그창 갱신용)."""
    def emit(msg):
        print(msg)
        if progress_callback:
            progress_callback(msg)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    all_events = []

    for i, (name, overrides) in enumerate(SCENARIOS):
        emit(f"=== 시나리오 실행: {name} (fault_injection={overrides or '기본값(전부 0)'}) ===")
        events = run_scenario(name, overrides, background_presets=background_presets)
        for e in events:
            e["scenario"] = name  # 사후 검증 전용 - 클러스터링 피처로는 쓰지 않음
        append_events_to_csv(CSV_PATH, events, write_header=(i == 0))
        all_events.extend(events)
        counts = dict(Counter(e["event_type"] for e in events))
        emit(f"[{name}] {len(events)}건 완료 - {counts}")

    emit("\n=== 전체 요약 ===")
    total_counts = Counter(e["event_type"] for e in all_events)
    for event_type, count in total_counts.items():
        emit(f"  {event_type}: {count}")
    emit(f"  총 요청 수: {len(all_events)}")
    emit(f"\nCSV 저장 위치: {CSV_PATH}")
    return all_events


if __name__ == "__main__":
    main()
