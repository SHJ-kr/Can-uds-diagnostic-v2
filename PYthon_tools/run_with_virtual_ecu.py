"""
가상 ECU + 진단 GUI를 한 프로세스 안에서 함께 실행하는 런처
------------------------------------------------------------
python-can의 virtual 버스는 "같은 프로세스" 안에서만 서로 통신 가능하기 때문에
(다른 터미널에서 각각 실행하면 서로 못 봄), 가상 ECU를 백그라운드 스레드로 띄우고
같은 프로세스 안에서 GUI를 실행한다.

사용법:
    python3 run_with_virtual_ecu.py

고장 주입을 켜고 싶으면 아래 FAULT_INJECTION 딕셔너리를 수정하세요.
예) N_Cr 타임아웃을 실제로 발생시켜보고 싶다면 drop_cf_prob를 1.0으로.
"""
import os
import sys
import time

# ★ GUI가 PCAN 실물 대신 가상 버스를 쓰도록 환경변수로 지정 (GUI 코드 자체는 수정 안 함)
os.environ["CAN_CHANNEL"] = "vcan_test"
os.environ["CAN_BUSTYPE"] = "virtual"

from virtual_ecu import VirtualECU

# 실제로 테스트해보고 싶은 상황에 맞게 조절하세요. 전부 0이면 "정상 동작"만 테스트.
FAULT_INJECTION = {
    "sensor_fail_prob": 0.0,        # 예: 0.3 -> 30% 확률로 센서 무응답(NRC 0x11 + DTC)
    "out_of_range_prob": 0.0,       # 예: 0.3 -> 30% 확률로 정상응답이지만 result_char='F'
    "response_delay_range": (0.0, 0.0),  # 예: (0.1, 0.8) -> 응답 전 0.1~0.8초 랜덤 지연
    "drop_cf_prob": 0.0,            # 예: 0.5 -> ECU Info 등 멀티프레임 응답 중 50% 확률로 CF 드롭
    "send_pending_prob": 0.0,       # 예: 0.5 -> 50% 확률로 NRC 0x78 먼저 보내고 실제 응답
}


def main():
    ecu = VirtualECU(
        channel=os.environ["CAN_CHANNEL"],
        bustype=os.environ["CAN_BUSTYPE"],
        fault_injection=FAULT_INJECTION,
        verbose=True,
    )
    ecu.start()
    time.sleep(0.3)  # ECU가 버스를 먼저 열 시간을 줌

    # canuds_gui.py를 이 파일과 같은 폴더에 두고 실행하세요.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from PyQt5 import QtWidgets
    from canuds_gui import CANUDSGui

    app = QtWidgets.QApplication(sys.argv)
    gui = CANUDSGui()
    gui.show()

    try:
        sys.exit(app.exec_())
    finally:
        ecu.stop()


if __name__ == "__main__":
    main()
