#!/bin/bash
input=$(cat)
# 무한 루프 방지: 이미 한 번 막혀서 재작업 중이면 통과
[ "$(echo "$input" | jq -r '.stop_hook_active')" = "true" ] && exit 0
cd "$CLAUDE_PROJECT_DIR" || exit 0
if ! output=$(./scripts/check.sh 2>&1); then
  echo "검증 실패. 아래 오류를 고친 뒤 다시 완료하세요:" >&2
  echo "$output" | tail -n 60 >&2
  exit 2   # 종료를 막고 오류를 Claude에게 보여줌
fi
exit 0