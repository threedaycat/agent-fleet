#!/usr/bin/env bash
# desk_push.sh —— 往 desk 送待决事项，但**绝不打断用户正在做的选择**。
#
# 背景：主会话直接 tmux send-keys 到 desk 时，如果 desk 正开着选择框等人，
# 注入的文本会把那个框冲掉——人还没选，要选的东西就没了（真发生过）。
#
# 用法：
#   desk_push.sh send  <pane> <<'EOF'   # 送一条；desk 忙就自动排队
#   ...待决事项全文...
#   EOF
#   desk_push.sh drain <pane>           # desk 空了，放一条排队的进去
#   desk_push.sh list                   # 看排了几条
#
# 队列文件一行一条（JSON，正文在 text 字段），只追加/重写，不并发。

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
Q="$DIR/data/desk_queue.ndjson"
mkdir -p "$DIR/data"
touch "$Q"

# desk 是不是正在等人选？看 pane 里有没有选择框的特征行。
desk_busy() {
  local pane="$1" snap
  snap="$(tmux capture-pane -t "$pane" -p 2>/dev/null || true)"
  # AskUserQuestion 选择框的固定页脚
  if grep -qE 'Enter to select|↑/↓ to navigate|Esc to cancel' <<<"$snap"; then
    return 0
  fi
  # 正在跑（还没弹框但在处理上一条），也别插队
  if grep -qE '^[✻✽✢·⏺●] .*\((esc to interrupt|[0-9]+m?s)' <<<"$snap"; then
    return 0
  fi
  return 1
}

enqueue() {
  python3 - "$Q" <<'PY'
import json, sys
q = sys.argv[1]
text = sys.stdin.read()
with open(q, "a", encoding="utf-8") as f:
    f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
PY
}

pop_one() {
  python3 - "$Q" <<'PY'
import json, sys, os
q = sys.argv[1]
lines = [l for l in open(q, encoding="utf-8").read().splitlines() if l.strip()]
if not lines:
    sys.exit(3)
first, rest = lines[0], lines[1:]
tmp = q + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(rest) + ("\n" if rest else ""))
os.replace(tmp, q)
sys.stdout.write(json.loads(first)["text"])
PY
}

# 真正注入：文字 → 停 0.4s → 回车（不停顿的话长文本回车会先到，任务卡输入框里）
inject() {
  local pane="$1" text="$2"
  tmux send-keys -t "$pane" -l "$text"
  sleep 0.4
  tmux send-keys -t "$pane" Enter
}

case "${1:-}" in
  send)
    pane="${2:?用法: desk_push.sh send <pane>}"
    text="$(cat)"
    if desk_busy "$pane"; then
      printf '%s' "$text" | enqueue
      echo "desk 正在等人选择，已排队（当前队列 $(wc -l < "$Q" | tr -d ' ') 条）"
    else
      inject "$pane" "$text"
      echo "已送进 desk"
    fi
    ;;
  drain)
    pane="${2:?用法: desk_push.sh drain <pane>}"
    if desk_busy "$pane"; then
      echo "desk 还在忙，不放"; exit 0
    fi
    if text="$(pop_one)"; then
      inject "$pane" "$text"
      echo "放了一条进 desk（剩 $(wc -l < "$Q" | tr -d ' ') 条）"
    else
      echo "队列是空的"
    fi
    ;;
  list)
    n=$(wc -l < "$Q" | tr -d ' ')
    echo "队列 $n 条"
    [ "$n" -gt 0 ] && python3 -c "
import json,sys
for i,l in enumerate(open('$Q',encoding='utf-8'),1):
    l=l.strip()
    if l: print(f'  {i}.', json.loads(l)['text'][:80].replace(chr(10),' '), '…')
"
    ;;
  *)
    echo "用法: desk_push.sh {send|drain} <pane> | list" >&2; exit 2 ;;
esac
