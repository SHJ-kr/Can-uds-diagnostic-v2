#!/bin/bash
# [pre-commit 훅] MISRA-C 검사. 위반이 있어도 경고만 하고 커밋은 허용한다.
# (기존 레거시 코드에 위반이 많아서 막아버리면 영원히 커밋을 못 하게 됨 - 점진적으로 줄여나가는 전략)

STAGED_C=$(git diff --cached --name-only --diff-filter=ACM -- '*.c')

if [ -z "$STAGED_C" ]; then
    exit 0
fi

echo "🔍 pre-commit: MISRA-C 검사 중 (경고만, 커밋은 막지 않음)..."

if ! command -v cppcheck >/dev/null 2>&1; then
    echo "⚠️  cppcheck가 설치되어 있지 않아 MISRA 검사를 건너뜁니다."
    exit 0
fi

# misra.py는 보통 cppcheck 설치 경로 바로 아래 addons/에 들어있다.
# (전체 디스크를 find / 로 훑는 건 느리고 불안정해서 후보 경로만 확인)
CPPCHECK_DIR=$(dirname "$(command -v cppcheck)")
MISRA_ADDON=""
for candidate in \
    "$CPPCHECK_DIR/addons/misra.py" \
    "$CPPCHECK_DIR/../share/cppcheck/addons/misra.py" \
    "/usr/share/cppcheck/addons/misra.py"; do
    if [ -f "$candidate" ]; then
        MISRA_ADDON="$candidate"
        break
    fi
done

if [ -z "$MISRA_ADDON" ]; then
    echo "⚠️  misra.py를 찾지 못해 MISRA 검사를 건너뜁니다. (cppcheck 설치에 addons가 포함되어 있는지 확인하세요)"
    exit 0
fi

TMPOUT=$(mktemp)
cppcheck --dump -I include $STAGED_C >/dev/null 2>&1
for f in $STAGED_C; do
    [ -f "$f.dump" ] && python3 "$MISRA_ADDON" --cli "$f.dump" 2>/dev/null
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
