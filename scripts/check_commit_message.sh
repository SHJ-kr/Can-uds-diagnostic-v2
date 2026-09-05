#!/bin/bash
# 커밋 메시지 컨벤션 검사
# 형식: <type>(<선택적 scope>): <설명>
# 예: feat: NRC 디코딩 로직 추가 / fix(gui): N_Cr 타임아웃 버그 수정
set -e

PATTERN='^(feat|fix|docs|style|refactor|test|chore|perf)(\([a-z0-9_-]+\))?: .{1,100}$'

# 이번 push로 새로 올라온 커밋들만 검사 (base..head 범위)
RANGE="${1:-HEAD~1..HEAD}"
FAILED=0

while read -r sha; do
    msg=$(git log -1 --pretty=%s "$sha")
    if [[ "$msg" =~ $PATTERN ]]; then
        echo "✅ $sha: $msg"
    else
        echo "❌ $sha: \"$msg\" - 컨벤션 위반"
        echo "   형식: <feat|fix|docs|style|refactor|test|chore|perf>(scope 선택): 설명"
        FAILED=1
    fi
done < <(git rev-list "$RANGE")

exit $FAILED
