#!/bin/bash
# dtwatch 守护脚本：
#   at-stream  常驻长连接，接「群里有人 @ 我」的实时事件 → data/at_events.ndjson
#   poll-loop  定时跑 dtwatch.py poll，做增量采集 + 打标
#   push-loop  盯自聊天，手机一发指令就回执 + 叫醒该接的那个 Claude 会话
#              （没有它就只能等某个会话恰好收尾才有人来拉，全闲着时压根没人接）
#
# 用法：
#   ./run.sh start [间隔秒数]   # 默认 300 秒，两个进程都拉起来
#   ./run.sh stop
#   ./run.sh status
#   ./run.sh tail               # 跟一下采集日志

set -uo pipefail
cd "$(dirname "$0")"
BASE="$PWD"
DATA="$BASE/data"
mkdir -p "$DATA"

PID_AT="$DATA/at.pid"
PID_POLL="$DATA/poll.pid"
PID_PUSH="$DATA/push.pid"
LOG_AT="$DATA/at.log"
LOG_POLL="$DATA/poll.log"
LOG_PUSH="$DATA/cc/cc.log"
PY="$(command -v python3)"

# launchd 给的 PATH 是最小集，dws 在 homebrew 下，必须显式补上
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

LA_DIR="$HOME/Library/LaunchAgents"
# launchd label 前缀。反向域名只是 launchd 的习惯，随便改成你自己的都行 ——
# 改了记得先 `./run.sh uninstall` 卸掉旧 label，否则会留下卸不掉的僵尸 plist。
#
# 默认值必须跟这台机器上 ~/Library/LaunchAgents/ 里实际装的 plist 一致——
# 2026-08-01 发现这里曾经默认 local.dtwatch，但机器上装的是 com.workos.dtwatch
# 前缀（谁装的、什么时候装的已经查不到了），导致 `status` 的 launchd_loaded()
# 永远查错 label、永远判断成"没有 launchd 托管"。后果不是显示错误这么简单：
# 每次巡检看到"托管方式：手工"就照着 TRIAGE.md 的指示 `./run.sh start`，
# 于是在 launchd 已经在跑的三个进程之上，又叠了一遍 at-stream/poll-loop/
# push-loop——多份进程同时轮询同一份 dws token、同时写同一份 data/state.json，
# 这才是当晚 last_poll_took_s 涨到 600+ 秒、"采集器可能停了"反复报警的真正原因，
# 不是采集器真的挂了。
LABEL_PREFIX="${DTWATCH_LABEL_PREFIX:-com.workos.dtwatch}"
LABEL_AT="$LABEL_PREFIX.at"
LABEL_POLL="$LABEL_PREFIX.poll"
LABEL_PUSH="$LABEL_PREFIX.push"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# 直接查这个 label，不要 `launchctl list | grep -q` ——
# grep -q 命中就退出会给 launchctl 一个 SIGPIPE，配上 pipefail 会假报「未加载」
launchd_loaded() { launchctl list "$1" >/dev/null 2>&1; }

write_plist() {  # $1=label  $2=参数...（run.sh 的子命令）
  local label="$1"; shift
  local plist="$LA_DIR/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    echo '    <string>/bin/bash</string>'
    echo "    <string>$BASE/run.sh</string>"
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>$BASE</string>"
    echo '  <key>EnvironmentVariables</key><dict>'
    echo '    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>'
    echo "    <key>HOME</key><string>$HOME</string>"
    echo '  </dict>'
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo '  <key>ThrottleInterval</key><integer>30</integer>'
    echo "  <key>StandardOutPath</key><string>$DATA/launchd.$label.out</string>"
    echo "  <key>StandardErrorPath</key><string>$DATA/launchd.$label.err</string>"
    echo '</dict></plist>'
  } > "$plist"
  echo "$plist"
}

at_stream() {
  # dws 的 consume 退出后自己带退订，正常结束不会泄漏订阅；这里只负责重连
  local backoff=5
  while true; do
    echo "[$(date '+%F %T')] at-stream 启动" >> "$LOG_AT"
    dws event consume user_im_message_receive_at --flatten -f ndjson \
      >> "$DATA/at_events.ndjson" 2>> "$LOG_AT"
    echo "[$(date '+%F %T')] at-stream 退出 rc=$?，${backoff}s 后重连" >> "$LOG_AT"
    sleep "$backoff"
    backoff=$(( backoff < 120 ? backoff * 2 : 120 ))
  done
}

poll_loop() {
  local interval="$1"
  while true; do
    "$PY" "$BASE/dtwatch.py" poll >> "$LOG_POLL" 2>&1
    sleep "$interval"
  done
}

push_loop() {
  # dtcc.py push-loop 自己就是常驻循环（内部按墙钟判间隔，不用长 sleep，
  # 免得 macOS 睡眠把它冻住），这里只负责崩了重起
  local backoff=5
  while true; do
    "$PY" "$BASE/dtcc.py" push-loop --interval "${1:-8}" >>"$LOG_PUSH" 2>&1
    echo "[$(date '+%F %T')] [push] 退出 rc=$?，${backoff}s 后重起" >> "$LOG_PUSH"
    sleep "$backoff"
    backoff=$(( backoff < 60 ? backoff * 2 : 60 ))
  done
}

case "${1:-}" in
  at-stream) at_stream ;;
  poll-loop) poll_loop "${2:-300}" ;;
  push-loop) push_loop "${2:-8}" ;;

  start)
    interval="${2:-300}"
    if [ ! -f "$DATA/state.json" ]; then
      echo "state.json 不存在，先跑一次 init"
      "$PY" "$BASE/dtwatch.py" init
    fi
    if alive "$PID_AT"; then
      echo "at-stream 已在跑 (pid $(cat "$PID_AT"))"
    else
      nohup "$BASE/run.sh" at-stream >/dev/null 2>&1 &
      echo $! > "$PID_AT"; echo "at-stream 已启动 pid $!"
    fi
    if alive "$PID_POLL"; then
      echo "poll-loop 已在跑 (pid $(cat "$PID_POLL"))"
    else
      nohup "$BASE/run.sh" poll-loop "$interval" >/dev/null 2>&1 &
      echo $! > "$PID_POLL"; echo "poll-loop 已启动 pid $!（间隔 ${interval}s）"
    fi
    if alive "$PID_PUSH"; then
      echo "push-loop 已在跑 (pid $(cat "$PID_PUSH"))"
    else
      nohup "$BASE/run.sh" push-loop 8 >/dev/null 2>&1 &
      echo $! > "$PID_PUSH"; echo "push-loop 已启动 pid $!（间隔 8s）"
    fi
    ;;

  stop)
    for f in "$PID_AT" "$PID_POLL" "$PID_PUSH"; do
      if alive "$f"; then
        pid=$(cat "$f")
        # 先杀 wrapper，再温和地收掉它拉起的 dws（不要 kill -9，会漏掉退订）
        pkill -TERM -P "$pid" 2>/dev/null
        kill -TERM "$pid" 2>/dev/null
        echo "已停 $(basename "$f" .pid) pid $pid"
      fi
      rm -f "$f"
    done
    pkill -TERM -f "dws event consume user_im_message_receive_at" 2>/dev/null
    ;;

  install)
    # 交给 launchd 托管：开机自启 + 挂了自动拉起。装之前先收掉手工起的，避免跑两份
    interval="${2:-300}"
    if alive "$PID_AT" || alive "$PID_POLL"; then
      echo "先停掉手工启动的进程……"; "$BASE/run.sh" stop
    fi
    [ -f "$DATA/state.json" ] || "$PY" "$BASE/dtwatch.py" init
    mkdir -p "$LA_DIR"
    p1=$(write_plist "$LABEL_AT"   at-stream)
    p2=$(write_plist "$LABEL_POLL" poll-loop "$interval")
    p3=$(write_plist "$LABEL_PUSH" push-loop 8)
    for pair in "$LABEL_AT:$p1" "$LABEL_POLL:$p2" "$LABEL_PUSH:$p3"; do
      label="${pair%%:*}"; plist="${pair#*:}"
      launchctl bootout "gui/$UID/$label" 2>/dev/null
      if launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null; then
        echo "已装 $label"
      elif launchctl load -w "$plist" 2>/dev/null; then
        echo "已装 $label（load 兼容路径）"
      else
        echo "装 $label 失败，手动看: launchctl bootstrap gui/$UID $plist"
      fi
    done
    echo "轮询间隔 ${interval}s。开机自动起，进程挂了 launchd 会拉回来。"
    ;;

  uninstall)
    for label in "$LABEL_AT" "$LABEL_POLL" "$LABEL_PUSH"; do
      launchctl bootout "gui/$UID/$label" 2>/dev/null || \
        launchctl unload -w "$LA_DIR/$label.plist" 2>/dev/null
      rm -f "$LA_DIR/$label.plist"
      echo "已卸 $label"
    done
    pkill -TERM -f "dws event consume user_im_message_receive_at" 2>/dev/null
    ;;

  status)
    if launchd_loaded "$LABEL_AT" || launchd_loaded "$LABEL_POLL" || launchd_loaded "$LABEL_PUSH"; then
      echo "托管方式：launchd（开机自启）"
      launchd_loaded "$LABEL_AT"   && echo "  $LABEL_AT   已加载" || echo "  $LABEL_AT   未加载"
      launchd_loaded "$LABEL_POLL" && echo "  $LABEL_POLL 已加载" || echo "  $LABEL_POLL 未加载"
      launchd_loaded "$LABEL_PUSH" && echo "  $LABEL_PUSH 已加载" || echo "  $LABEL_PUSH 未加载"
    else
      echo "托管方式：手工（run.sh start，重启电脑就没了）"
    fi
    alive "$PID_AT"   && echo "at-stream  运行中 pid $(cat "$PID_AT")"   || echo "at-stream  无手工进程"
    alive "$PID_POLL" && echo "poll-loop  运行中 pid $(cat "$PID_POLL")" || echo "poll-loop  无手工进程"
    alive "$PID_PUSH" && echo "push-loop  运行中 pid $(cat "$PID_PUSH")" || echo "push-loop  无手工进程"
    pgrep -fl "dws event consume user_im_message_receive_at" 2>/dev/null | head -3
    "$PY" "$BASE/dtwatch.py" status
    ;;

  tail) tail -f "$LOG_POLL" ;;

  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
