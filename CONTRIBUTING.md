# 개발 컨벤션

## 브랜치 전략 (Git Flow)
- `main` : 배포 가능한 상태만 유지
- `develop` : 다음 배포를 위한 통합 브랜치
- `feature/*` : 기능 단위 개발 (develop에서 분기, develop으로 병합)
- `release/*` : 배포 준비, 버그 수정만 (develop에서 분기, main+develop으로 병합)
- `hotfix/*` : 운영 중 긴급 수정 (main에서 분기, main+develop으로 병합)

## 커밋 메시지 컨벤션
형식: `<type>(<scope 생략가능>): <설명>`

| type | 의미 |
|---|---|
| feat | 새 기능 추가 |
| fix | 버그 수정 |
| docs | 문서만 수정 |
| style | 코드 동작에 영향 없는 스타일 변경 (포맷팅 등) |
| refactor | 기능 변화 없는 구조 개선 |
| test | 테스트 코드 추가/수정 |
| chore | 빌드, 설정 등 기타 변경 |
| perf | 성능 개선 |

**예시**
```
feat: NRC 0x78 ResponsePending 처리 로직 추가
fix(gui): N_Cr 타임아웃 감지 안 되던 버그 수정
test: DTC 클리어 로직 단위 테스트 추가
```

이 형식을 안 지키면 push 시 CI의 `commit-convention` 잡이 실패하고, 로컬 커밋 시점에도 아래 pre-commit 훅에서 막힙니다.

## 로컬 개발 환경 셋업 (클론 후 최초 1회)
이 저장소는 [pre-commit](https://pre-commit.com) 프레임워크로 git hook을 관리합니다. `.githooks` 같은 커밋된 훅 폴더를 쓰지 않으므로, **클론 후 반드시 아래 명령을 한 번 실행**해야 커밋 시점에 검사가 동작합니다.

```bash
pip install pre-commit
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

이 명령은 `.git/hooks/pre-commit`, `.git/hooks/commit-msg`를 생성합니다(로컬 전용, git으로 추적되지 않음). clang-format은 pre-commit이 pip으로 미리 빌드된 바이너리를 자동으로 받아오므로 별도로 LLVM을 설치할 필요가 없습니다.

## 자동으로 검사되는 것
| 시점 | 검사 항목 |
|---|---|
| **커밋할 때** (로컬, pre-commit 훅) | clang-format (위반 시 자동 수정 후 커밋 중단 → `git diff`로 확인, `git add`, 재커밋) |
| | MISRA-C (`cppcheck_config/misra_rule_texts.txt` 규칙 설명 포함, 경고만·커밋은 막지 않음) |
| | 커밋 메시지 컨벤션 |
| **push/PR할 때** (CI, `ci.yml`) | clang-format, 커밋 메시지 컨벤션, 일반 cppcheck 정적분석(`--enable=all`, 참고용·실패 처리 안 함), Google Test 동적 검증 |

**MISRA-C는 로컬 pre-commit 훅에서만 검사합니다.** CI는 MISRA 특화 검사 대신 더 범용적인 cppcheck 검사를 참고용 안전망으로만 돌리고, 결과가 있어도 빌드를 실패시키지 않습니다(리포트는 Actions 아티팩트로 업로드됨). 이는 project 1과 동일한 구조입니다 — 로컬 훅이 이미 강제성 있는 검사(스타일)와 참고용 검사(MISRA)를 모두 담당하므로, CI에서 같은 걸 중복으로 강하게 막을 필요가 없기 때문입니다.

## 로컬에서 훅을 수동으로 돌려보고 싶다면
```bash
# 스테이지된 파일에 대해 모든 훅 실행 (실제 커밋 없이 미리 확인)
pre-commit run

# 저장소 전체 파일에 대해 실행
pre-commit run --all-files

# 특정 훅만 실행
pre-commit run clang-format
```
