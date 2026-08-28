#!/usr/bin/env python3
"""终端仪表盘 —— 进去时各项逐个点亮，之后常驻自刷。

跟另外两块屏的分工：
  board_html.py   面向**消息**：我该处理什么、谁在等我
  console.py      面向**系统**，输出 HTML：给浏览器常驻一个标签页
  dash.py         面向**系统**，输出终端：手边这块屏，不用切到浏览器

**状态判定一律不在这里重算** —— 全部走 console.py 的 collect_* 和 fleet_up 的
svc_status。这里只负责「怎么画」。加一个指标应该加在 console.py 的采集函数里，
两块屏同时得到，而不是在这儿再实现一遍（那就有两份真相了）。

用法：
    python3 dash.py              # 启动序列 + 常驻（q 或 Ctrl-C 退出）
    python3 dash.py --once       # 画一帧就退出，可以管道出去
    python3 dash.py --no-boot    # 跳过启动动画直接进面板
    python3 dash.py --plain      # 不上色（管道/非 tty 时自动就是这个）

刷新与动画是**两个频率**：采集每 REFRESH_SECONDS 一次（会跑 tmux capture，是整个
程序最慢的一步），画面每 FRAME_SECONDS 一次（纯计算，让进度条能平滑追上新值）。
"""

from __future__ import annotations

import argparse
import os
import re
import select
import shutil
import signal
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import console  # noqa: E402  单一真相：采集与判定都在它那儿

try:
    import fleet  # noqa: E402  只为了拿 AUTOCOMPACT_THRESHOLD_KB 这条阈值
except Exception:                                    # pragma: no cover
    fleet = None

try:
    import dtwatch  # noqa: E402  哨兵积压
except Exception:                                    # pragma: no cover
    dtwatch = None

REFRESH_SECONDS = 2.0
FRAME_SECONDS = 0.1
BOOT_STEP_SECONDS = 0.045
CTX_ROWS = 6                 # 会话按占用降序，只显示前几个
BAR_CELLS = 12
COORD_W = 20                  # 会话坐标列宽，超了截断（不截会把进度条整列推歪）
EASE = 0.28                  # 进度条每帧向目标值靠近的比例
MIN_WIDTH = 46

# 上下文超过这条线就标黄——跟 autocompact 真正用的阈值同一个数，
# 免得面板说「还行」而看门狗已经在压了。
COMPACT_KB = getattr(fleet, "AUTOCOMPACT_THRESHOLD_KB", 300)


# ------------------------------------------------------------------ 上色
#
# tone 只有四种，跟 console.py 的 css class 一一对应（它的 TONE 表直接复用）。

SGR = {"ok": "38;5;78", "warn": "38;5;179", "bad": "38;5;167",
       "mute": "38;5;244", "accent": "38;5;80", "": "0"}


def paint(text: str, tone: str, color: bool) -> str:
    """给一段文字上色。`color=False` 时原样返回——非 tty / --plain 走这条。"""
    if not color or not tone:
        return text
    return f"\x1b[{SGR.get(tone, '0')}m{text}\x1b[0m"


def tone_of_state(state: str) -> str:
    """服务状态字串 → tone。表在 console.TONE，不在这里维护第二份。"""
    return console.TONE.get(state, "mute")


# ------------------------------------------------------------------ 纯判据


def parse_ctx(s):
    """`"529k (53%)"` → `(529, 53)`。解析不到的那一半给 None，**不猜也不给 0**。

    为什么不复用 `fleet._ctx_kb`：那个只返回 kb，画进度条还要百分比。
    两边必须对同一批输入给出同一个 kb —— 有用例钉着（test_dash 里那条对账）。
    """
    if not s:
        return (None, None)
    kb = pct = None
    m = re.match(r"(\d+)k", s)
    if m:
        kb = int(m.group(1))
    m = re.search(r"\((\d+)%\)", s)
    if m:
        pct = int(m.group(1))
    return (kb, pct)


def bar(pct, cells: int = BAR_CELLS) -> str:
    """百分比 → 进度条。`pct is None` 时给一条空槽，**不画成 0%**——
    「读不出」和「真的空着」在屏幕上必须长得不一样，所以空槽用别的字符。"""
    if pct is None:
        return "╌" * cells
    filled = max(0, min(cells, round(cells * pct / 100.0)))
    return "█" * filled + "▁" * (cells - filled)


def ctx_rows(panes: list[dict], top: int = CTX_ROWS) -> tuple[list[dict], int]:
    """挑出要显示的会话：按占用降序，**同一个 pane 只留一行**，取前 top 个。

    去重不是洁癖：一个 pane 可能被多条记录指着（autocompact 就在这上面栽过，
    同一个窗口被选中压三次）。返回 `(行, 被省掉的个数)` —— 省掉多少必须能说出来，
    截断了却不说等于谎报「就这些」。
    """
    seen = set()
    uniq = []
    for p in panes:
        key = p.get("pane")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: (parse_ctx(p.get("ctx"))[0] is None,
                             -(parse_ctx(p.get("ctx"))[0] or 0),
                             p.get("coord") or ""))
    return uniq[:top], max(0, len(uniq) - top)


def fmt_age(seconds) -> str:
    """秒 → 「1天5时」「6分12秒」。`None` → `?`，不编。"""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分{s % 60}秒"
    if s < 86400:
        return f"{s // 3600}时{s % 3600 // 60}分"
    return f"{s // 86400}天{s % 86400 // 3600}时"


def clip(text: str, width: int) -> str:
    """按**显示宽度**截断（中文算两格），超了给省略号。"""
    if display_width(text) <= width:
        return text
    out, w = "", 0
    for ch in text:
        cw = 2 if is_wide(ch) else 1
        if w + cw > width - 1:
            return out + "…"
        out += ch
        w += cw
    return out


def is_wide(ch: str) -> bool:
    o = ord(ch)
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6)


def display_width(text: str) -> int:
    return sum(2 if is_wide(ch) else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """补空格到指定显示宽度（宽字符算两格，所以不能用 str.ljust）。"""
    gap = width - display_width(text)
    return text + " " * gap if gap > 0 else text


# ------------------------------------------------------------------ 组版
#
# compose 是纯函数：给它一份快照就得到一屏文字。屏幕上出现的每个数字都能追到
# snapshot 里的某个键，没有「顺手算一下」的。

LABEL_W = 10
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def compose(snap: dict, width: int, frame: int = 0, eased=None) -> list[tuple]:
    """一屏 → `[(文字, tone), …]`。`eased` 是每个 pane 当前画到的百分比。"""
    width = max(MIN_WIDTH, width)
    inner = width - 4
    eased = eased or {}
    L: list[tuple] = []

    def row(label: str, body: str, tone: str = ""):
        L.append(("  " + pad(label, LABEL_W) + clip(body, inner - LABEL_W), tone))

    stamp = snap.get("at") or ""
    title = "agent-fleet"
    L.append(("  " + pad(title, inner - display_width(stamp)) + stamp, "accent"))
    L.append(("  " + "─" * (inner - 2), "mute"))

    # ---- 自检：一行塞完，只有出问题的才展开
    checks = snap.get("checks")
    if checks is None:
        row("自检", "读不出", "bad")
    else:
        good = [c for c in checks if c["ok"]]
        bad = [c for c in checks if not c["ok"]]
        row("自检", f"{len(good)}/{len(checks)} 通过", "ok" if not bad else "warn")
        for c in bad:
            row("", "✗ " + c["label"] + ("  " + c["note"] if c["note"] else ""), "bad")

    # ---- 服务
    svcs = snap.get("services")
    if svcs is None:
        row("服务", "读不出", "bad")
    else:
        for i, s in enumerate(svcs):
            tone = tone_of_state(s["state"])
            mark = "●" if tone == "ok" else ("◐" if tone == "warn" else "○")
            body = f"{mark} {pad(s['name'], 16)} {pad(s['state'], 6)} {s['when']}"
            row("服务" if i == 0 else "", body, tone)

    # ---- 采集痕迹（含待拍板草稿，来自 console.collect_collector）
    coll = snap.get("collector")
    if coll is None:
        row("采集", "读不出", "bad")
    else:
        for i, c in enumerate(coll):
            row("采集" if i == 0 else "", f"{pad(c['k'], 14)} {c['v']}",
                "warn" if c.get("warn") else "")

    # ---- 哨兵
    st = snap.get("stale")
    if st is None:
        row("哨兵", "读不出", "bad")
    else:
        row("哨兵", f"点过名还没处理 {st} 条", "warn" if st else "ok")

    # ---- 会话上下文
    panes = snap.get("panes")
    if panes is None:
        row("会话", "读不出", "bad")
    else:
        rows, hidden = ctx_rows(panes)
        if not rows:
            row("会话", "没有 Claude pane", "mute")
        for i, p in enumerate(rows):
            kb, pct = parse_ctx(p.get("ctx"))
            shown = eased.get(p.get("pane"))
            num = "  —  " if kb is None else f"{kb:>4}k"
            tone = "warn" if (kb is not None and kb >= COMPACT_KB) else ""
            tail = "  该压了" if tone == "warn" else ""
            # 坐标必须先 clip 再 pad —— 只 pad 的话超宽的坐标会把后面整列推歪
            body = (f"{pad(clip(p['coord'], COORD_W), COORD_W)} "
                    f"{bar(pct if shown is None else shown)} {num}{tail}")
            row("会话" if i == 0 else "", body, tone)
        if hidden:
            row("", f"另有 {hidden} 个 pane 未显示（按占用取前 {CTX_ROWS} 个）", "mute")

    L.append(("  " + "─" * (inner - 2), "mute"))
    spin = SPIN[frame % len(SPIN)]
    foot = f"q 退出 · 每 {REFRESH_SECONDS:g} 秒自刷"
    L.append(("  " + pad(foot, inner - 2) + spin, "mute"))
    return L


# ------------------------------------------------------------------ IO


def snapshot(with_ctx: bool = True) -> dict:
    """跑一次采集。**每一项单独兜异常** —— 一项炸了不能把整屏带走，
    而且必须留成 `None`（屏幕上显示「读不出」），不能悄悄变成 0 或空列表。"""
    snap = {"at": time.strftime("%H:%M:%S")}

    def grab(key, fn):
        try:
            snap[key] = fn()
        except Exception:
            snap[key] = None

    grab("checks", console.collect_checks)
    grab("services", console.collect_services)
    grab("collector", console.collect_collector)
    grab("panes", lambda: console.collect_panes(with_ctx))
    grab("stale", lambda: len(dtwatch.stale_at_me(None)) if dtwatch else None)
    return snap


# ------------------------------------------------------------------ 终端


class Screen:
    """备用屏 + 隐藏光标 + cbreak，退出时**一定**还原（异常路径也走 finally）。"""

    def __init__(self, color: bool, alt: bool):
        self.color, self.alt = color, alt
        self.saved = None
        self.height = 0

    def __enter__(self):
        if self.alt:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
        try:
            import termios
            import tty
            self.saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            self.saved = None
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)
            except Exception:
                pass
        if self.alt:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        return False

    def draw(self, lines: list[tuple], reveal=None):
        """画到屏上。`reveal` 只画前 n 行——启动序列就是靠它逐行点亮的。"""
        n = len(lines) if reveal is None else min(reveal, len(lines))
        buf = ["\x1b[H\x1b[2J"] if self.alt else []
        for i, (text, tone) in enumerate(lines[:n]):
            hot = reveal is not None and i == n - 1
            body = paint(text, tone, self.color)
            if hot and self.color:
                body = "\x1b[1m" + body + "\x1b[0m"
            buf.append(body + ("\x1b[K" if self.alt else "") + "\n")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def quit_pressed(self) -> bool:
        if self.saved is None:
            return False
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0)
            return bool(r) and sys.stdin.read(1) in ("q", "Q", "\x03")
        except Exception:
            return False


def term_width() -> int:
    return shutil.get_terminal_size((100, 30)).columns


def step_eased(eased: dict, panes, ratio: float = EASE) -> dict:
    """让进度条**平滑追**目标值，而不是跳变。纯函数：旧值 + 目标 → 新值。

    只对读得出百分比的 pane 动；读不出的从字典里去掉，让 `bar(None)` 画空槽。
    """
    out = {}
    for p in panes or []:
        key = p.get("pane")
        _, pct = parse_ctx(p.get("ctx"))
        if pct is None:
            continue
        cur = eased.get(key, 0.0)
        out[key] = cur + (pct - cur) * ratio if abs(pct - cur) > 0.5 else float(pct)
    return out


def run(args) -> int:
    color = not args.plain and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    alt = sys.stdout.isatty() and not args.once

    if args.once:
        snap = snapshot(with_ctx=not args.fast)
        with Screen(color, alt=False) as sc:
            sc.draw(compose(snap, term_width()))
        return 0

    stop = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.__setitem__("now", True))

    with Screen(color, alt) as sc:
        snap = snapshot(with_ctx=not args.fast)
        eased: dict = {}
        lines = compose(snap, term_width(), 0, eased)

        if not args.no_boot:
            for n in range(1, len(lines) + 1):
                if stop["now"]:
                    return 0
                eased = step_eased(eased, snap.get("panes"))
                sc.draw(compose(snap, term_width(), 0, eased), reveal=n)
                time.sleep(BOOT_STEP_SECONDS)

        frame = 0
        last = time.monotonic()
        while not stop["now"]:
            if sc.quit_pressed():
                break
            if time.monotonic() - last >= REFRESH_SECONDS:
                snap = snapshot(with_ctx=not args.fast)
                last = time.monotonic()
            eased = step_eased(eased, snap.get("panes"))
            sc.draw(compose(snap, term_width(), frame, eased))
            frame += 1
            time.sleep(FRAME_SECONDS)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="agent-fleet 终端仪表盘")
    ap.add_argument("--once", action="store_true", help="画一帧就退出（可管道）")
    ap.add_argument("--no-boot", action="store_true", help="跳过启动动画")
    ap.add_argument("--plain", action="store_true", help="不上色")
    ap.add_argument("--fast", action="store_true",
                    help="不采上下文占用（省掉每个 pane 一次 tmux capture）")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
