#!/bin/bash
# [commit-msg 훅] 방금 작성한 커밋 메시지가 컨벤션(feat:, fix: 등)을 지키는지 검사.
# git이 이 스크립트를 부를 때, $1 자리에 "메시지가 담긴 임시 파일 경로"를 넘겨준다.
COMMIT_MSG_FILE="$1"
PATTERN='^(feat|fix|docs|style|refactor|test|chore|perf)(\([a-z0-9_-]+\))?: .{1,100}$'

# '#'으로 시작하는 안내 줄은 무시하고, 첫 실제 줄(제목 줄)만 검사
SUBJECT=$(grep -v '^#' "$COMMIT_MSG_FILE" | grep -v '^$' | head -1)

if [[ "$SUBJECT" =~ $PATTERN ]]; then
    exit 0
fi

echo "❌ 커밋 메시지 컨벤션 위반: \"$SUBJECT\""
echo "   형식: <feat|fix|docs|style|refactor|test|chore|perf>(scope 선택): 설명"
echo "   예:   feat: NRC 디코딩 로직 추가"
exit 1
