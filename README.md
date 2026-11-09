# CAN / UDS diagnostic simulator

TC375용 CAN/UDS 코드, PC 진단 GUI, Python 가상 ECU와 통신 검증 도구입니다.
실물 하드웨어 없이 가상 메시지 송수신을 시험할 수 있습니다. MCU 전체 SDK와
보드 빌드 환경은 이 저장소에 포함되어 있지 않습니다.

## 설치와 GUI

```powershell
python -m pip install -r requirements.txt
python PYthon_tools/run_with_virtual_ecu.py
```

## 부하에 따른 진단 응답 성능 측정 (v2)

목적은 **배경 트래픽이 늘어도 진단 요청을 정해진 시간 안에 완료할 수 있는지**
확인하는 것입니다. 이 실험은 추가 결함 주입을 끄고, 센서값도 정상 범위의
고정값으로 설정합니다. 확률적 고장을 다시 집계하는 실험과 분리되어 있습니다.

```powershell
python PYthon_tools/bus_load_sweep.py --levels 0,10,20,30,45,55 --requests 200 --repeats 3 --deadline-ms 100 --hard-timeout-ms 1000
python PYthon_tools/bus_load_table.py
```

`100 ms`는 사용자가 지정하는 **실험 요구사항**이며 UDS 표준의 고정 제한시간이
아닙니다. `hard-timeout-ms`는 요청 전송 대기를 포함한 전체 관측의 타임아웃 예산입니다.
Python/OS 스케줄링 때문에 실제 실패 감지 시점은 이 예산을 초과할 수 있습니다.
우선 짧게 실행하려면 `--levels 0,20,55 --requests 7`을 사용할 수 있습니다.

새 결과는 `logs/bus_load_v2/`에 저장합니다. 이전 `logs/bus_load_sweep.csv`와
결함 분석용 `logs/real_diagnostic_log.csv`는 덮어쓰지 않습니다.

| 파일 | 내용 |
|---|---|
| `summary.csv` | 부하 조건별 집계 |
| `requests.csv` | 요청별 분류, 전송 대기, 첫 응답, 완료/실패 시간, 제한시간 판정 |
| `metadata.json` | 실험 설정, 실행 환경, 지표 정의, 모델 한계 |
| `bus_load_sweep.png` | 완료 응답시간과 제한시간 내 완료율 |
| `bus_load_sweep_table.png` | 표 렌더링 명령으로 생성하는 요약 이미지 |

### 지표의 의미

- `response_time_ms`: 요청을 보내기 **전**부터 응답 검증 완료 또는 실패까지의
  경과시간. 요청의 중재 대기시간을 포함하며 `perf_counter()`로 측정합니다.
- `request_tx_wait_ms`: 요청 프레임이 송신 큐에서 기다린 시간. 송신 타임아웃처럼
  시작 시점을 얻지 못한 요청은 공란이고, 집계에 표본 수도 기록합니다.
- `first_response_ms`: 처음으로 요청과 일치하는 응답 프레임을 클라이언트가 처리한
  시각까지의 시간. 중간 NRC 0x78도 첫 응답이며 최종 완료로는 세지 않습니다.
- `on_time_completion_rate_pct`: 제한시간 안에 검증을 마친 POS 또는 최종 NRC의
  비율. **NRC는 통신 완료지만 서비스 긍정 응답은 아닙니다.**
- `on_time_pos_rate_pct`: 제한시간 안에 POS로 완료한 요청의 비율.
- `deadline_miss_rate_pct`: 늦게 완료했거나 응답을 완료하지 못한 요청의 비율.
- `pos_rate_pct`: 제한시간과 관계없는 POS 비율. POS 100%도 지연 성능을 보장하지
  않습니다. 센서 범위 초과를 알리는 P/F 데이터도 POS에 포함될 수 있습니다.
- `avg_completed_response_ms`, `p95_completed_response_ms` 등: 완전한 POS/NRC를
  받은 요청만의 지연 통계. 실패율과 표본 수를 반드시 함께 해석해야 합니다.
  `avg_wait_ms_all_requests`는 실패 시 대기시간까지 포함한 별도의 통계입니다.
- `modeled_bus_load_pct`: 요청 관측 구간들의 총 시간 대비 **모델상 프레임 전송시간**.
  배경 프레임과 진단 요청/응답/FC를 모두 포함합니다. 배경 ECU가 0개여도 진단
  통신이 있으므로 0%가 아닙니다. 실물 버스에서 측정한 점유율이 아닙니다.
- `service_overhead_pct`: 프레임 전송을 처리한 실제 시간 중 모델 전송시간을
  초과한 부분의 비율. 유휴 구간이나 모든 OS 지연을 포괄하는 지표는 아닙니다.
- `nominal_offered_background_load_pct`: ECU 수 × 프레임 전송시간 ÷ 평균 생성주기의
  명목 제공 부하. 관측 점유율과 구분합니다.
- `background_coalesced`: 같은 배경 노드에 이미 대기 프레임이 있어 합쳐진 후속
  생성 횟수. 노드당 대기 프레임 1개 정책이며 무한 큐를 가정하지 않습니다.
- `mean_latency_increase_vs_idle_pct`: 배경 ECU 0개 조건의 완료 응답 평균 대비
  평균 응답시간 증가율. 기준 조건이나 완료 표본이 없으면 공란입니다.

### 모델과 복구 동작

중재기는 버스가 빌 때 이미 생성된 모든 프레임 중 낮은 ID를 선택합니다. 과거의
프레임당 0.5 ms 후보 수집 지연은 제거했습니다. 배경 메시지의 생성 예정 시각은
전송 완료 시각에 맞춰 밀지 않으며, 모든 배경 노드가 같은 중재 후보에 참여합니다.

응답의 SID/DID, 길이, CF 순서를 확인하고, 타임아웃·잘못된 프레임 이후에는 가상
ECU/채널을 새로 만듭니다. UDS에는 모든 요청에 공통으로 적용되는 트랜잭션 번호가
없어, 같은 DID의 늦은 응답을 다음 요청에 연결하지 않기 위한 **시험 격리 방식**입니다.
복구/워밍업 시간은 요청 지연 통계에서 제외합니다. 실물 ECU 재연결을 구현한 것은 아닙니다.

이 도구는 real-time Python 가상 통신 모델입니다. 프레임 길이는 기존의
`(47 + 8 × DLC) × 1.2 / bitrate`로 근사합니다(IFS 포함). 스레드/OS 스케줄링 영향을
받으므로 seed를 고정해도 지연 수치까지 동일하지는 않습니다. 실제 오류 프레임,
재전송, bus-off와 전기적 현상은 재현하지 않습니다. 결과를 실제 CAN 버스의 포화점이나
TC375 펌웨어의 정확성 증명으로 사용하지 않습니다. 물리적 성능 확인에는 실제 CAN
인터페이스의 타임스탬프·버스 부하 계측 또는 검증된 시뮬레이터와의 비교가 필요합니다.

## 확률적 결함 처리 시험

```powershell
python PYthon_tools/batch_log_generator.py
python PYthon_tools/sim_config_gui.py
```

이 도구들은 센서 실패, 범위 초과, 응답 지연, CF 누락, NRC 0x78 등 5개 결함 조건과
baseline을 시험합니다. 실제 하드웨어 고장률을 추정하는 실험이 아닙니다.
부하 실험과는 출력과 목적이 다릅니다. 새 배치 로그에는 제한시간과 지연 항목도 기록합니다.

## 검증

```powershell
python -m unittest discover -s test -p test_python_diagnostics.py -v
```

Python 검증은 시간 계측, 요청/응답 매칭, 중재 순서, 점유율 집계, 오류 후 세션 격리를
확인합니다. GitHub Actions에서도 별도 작업으로 실행합니다. C/Google Test 빌드는
`CMakeLists.txt`, Git 훅 설치와 MISRA-C 검사 범위는 `CONTRIBUTING.md`를 참조하세요.
