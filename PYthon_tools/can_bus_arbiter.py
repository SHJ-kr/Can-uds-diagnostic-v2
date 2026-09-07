"""
CAN ID 우선순위 기반 버스 중재(arbitration) + 배경 트래픽 시뮬레이터
---------------------------------------------------------------------
python-can의 virtual 버스는 send()를 부르는 즉시 같은 채널의 다른 Bus 인스턴스에게
메시지를 전달할 뿐이라서, 실제 CAN 버스처럼 "여러 노드가 동시에 보내려 하면 ID가
낮은 쪽이 먼저 버스를 차지하고, 진 쪽은 물러났다가 재시도한다"는 중재(arbitration)를
전혀 흉내내지 않는다. 우리 진단 클라이언트/ECU만 버스에 있는 1:1 통신에서는 애초에
동시에 보내려는 경쟁 자체가 없어서 이 중재가 관여할 자리가 없었다.

이 파일은 그 위에 얇은 중재 레이어를 추가해서 실제로 경쟁이 일어나게 만든다:
  - 이 채널로 보내고 싶은 모든 노드(진단 클라이언트, VirtualECU, 배경 트래픽 노드들)는
    bus.send()를 직접 부르는 대신 ArbitratedBus.transmit()을 호출해서 "나 이 메시지
    보내고 싶다"고 등록만 한다.
  - 중재 스레드가 버스가 빌 때마다, 그 순간 대기 중인 후보들 중 arbitration_id가
    가장 낮은(=우선순위가 가장 높은) 것을 골라 실제로 내보낸다. 진 쪽은 큐에 남아
    다음 라운드에 다시 경쟁한다 - 실제 CAN의 "낮은 ID가 이긴다" 규칙과 동일한 결과.
  - BackgroundTrafficNode는 진단 통신과 무관한 다른 ECU들(엔진/바디 제어기 등)이
    항상 트래픽을 내고 있는 실제 차량 버스 상황을 흉내낸다. 우리 진단 ID(0x7E0/0x7E8)는
    배경 트래픽보다 일부러 ID를 높게(=우선순위 낮게) 잡았다 - 실제 차량에서도 진단
    서비스는 보통 안전/제어 메시지보다 우선순위가 낮게 설계되므로, 진단 프레임이
    배경 트래픽에 실제로 밀리는 상황이 나오게 된다.
"""
import random
import sys
import threading
import time

import can

from virtual_ecu import CAN_BITRATE, frame_tx_time, precise_wait

# CPython 기본 GIL 스위치 간격(5ms)이 우리가 원하는 중재 윈도우(아래 ARBITRATION_WINDOW)
# 보다 커서, "동시에" 실행 중인 다른 스레드가 그 안에 자기 차례를 못 받고 밀릴 수
# 있다(실제로 겪은 문제). 스위치 간격을 훨씬 짧게 줄여서, 작은 중재 윈도우로도
# 진짜 동시 경쟁을 안정적으로 잡아낼 수 있게 한다.
sys.setswitchinterval(0.0001)

# 배경 트래픽 노드 프리셋: (이름, arbitration_id, 전송 간격 범위(초))
# ID는 전부 우리 진단 ID(req=0x7E0, res=0x7E8)보다 낮게(=우선순위 높게) 잡아서,
# 실제로 진단 프레임이 이 트래픽들에게 버스를 양보하는 상황이 발생하게 한다.
ARBITRATION_WINDOW = 0.0005  # 후보를 모으는 중재 윈도우(500us) - 매 프레임마다 붙는 고정비용이라 짧게 유지

DEFAULT_BACKGROUND_NODES = [
    ("EngineECU", 0x100, (0.008, 0.012)),   # 엔진 RPM/토크 - 약 10ms 주기, 최고 우선순위
    ("BodyECU", 0x300, (0.018, 0.022)),     # 바디 제어기(도어/라이트 등) - 약 20ms 주기
    ("InfotainmentECU", 0x600, (0.045, 0.055)),  # 인포테인먼트 상태 - 약 50ms 주기, 그래도 진단(0x7E0/0x7E8)보다는 우선순위 높음
]


class _PendingFrame:
    __slots__ = ("message", "sent", "enqueued_at")

    def __init__(self, message):
        self.message = message
        self.sent = False
        self.enqueued_at = time.time()


class ArbitratedBus:
    """실제로 프레임을 버스에 내보내는 단일 지점. 여러 송신자는 transmit()으로만
    접근하고, 수신은 각자 자기 can.interface.Bus(channel=같은 채널)로 직접 한다
    (virtual 버스는 같은 채널명을 쓰는 모든 Bus 인스턴스가 같은 방송 도메인이라
    송신 경로만 이렇게 한 곳으로 모아도 수신에는 영향이 없다)."""

    def __init__(self, channel, bitrate=CAN_BITRATE):
        self.channel = channel
        self.bitrate = bitrate
        self._tx_bus = can.interface.Bus(channel=channel, interface="virtual")
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending = []
        self._stop_flag = threading.Event()
        self._thread = threading.Thread(target=self._arbiter_loop, daemon=True)
        self._thread.start()

    def transmit(self, message):
        """이 메시지를 보내고 싶다고 등록하고, 실제로 버스에 나갈 때까지(=중재에서
        이겨서 전송이 끝날 때까지) 블록한다. 호출부 입장에서는 bus.send()를 부른 것과
        똑같이 보이지만, 그 안에서 다른 노드들과 실제로 우선순위 경쟁이 일어난다."""
        ticket = _PendingFrame(message)
        with self._cv:
            self._pending.append(ticket)
            self._cv.notify_all()
            while not ticket.sent and not self._stop_flag.is_set():
                self._cv.wait(timeout=0.1)

    def _arbiter_loop(self):
        while not self._stop_flag.is_set():
            with self._cv:
                if not self._pending:
                    self._cv.wait(timeout=0.05)
                    continue

            # 첫 후보가 등록된 순간 바로 그걸 골라버리면, 거의 동시에(수십~수백us
            # 차이로) 등록되는 다른 후보들은 경쟁할 기회조차 없이 밀려난다 - 실제로
            # 세 스레드를 동시에 띄워서 테스트해보니 ID 우선순위가 아니라 스레드
            # 스케줄링 순서대로 나가는 걸 확인했다. 그래서 짧은 "중재 윈도우"만큼
            # 기다려서 그 사이 도착한 후보들까지 모은 다음 비교한다 - 실제 CAN이
            # SOF 시점에 대기 중이던 노드들을 한꺼번에 비트 단위로 비교하는 것과
            # 동일한 결과를 내기 위한 장치.
            precise_wait(ARBITRATION_WINDOW)

            with self._cv:
                if not self._pending:
                    continue
                # 지금까지 모인 후보들 중 arbitration_id가 가장 낮은(우선순위가
                # 가장 높은) 걸 골라 이번 라운드의 승자로 정한다.
                winner = min(self._pending, key=lambda t: t.message.arbitration_id)
                self._pending.remove(winner)

            # 승자가 버스를 점유하는 동안(=실제 프레임 전송시간)은 락 밖에서
            # 기다려서, 그 사이에 다른 노드들이 transmit()으로 새 후보를 등록할
            # 수 있게 한다(실제 버스에서 다음 프레임을 준비하는 것과 동일).
            precise_wait(frame_tx_time(len(winner.message.data), self.bitrate))
            self._tx_bus.send(winner.message)

            with self._cv:
                winner.sent = True
                self._cv.notify_all()

    def stop(self):
        self._stop_flag.set()
        with self._cv:
            self._cv.notify_all()
        self._thread.join(timeout=1.0)
        self._tx_bus.shutdown()


class BackgroundTrafficNode(threading.Thread):
    """진단 통신과 무관하게 계속 트래픽을 내는 다른 ECU 하나를 흉내낸다.
    arbitration_id가 낮을수록(=우선순위가 높을수록) 우리 진단 프레임(0x7E0/0x7E8)을
    실제로 밀어낼 확률이 커진다."""

    def __init__(self, arbiter, arbitration_id, interval_range, name="bg"):
        super().__init__(daemon=True, name=name)
        self.arbiter = arbiter
        self.arbitration_id = arbitration_id
        self.interval_range = interval_range
        self._stop_flag = threading.Event()

    def stop(self):
        self._stop_flag.set()

    def run(self):
        while not self._stop_flag.is_set():
            msg = can.Message(
                arbitration_id=self.arbitration_id,
                data=[random.randint(0, 255) for _ in range(8)],
                is_extended_id=False,
            )
            self.arbiter.transmit(msg)
            time.sleep(random.uniform(*self.interval_range))


def start_background_traffic(arbiter, presets=DEFAULT_BACKGROUND_NODES):
    """presets에 정의된 배경 트래픽 노드들을 전부 만들어 시작하고 리스트로 반환한다.
    끝날 때 각 노드의 stop()을 호출해줘야 한다."""
    nodes = []
    for name, arb_id, interval_range in presets:
        node = BackgroundTrafficNode(arbiter, arb_id, interval_range, name=name)
        node.start()
        nodes.append(node)
    return nodes
