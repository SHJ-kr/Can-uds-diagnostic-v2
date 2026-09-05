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

이 형식을 안 지키면 push 시 CI의 `commit-convention` 잡이 실패합니다.

## 자동으로 검사되는 것 (push할 때마다)
1. **clang-format** — `src/`, `include/` 안의 `.c`/`.h` 파일 스타일 검사
2. **커밋 메시지 컨벤션** — 위 형식 준수 여부
3. **MISRA-C** — cppcheck의 misra addon으로 `src/` 안의 `.c` 파일 검사
4. **Google Test 동적 검증** — `test/` 안의 테스트를 CMake로 빌드 후 실행

## 로컬에서 미리 확인하고 싶다면
```bash
# 포맷 확인
clang-format --dry-run --Werror src/*.c include/*.h

# 포맷 자동 수정
clang-format -i src/*.c include/*.h

# MISRA 검사
cppcheck --dump -I include src/*.c
python3 $(find / -iname misra.py 2>/dev/null | head -1) --cli src/*.c.dump

# 테스트
cmake -S . -B build && cmake --build build && ctest --test-dir build
```
