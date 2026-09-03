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

# 五种角色状态各给一个不同记号。`never` 和 `unknown` 故意不共用 ——
# 「从没干过活」是结论，「读不出」是没有结论，屏幕上必须能分开。
ROLE_MARK = {"missing": "✗", "never": "○", "stale": "◐", "unknown": "?", "live": "●"}

# 舰队编组：一个 tmux session 画一个框，框里是它的成员 pane。
# 组数和成员数都**不设上限**（2026-09-03 他明确要「开放，都画出来」）。
# 省略只在宽度真的放不下一个框时才发生，而那时也要报数。
GROUP_MAX = None             # None = 全画
GROUP_ROWS = None            # None = 全列
GROUP_BAR = 8                # 框里的条比外面窄，给坐标让位
BOX_MIN = 34                 # 一个框最少要这么宽才装得下一行成员：
                             # 边框2 + 记号1 + 空1 + 角色6 + 空1 + 坐标6 + 空1
                             #        + 条8 + 空1 + 数字5 ≈ 32，留 2 余量
BOX_GAP = 2                  # 框与框之间的空隙
MEMBER_MARK = {"running": "▶", "never": "○", "unknown": "?", "idle": "·"}


def member_mark(p: dict) -> str:
    """成员那一列的记号。

    **`unknown` 和 `never` 必须分得开**（跟 `console.role_state` 同一条规矩）：
    上下文读不出是「没有结论」，0k 是「有结论、结论是从没干过活」。
    把前者画成后者就是编造。
    """
    if (p.get("status") or "") == "running":
        return MEMBER_MARK["running"]
    kb, _ = parse_ctx(p.get("ctx"))
    if kb is None:
        return MEMBER_MARK["unknown"]
    if kb == 0:
        return MEMBER_MARK["never"]
    return MEMBER_MARK["idle"]


def _kb(p: dict) -> int:
    kb, _ = parse_ctx(p.get("ctx"))
    return kb or 0


def group_panes(panes: list, cap=GROUP_MAX, rows=GROUP_ROWS) -> tuple[list, int, int]:
    """按 tmux session 编组。**纯函数** → `(组, 被省掉的组数, 被省掉的成员数)`。

    组间排序：**有声明角色的排最前**（那是舰队正式编制，不是随手开的窗口），
    其次是有正在跑的，再次按上下文总量。组内同理。

    省略必须报数。一个框列 5 个而实际有 10 个，却不说，
    读的人会以为那就是全部 —— 那跟把 625 条投不出去的消息说成 0 条是一个错。
    """
    buckets: dict = {}
    for p in panes or []:
        buckets.setdefault(p.get("session") or "?", []).append(p)

    def prank(p):
        return (0 if p.get("role") else 1,
                0 if (p.get("status") or "") == "running" else 1,
                -_kb(p), p.get("coord") or "")

    def grank(kv):
        name, ms = kv
        return (0 if any(m.get("role") for m in ms) else 1,
                0 if any((m.get("status") or "") == "running" for m in ms) else 1,
                -sum(_kb(m) for m in ms), name)

    ordered = sorted(buckets.items(), key=grank)
    # cap/rows 为 None = 不限。用 len() 代替 None 做切片上界，
    # 这样下面「被省掉几个」的算式对两种情况都成立，不用分支。
    cap = len(ordered) if cap is None else cap
    out, lost_m = [], 0
    for name, ms in ordered[:cap]:
        ms = sorted(ms, key=prank)
        keep = len(ms) if rows is None else rows
        # 统计一律**按全组算**，不按画出来的几行算。
        # 第一版拿截断后的列表数「干过活」，10 个 pane 的组只画 5 行就报「5 干过活」，
        # 读的人会以为剩下 5 个都是死的 —— 那是编造出来的结论。
        stat = {"total": len(ms),
                "live": sum(1 for m in ms if _kb(m) > 0),
                "run": sum(1 for m in ms if (m.get("status") or "") == "running"),
                "hot": sum(1 for m in ms if needs_compact(m))}
        lost_m += max(0, len(ms) - keep)
        out.append((name, ms[:keep], stat))
    lost_g = max(0, len(ordered) - cap)
    lost_m += sum(len(ms) for _, ms in ordered[cap:])
    return out, lost_g, lost_m


def fit_columns(width: int, box_min: int = BOX_MIN, gap: int = BOX_GAP) -> int:
    """这个宽度一行能并排放几个框。**纯函数。**

    至少 1 —— 窄到放不下一个框时也得画一个（挤一点好过整节消失）。
    """
    if width < box_min:
        return 1
    return max(1, (width + gap) // (box_min + gap))


def col_width(width: int, cols: int, gap: int = BOX_GAP) -> int:
    """并排 `cols` 个框时每个框多宽。余数留在右边不摊，摊了各框宽度不齐、列对不上。"""
    cols = max(1, cols)
    return max(BOX_MIN, (width - gap * (cols - 1)) // cols)


def right_fit(stat: dict, avail: int) -> str:
    """框右上角的统计，按剩余宽度降级。**信息优先级：总数 > 干过活 > 在跑。**

    降到放不下就给空串 —— 让 `box_lines` 把整段丢掉，也比截成
    「6 干过」这种半句话好。
    """
    run, hot = stat.get("run") or 0, stat.get("hot") or 0
    # 「该压」放在最前面参与降级：框窄到放不下逐行提示时，
    # 这个数字就是唯一还看得见的「有人该压了」，不能跟着一起没。
    h_long = f" · {hot} 该压" if hot else ""
    h_short = f" · {hot}压" if hot else ""
    forms = [f"{stat['total']} pane · {stat['live']} 干过活"
             + (f" · {run} 跑" if run else "") + h_long,
             f"{stat['total']}p · {stat['live']}活"
             + (f" · {run}▶" if run else "") + h_short,
             f"{stat['total']}/{stat['live']}" + h_short,
             f"{stat['total']}" + (f"·{hot}压" if hot else "")]
    for f in forms:
        if display_width(f) + 2 <= avail:
            return f
    return ""


def pack(items: list, per_row: int) -> list:
    """切成每行 `per_row` 个。**纯函数。**"""
    per_row = max(1, per_row)
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def box_lines(title: str, right: str, body: list, width: int) -> list:
    """画一个框。**纯函数**，只吐字符串，不知道颜色也不知道终端。

    `width` 是整个框占的列数（含两条竖边）。标题和右上角的统计压进上边框里，
    压不下就先砍右边的统计 —— 标题是「这是谁」，比数字重要。
    """
    width = max(12, width)
    inner = width - 2
    head = f" {title} "
    tail = f" {right} " if right else ""
    if display_width(head) + display_width(tail) > inner:
        tail = ""
    if display_width(head) > inner:
        head = " " + clip(title, inner - 2) + " "
    fill = inner - display_width(head) - display_width(tail)
    out = ["┌" + head + "─" * max(0, fill) + tail + "┐"]
    for b in body:
        out.append("│" + pad(clip(b, inner), inner) + "│")
    out.append("└" + "─" * inner + "┘")
    return out


COMPACT_HINT = "该压了"
COMPACT_MARK = "!"           # 框窄到放不下「该压了」时，退到记号列的一个字
MIN_COORD = 8                # 坐标列再窄就认不出是哪个窗口了，宁可砍提示
MEMBER_FIXED = 24            # 成员行除坐标外的固定开销，见 member_line 的注释


def tail_for(width: int, any_hot: bool) -> int:
    """成员行给「该压了」留几列。**纯函数。**

    留不下就返回 0 —— 那时 `member_line` 把提示压到记号列（`!`），
    框头的 `· N 该压` 也还在。**三种宽度下都还看得见「有人该压了」，
    只是形状不同。** 2026-09-03 第一版没做这个判断，`该压了` 在 37 宽的框里
    被截成「该…」，等于又把提示弄丢了一次。
    """
    if not any_hot:
        return 0
    full = 2 + display_width(COMPACT_HINT)
    return full if width - MEMBER_FIXED - full >= MIN_COORD else 0


def needs_compact(p: dict) -> bool:
    """够不够得上「该压了」。读不出的一律不报 —— 没有结论就不催人干活。"""
    kb, _ = parse_ctx(p.get("ctx"))
    return kb is not None and kb >= COMPACT_KB


def member_line(p: dict, width: int, tail_w: int = 0) -> str:
    """框里的一行成员。坐标去掉 session 前缀 —— 那已经写在框的标题上了。

    `tail_w` 是「该压了」那一列预留的宽度，**由调用方按整个框统一算**：
    框里只要有一个 pane 超线，这一框每行都留这么宽，框内的列才对得齐。
    留 0 就是这框里没人超线，一列都不浪费。
    """
    coord = p.get("coord") or "?"
    short = coord.split(":", 1)[1] if ":" in coord else coord
    kb, pct = parse_ctx(p.get("ctx"))
    num = "   — " if kb is None else f"{kb:>4}k"
    role = p.get("role") or ""
    # 固定列：记号1 + 空1 + 角色6 + 空1 + …坐标… + 空1 + 条8 + 空1 + 数字5 = 24
    cw = max(6, width - MEMBER_FIXED - tail_w)
    hot = needs_compact(p)
    tail = ""
    if tail_w:
        # 前导空格必须在字符串里，不能靠 pad —— pad 是左对齐补右边，
        # 那样会渲染成「442k该压了」，数字和提示黏在一起。
        tail = pad(("  " + COMPACT_HINT) if hot else "", tail_w)
    # 没给提示留位置时，把它压进记号列。`▶`（正在跑）优先 —— 一个正在跑的
    # 会话你现在也压不了它，先知道它在跑更有用。
    mark = member_mark(p)
    if hot and not tail_w and mark == MEMBER_MARK["idle"]:
        mark = COMPACT_MARK
    return (f"{mark} {pad(clip(role, 6), 6)} "
            f"{pad(clip(short, cw), cw)} {bar(pct, GROUP_BAR)} {num}{tail}")
ROLE_TONE = {"missing": "bad", "never": "bad", "stale": "warn",
             "unknown": "mute", "live": "ok"}


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

    # ---- 角色：声明了六个，实际有几个真干过活
    roles = snap.get("roles")
    if roles is None:
        row("角色", "读不出", "bad")
    else:
        bad = [r for r in roles if r["state"] != "live"]
        row("角色", f"{len(roles) - len(bad)}/{len(roles)} 在干活",
            "ok" if not bad else "warn")
        for r in bad:
            row("", f"{ROLE_MARK.get(r['state'], '?')} {pad(r['role'], 8)} "
                    f"{pad(clip(r['coord'] or '—', 20), 20)} {r['note']}",
                ROLE_TONE.get(r["state"], "warn"))

    # ---- 舰队：一个 tmux session 一个框，框里是它的成员
    panes = snap.get("panes")
    if panes is None:
        row("舰队", "读不出", "bad")
    else:
        groups, lost_g, lost_m = group_panes(panes)
        if not groups:
            row("舰队", "没有 Claude pane", "mute")
        grid = inner - 2                                 # 网格总宽，跟分隔线对齐
        cols = fit_columns(grid)
        bw = col_width(grid, cols)
        for chunk in pack(groups, cols):
            # 同一行里所有框拉到一样高：上边框、成员、下边框才逐行对得齐。
            # 只在底部补空行会让下边框错位，看着就是没做完。
            h = max(len(ms) for _, ms, _ in chunk)
            drawn = []
            for name, ms, stat in chunk:
                tw = tail_for(bw - 4, any(needs_compact(m) for m in ms))
                body = [member_line(m, bw - 4, tw) for m in ms]
                body += [""] * (h - len(ms))
                drawn.append(box_lines(name, right_fit(stat, bw - display_width(name) - 4),
                                       body, bw))
            sep = " " * BOX_GAP
            for i in range(h + 2):
                # tone 按整行给。成员行里几个框状态各不相同，**给谁的颜色都是撒谎**，
                # 所以一律中性 —— 状态全写在字面上（▶ 在跑 / ○ 从没干过活 /
                # ? 读不出 / 该压了），不靠颜色传达。
                tone = "accent" if i == 0 else ("mute" if i == h + 1 else "")
                L.append(("  " + sep.join(d[i] for d in drawn), tone))
        if lost_g or lost_m:
            row("", f"另有 {lost_g} 个小组 · {lost_m} 个 pane 未画", "mute")

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
    # 角色**必须**在 panes 之后算，而且 panes 读不出时角色也只能是读不出 ——
    # 拿一个空 pane 列表去比对，会把六个角色全报成 missing（假警报）。
    if snap.get("panes") is None:
        snap["roles"] = None
    else:
        grab("roles", lambda: console.role_report(
            snap["panes"], console.declared_roles(), time.time()))
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
