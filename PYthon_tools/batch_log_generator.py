"""
가상 ECU 배치 시뮬레이션 로그 생성기
------------------------------------
virtual_ecu.VirtualECU를 여러 결함 주입(fault_injection) 시나리오로 반복 구동해서,
canuds_gui.py와 동일한 스키마의 진단 로그를 logs/real_diagnostic_log.csv에 쌓는다.

UdsClient는 Qt와 독립된 진단 클라이언트다. 요청/응답 매칭, ISO-TP 재조립,
N_Cr 감시, NRC 0x78 처리와 요청 전송 전부터의 지연 계측을 수행한다.
GUI 기본 로그 열에 제한시간/완료 여부 등 계측 열을 추가한다. 부하 스윕도 이
클라이언트를 재사용하지만, 결함 주입과 센서 난수 생성을 끄고 별도 파일에 기록한다.

주의: python-can의 virtual 버스는 "같은 프로세스" 안에서 채널명이 같은 Bus끼리만
통신되므로(run_with_virtual_ecu.py와 동일한 제약), 이 스크립트도 VirtualECU를 같은
프로세스 안에서 스레드로 띄우고 그 안에서 클라이언트 Bus를 연다.
"""
import csv
import random
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import can

sys.path.insert(0, str(Path(__file__).resolve().parent))
from virtual_ecu import VirtualECU, CAN_BITRATE, frame_tx_time, precise_wait  # noqa: E402
from can_bus_arbiter import ArbitratedBus, DEFAULT_BACKGROUND_NODES  # noqa: E402

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
CSV_PATH = LOGS_DIR / "real_diagnostic_log.csv"
# scenario는 canuds_gui.py의 원래 스키마엔 없던 컬럼이다. 클러스터링 입력 피처로는
# 절대 쓰지 않고(nrc_clustering.py의 build_pipeline 참고), 클러스터링이 끝난 뒤에만
# "진짜 어느 결함 시나리오에서 나온 건지" 사후 대조하는 용도로만 쓴다 - 클러스터링에게
# 정답을 미리 알려주면 검증 자체가 무의미해지기 때문에 반드시 이 용도로만 한정한다.
FIELDNAMES = ["timestamp", "req_sid", "did", "event_type", "code", "response_time_ms", "scenario", "result_char"]
FIELDNAMES += ["request_tx_wait_ms", "first_response_ms", "deadline_ms", "response_complete", "deadline_met", "ignored_frames"]

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
    """UDS client with request matching, bounded waits and end-to-end timing."""

    N_CR_TIMEOUT = 1.0

    def __init__(self, bus, req_id=REQ_ID, res_id=RES_ID, bitrate=CAN_BITRATE,
                 arbiter=None, deadline_ms=100.0, hard_timeout=3.0):
        if deadline_ms <= 0 or hard_timeout <= 0:
            raise ValueError("deadline and hard timeout must be positive")
        self.bus = bus
        self.req_id = req_id
        self.res_id = res_id
        self.bitrate = bitrate
        self.arbiter = arbiter
        self.deadline_ms = deadline_ms
        self.hard_timeout = hard_timeout
        self._first_response_at = None
        self.ignored_frames = 0

    def _send(self, data8, timeout=None):
        msg = can.Message(arbitration_id=self.req_id, data=data8, is_extended_id=False)
        if self.arbiter is not None:
            return self.arbiter.transmit(msg, timeout=self.hard_timeout if timeout is None else timeout)
        precise_wait(frame_tx_time(len(data8), self.bitrate))
        self.bus.send(msg, timeout=timeout)
        return None

    @staticmethod
    def _matches(payload, req_sid, did):
        if not payload:
            return False
        if payload[0] == 0x7F:
            return len(payload) >= 3 and payload[1] == req_sid
        if payload[0] != ((req_sid + 0x40) & 0xFF):
            return False
        if req_sid in (0x22, 0x2E):
            return len(payload) >= 3 and ((payload[1] << 8) | payload[2]) == did
        return req_sid != 0x19 or (len(payload) >= 2 and payload[1] == 0x02)

    def recv_all(self, timeout, req_sid, did=None):
        """Collect only this request's response; the hard deadline never extends."""
        frames = []
        deadline = time.perf_counter() + timeout
        expected_len = received_len = 0
        next_sn = 1
        last_cf_at = None
        self._first_response_at = None
        self.ignored_frames = 0
        while True:
            now = time.perf_counter()
            if now >= deadline:
                return frames, "INCOMPLETE_REASSEMBLY" if frames else "NO_RESPONSE"
            if last_cf_at is not None and now - last_cf_at >= self.N_CR_TIMEOUT:
                return frames, "N_CR_TIMEOUT"
            wait = min(0.02, deadline - now)
            if last_cf_at is not None:
                wait = min(wait, max(0.0, self.N_CR_TIMEOUT - (now - last_cf_at)))
            msg = self.bus.recv(timeout=wait)
            if time.perf_counter() >= deadline:
                return frames, "INCOMPLETE_REASSEMBLY" if frames else "NO_RESPONSE"
            if msg is None or msg.arbitration_id != self.res_id:
                continue
            data = msg.data
            if not data:
                return frames, "INVALID_RESPONSE"
            pci = data[0] >> 4
            if pci == 0:
                length = data[0] & 0x0F
                if not (1 <= length <= 7) or len(data) < length + 1:
                    return frames, "INVALID_RESPONSE"
                payload = bytes(data[1:1 + length])
                if not self._matches(payload, req_sid, did):
                    self.ignored_frames += 1
                    continue
                now = time.perf_counter()
                if self._first_response_at is None:
                    self._first_response_at = now
                if payload[0] == 0x7F and payload[2] == 0x78:
                    continue  # Intermediate response; still bounded by hard timeout.
                if frames:
                    return frames, "INVALID_RESPONSE"
                return [msg], "OK"
            if pci == 1:
                if len(data) != 8:
                    return frames, "INVALID_RESPONSE"
                if not self._matches(bytes(data[2:8]), req_sid, did):
                    self.ignored_frames += 1
                    continue
                expected_len = ((data[0] & 0x0F) << 8) | data[1]
                if expected_len <= 7 or frames:
                    return frames, "INVALID_RESPONSE"
                frames.append(msg)
                received_len = 6
                next_sn = 1
                last_cf_at = time.perf_counter()
                if self._first_response_at is None:
                    self._first_response_at = last_cf_at
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return frames, "INCOMPLETE_REASSEMBLY"
                try:
                    self._send([0x30, 0, 0, 0, 0, 0, 0, 0], timeout=remaining)
                except TimeoutError:
                    return frames, "TX_TIMEOUT"
            elif pci == 2:
                if not frames:
                    self.ignored_frames += 1
                    continue
                if len(data) != 8 or (data[0] & 0x0F) != next_sn:
                    return frames, "SEQUENCE_ERROR"
                frames.append(msg)
                received_len += min(7, expected_len - received_len)
                next_sn = (next_sn + 1) & 0x0F
                last_cf_at = time.perf_counter()
                if received_len >= expected_len:
                    return frames, "OK"
            elif pci != 0:
                self.ignored_frames += 1

    def parse_response(self, frames, req_sid, did, response_time_ms=None):
        payload = bytearray()
        expected_len = 0
        next_sn = 1
        for frame in frames:
            data = frame.data
            if not data:
                return record_event(req_sid, did, "INVALID_RESPONSE", response_time_ms=response_time_ms)
            pci = data[0] >> 4
            if pci == 0 and not payload:
                length = data[0] & 0x0F
                if not 1 <= length <= 7 or len(data) < length + 1:
                    return record_event(req_sid, did, "INVALID_RESPONSE", response_time_ms=response_time_ms)
                payload.extend(data[1:1 + length])
            elif pci == 1 and not payload and len(data) == 8:
                expected_len = ((data[0] & 0x0F) << 8) | data[1]
                if expected_len <= 7:
                    return record_event(req_sid, did, "INVALID_RESPONSE", response_time_ms=response_time_ms)
                payload.extend(data[2:8])
            elif pci == 2 and expected_len and len(payload) < expected_len:
                if len(data) != 8 or (data[0] & 0x0F) != next_sn:
                    return record_event(req_sid, did, "SEQUENCE_ERROR", response_time_ms=response_time_ms)
                payload.extend(data[1:1 + min(7, expected_len - len(payload))])
                next_sn = (next_sn + 1) & 0x0F
            else:
                return record_event(req_sid, did, "INVALID_RESPONSE", response_time_ms=response_time_ms)
        if not payload:
            return record_event(req_sid, did, "NO_RESPONSE", response_time_ms=response_time_ms)
        if expected_len and len(payload) != expected_len:
            return record_event(req_sid, did, "INCOMPLETE_REASSEMBLY", response_time_ms=response_time_ms)
        if not self._matches(payload, req_sid, did):
            return record_event(req_sid, did, "RESPONSE_MISMATCH", response_time_ms=response_time_ms)
        if payload[0] == 0x7F:
            if len(payload) != 3 or payload[2] == 0x78:
                return record_event(req_sid, did, "INVALID_RESPONSE", response_time_ms=response_time_ms)
            return record_event(req_sid, did, "NRC", payload[2], response_time_ms)
        valid = True
        result_char = None
        if req_sid == 0x22:
            if did in (1, 2, 3, 4):
                valid = len(payload) == 6 and payload[5] in (ord("P"), ord("F"))
                if valid:
                    result_char = chr(payload[5])
            elif did == 5:
                valid = len(payload) == 103  # Current Python ECU/GUI wire format.
        elif req_sid == 0x19:
            valid = len(payload) >= 3 and (len(payload) - 3) % 4 == 0
        elif req_sid == 0x2E:
            valid = len(payload) == 3
        elif req_sid == 0x14:
            valid = len(payload) == 2 and payload[1] == 0xFF
        if not valid:
            return record_event(req_sid, did, "INVALID_RESPONSE", response_time_ms=response_time_ms)
        return record_event(req_sid, did, "POS", payload[0], response_time_ms, result_char)

    def _request(self, data8, req_sid, did=None):
        start = time.perf_counter()  # Includes the request's arbitration wait.
        ticket = None
        self._first_response_at = None
        self.ignored_frames = 0
        try:
            ticket = self._send(data8, timeout=self.hard_timeout)
            remaining = self.hard_timeout - (time.perf_counter() - start)
            if remaining <= 0:
                frames, status = [], "NO_RESPONSE"
            else:
                frames, status = self.recv_all(remaining, req_sid, did)
            if status == "OK":
                event = self.parse_response(frames, req_sid, did)
            else:
                event = record_event(req_sid, did, status)
        except TimeoutError:
            event = record_event(req_sid, did, "TX_TIMEOUT")
        end = time.perf_counter()
        elapsed_ms = (end - start) * 1000
        complete = event["event_type"] in ("POS", "NRC")
        event.update({
            "response_time_ms": round(elapsed_ms, 3),
            "request_tx_wait_ms": round((ticket.started_at - ticket.enqueued_at) * 1000, 3) if ticket else "",
            "first_response_ms": round((self._first_response_at - start) * 1000, 3)
                if self._first_response_at is not None else "",
            "deadline_ms": self.deadline_ms,
            "response_complete": complete,
            "deadline_met": complete and elapsed_ms <= self.deadline_ms,
            "ignored_frames": self.ignored_frames,
        })
        return event

    def read_by_did(self, did):
        return self._request([3, 0x22, did >> 8, did & 0xFF, 0, 0, 0, 0], 0x22, did)

    def read_dtc(self):
        return self._request([3, 0x19, 2, 0xFF, 0, 0, 0, 0], 0x19)

    def clear_dtc(self):
        return self._request([2, 0x14, 0xFF, 0, 0, 0, 0, 0], 0x14)


def build_request_plan(n, rng=None):
    reps = (n // len(REQUEST_KINDS)) + 1
    plan = (REQUEST_KINDS * reps)[:n]
    (rng or random).shuffle(plan)
    return plan


def run_scenario(name, fault_overrides, background_presets=None, request_count=REQUESTS_PER_SCENARIO,
                 deadline_ms=100.0, hard_timeout=3.0, seed=42, measure_load=False,
                 sensor_values=None, progress_callback=None):
    """Run requests sequentially; reset the whole virtual session after transport failure.

    UDS has no general transaction ID. A fresh channel/ECU after a timeout or
    malformed transfer prevents late frames (including same-DID responses) from
    being assigned to the next request. Reset/warm-up time is excluded from
    per-request measurement windows. NRC is a completed, negative service result.
    """
    if request_count <= 0 or deadline_ms <= 0 or hard_timeout <= 0:
        raise ValueError("request_count, deadline and hard_timeout must be positive")
    rng = random.Random(seed)
    plan = build_request_plan(request_count, rng)
    events = []
    session = None
    session_number = 0

    def close_session(active):
        if active is None:
            return
        arbiter, ecu, bus, client = active
        ecu.stop()
        arbiter.stop()  # Unblocks an ECU waiting for a queued transmission.
        ecu.join(timeout=1.0)
        bus.shutdown()
        if ecu.is_alive():
            raise RuntimeError("virtual ECU did not stop")

    def open_session():
        channel = f"vcan_{name}_{uuid.uuid4().hex}"
        arbiter = ArbitratedBus(channel=channel, seed=seed + session_number)
        bus = ecu = None
        try:
            presets = background_presets if background_presets is not None else DEFAULT_BACKGROUND_NODES
            arbiter.configure_background(presets)
            bus = can.interface.Bus(channel=channel, interface="virtual")
            ecu = VirtualECU(
                channel=channel, bustype="virtual", fault_injection=fault_overrides,
                verbose=False, arbiter=arbiter, sensor_values=sensor_values,
            )
            client = UdsClient(bus, arbiter=arbiter, deadline_ms=deadline_ms, hard_timeout=hard_timeout)
            ecu.start()
            if not ecu.ready.wait(1.0) or ecu.startup_error is not None:
                raise RuntimeError("virtual ECU failed to start") from ecu.startup_error
            time.sleep(0.03)  # Warm-up, outside measured request windows.
            return arbiter, ecu, bus, client
        except BaseException:
            if ecu is not None:
                ecu.stop()
            arbiter.stop()
            if ecu is not None and ecu.ident is not None:
                ecu.join(timeout=1.0)
            if bus is not None:
                bus.shutdown()
            raise

    try:
        for request_index, (kind, arg) in enumerate(plan):
            if session is None:
                session = open_session()
            arbiter, ecu, bus, client = session
            counters_before = arbiter.background_counts()
            start = time.perf_counter()
            if kind == "did":
                event = client.read_by_did(arg)
            elif kind == "read_dtc":
                event = client.read_dtc()
            else:
                event = client.clear_dtc()
            end = time.perf_counter()
            if measure_load:
                window = arbiter.measure_window(start, end)
                for key in ("background_releases", "background_coalesced"):
                    window[key] -= counters_before[key]
                event.update(window)
                event["request_index"] = request_index + 1
                event["session_index"] = session_number
            events.append(event)
            if progress_callback:
                progress_callback(request_index + 1, event)
            if not event["response_complete"]:
                active, session = session, None
                close_session(active)
                session_number += 1
    finally:
        close_session(session)
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
