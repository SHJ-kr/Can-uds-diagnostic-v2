#!/bin/bash
# [pre-commit 훅] MISRA-C 검사. 위반이 있어도 경고만 하고 커밋은 허용한다.
# (기존 레거시 코드에 위반이 많아서 막아버리면 영원히 커밋을 못 하게 됨 - 점진적으로 줄여나가는 전략)

STAGED_C=$(git diff --cached --name-only --diff-filter=ACM -- '*.c')

if [ -z "$STAGED_C" ]; then
    exit 0
fi

echo "🔍 pre-commit: MISRA-C 검사 중 (경고만, 커밋은 막지 않음)..."

# cppcheck는 pip 패키지(cppcheck-wheel)로 이 훅 전용 가상환경에 자동 설치된다.
# (.pre-commit-config.yaml의 misra-check 훅: language: python, additional_dependencies: [cppcheck])
# 그 패키지가 공식으로 제공하는 get_cppcheck_dir()로 실제 설치 위치(바이너리+addons)를 찾는다.
CPPCHECK_DIR=$(python -c "from cppcheck import get_cppcheck_dir; print(get_cppcheck_dir())" 2>/dev/null)

if [ -z "$CPPCHECK_DIR" ]; then
    echo "⚠️  cppcheck pip 패키지를 찾지 못해 MISRA 검사를 건너뜁니다."
    exit 0
fi

CPPCHECK_BIN="$CPPCHECK_DIR/cppcheck"
[ -f "$CPPCHECK_BIN.exe" ] && CPPCHECK_BIN="$CPPCHECK_BIN.exe"
MISRA_ADDON="$CPPCHECK_DIR/addons/misra.py"

if [ ! -f "$MISRA_ADDON" ]; then
    echo "⚠️  misra.py를 찾지 못해 MISRA 검사를 건너뜁니다."
    exit 0
fi

TMPOUT=$(mktemp)
"$CPPCHECK_BIN" --dump -I include $STAGED_C >/dev/null 2>&1
for f in $STAGED_C; do
    [ -f "$f.dump" ] && python "$MISRA_ADDON" --cli "$f.dump" 2>/dev/null
    rm -f "$f.dump"  # 임시 dump 파일 정리
done > "$TMPOUT"

if [ -s "$TMPOUT" ]; then
    COUNT=$(wc -l < "$TMPOUT")
    echo "⚠️  MISRA-C 위반 ${COUNT}건 발견 (커밋은 막지 않음, 참고만 하세요)"
else
    echo "✅ MISRA-C 위반 없음"
fi
rm -f "$TMPOUT"

exit 0
