#!/usr/bin/env python3
"""board 的网页版 —— 生成一个静态 HTML，浏览器开一个标签页常驻。

为什么不做成 web 服务：这台机器上已经有一堆常驻进程了，再加一个端口、一个框架、
一份依赖，只为了看几张表不值得。**一个 HTML 文件 + 浏览器自己定时重载**就够，
挂掉了也不影响任何东西，重新跑一次脚本就有。

数据全部复用 `fleet.py`（那边是数据层，这里只管渲染），所以两边永远一致，
不会出现「终端说 4 条、网页说 3 条」。

用法：
    python3 board_html.py            # 生成一次，打印文件路径
    python3 board_html.py --loop 10  # 每 10 秒重生成一次（常驻）
    python3 board_html.py --open     # 生成并用默认浏览器打开

摆脱终端之后放回来的东西（终端版为了对齐砍掉的）：
  - 心跳的最后一句**完整显示**，不再只取第一句
  - @你的消息**完整正文**，图片直接内嵌显示
  - 调度流水给到 30 条
"""

import argparse
import hashlib
import json
import html
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet                                                    # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "data", "board")
OUT = os.path.join(OUT_DIR, "index.html")
IMG_DIR = os.path.join(OUT_DIR, "img")


# ---------------------------------------------------------------- 图片

def fetch_image(msg: dict, media_id: str) -> str:
    """把钉钉图片下到本地，返回相对路径。已经下过就不重下。

    图片是能下的（`dws chat message download-media`），所以网页版没理由
    只显示一行「[图片]」—— 有人发过来的就是一整张聊天记录截图，
    不看图根本不知道他在说什么。
    """
    os.makedirs(IMG_DIR, exist_ok=True)
    name = hashlib.sha1(media_id.encode()).hexdigest()[:16] + ".png"
    path = os.path.join(IMG_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return f"img/{name}"
    cid, mid = msg.get("cid", ""), msg.get("id", "")
    if not (cid and mid):
        return ""
    try:
        subprocess.run(["dws", "chat", "message", "download-media",
                        "--type", "mediaId", "--resource-id", media_id,
                        "--message-id", mid, "--open-conversation-id", cid,
                        "--output", path, "--format", "json"],
                       capture_output=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"img/{name}" if os.path.exists(path) else ""


# ---------------------------------------------------------------- 渲染件

def esc(t) -> str:
    return html.escape(str(t or ""))


def card(title: str, body: str, count: str = "", tone: str = "") -> str:
    badge = f'<span class="count {tone}">{esc(count)}</span>' if count else ""
    return (f'<section class="card">'
            f'<h2>{esc(title)}{badge}</h2>{body}</section>')


def empty(msg: str) -> str:
    return f'<p class="empty">{esc(msg)}</p>'


def table(headers: list, rows: list, cls: str = "") -> str:
    if not rows:
        return ""
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = []
    for r in rows:
        tds = "".join(f"<td>{cell}</td>" for cell in r)   # cell 已自行转义
        trs.append(f"<tr>{tds}</tr>")
    return (f'<div class="scroll"><table class="{cls}">'
            f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody>"
            f"</table></div>")


def pill(text: str, tone: str = "") -> str:
    return f'<span class="pill {tone}">{esc(text)}</span>'


def state_pill(rec: dict, stale: int) -> str:
    """会话状态。四层证据对应四种说法，别笼统说「已关闭」
    —— 状态文件会缩水，那样会把活着的会话冤枉了。"""
    if not rec:
        return pill("查不到", "muted")
    k = rec.get("known")
    if k == "gone":
        return pill("pane 已关", "bad")
    if k == "remembered":
        return pill("pane 在·状态未知", "muted")
    if k == "transcript":
        return (pill(f"活着 · {fleet.fmt_age(rec['age'])}前写过", "ok")
                if rec["age"] < 3600 else pill(f"静默 {fleet.fmt_age(rec['age'])}", "muted"))
    if rec["state"] == "busy":
        return pill("在跑", "run")
    if rec["state"] == "blocked":
        return pill("卡住", "bad")
    if rec["age"] > stale:
        return pill(f"静默 {fleet.fmt_age(rec['age'])}", "muted")
    return pill("闲着", "ok")


# ---------------------------------------------------------------- 主渲染

def render(stale: int = 900, threshold: int = 8, min_vol: int = 12,
           flow_n: int = 30) -> str:
    live = fleet.sessions()
    hits, unrouted, undeliv, cfg = fleet.inbox_stats()
    route = cfg.get("route") or {}
    low_pri = set(cfg.get("low_priority_conversations") or [])
    tasks = fleet.tasks_load()
    ats = fleet.at_me_open(limit=12)
    now = fleet.ts(fleet.now())

    # ---- 先算「要他处理」的总数，决定顶部那句话 ----
    stuck = []
    for sid, n in undeliv.items():
        rec = live.get(sid) or {}
        if not rec or (rec.get("known") == "live"
                       and rec["age"] > stale and rec["state"] != "busy"):
            stuck.append((sid, n, rec))
    pend = [t for t in tasks.values() if t.get("status") == "pending"]
    disp = [t for t in tasks.values() if t.get("status") == "dispatched"]
    backlog = [(cv, d) for cv, d in unrouted.items()
               if d["open"] >= threshold and cv not in low_pri]
    total = len(ats) + len(stuck) + len(pend) + len(backlog)

    parts = []

    # ① 结论
    if total:
        parts.append(f'<header class="verdict warn"><span class="big">'
                     f'{total} 件要你处理</span><time>{esc(now)}</time></header>')
    else:
        parts.append(f'<header class="verdict ok"><span class="big">无事</span>'
                     f'<time>{esc(now)}</time></header>')

    # ② @你 —— 完整正文 + 图片内嵌
    if ats:
        blocks = []
        for r in ats:
            body, media = fleet.split_media(fleet.strip_ats(r.get("text", "")))
            imgs = ""
            for mid in media:
                src = fetch_image(r, mid)
                # 缩略图，点击才放大 —— 原尺寸截图会占满整屏，把别的信息挤没了
                imgs += (f'<img class="thumb" src="{esc(src)}" loading="lazy" '
                         f'onclick="zoom(this.src)" title="点击放大">'
                         if src else '<p class="empty">（图片下载失败）</p>')
            blocks.append(
                f'<article class="msg"><div class="meta">'
                f'<b>{esc(r.get("sender", "?"))}</b>'
                f'<span class="conv">{esc(r.get("conv", "?"))}</span>'
                f'<time>{esc(r.get("time", ""))}</time></div>'
                f'<div class="body">{esc(body) or "（无文字）"}</div>{imgs}</article>')
        parts.append(card("@你", "".join(blocks), f"{len(ats)} 条未处理", "warn"))
    else:
        parts.append(card("@你", empty("没有未处理的 @我 消息"), "0", "ok"))

    # ③ 路由表
    rows = []
    for key, v in sorted(hits.items(), key=lambda x: -x[1]["n"]):
        sid = (route.get(key) or {}).get("session", "")
        lab = (route.get(key) or {}).get("label", "")
        rec = live.get(sid) or {}
        who = (esc(fleet.disp_of(rec, short=True)) if rec
               else f'<span class="muted-txt">{esc(lab)}（sid {esc(sid[:8])}…）</span>')
        rows.append([esc(key), esc(v["n"]),
                     esc(v["open"]) if v["open"] else '<span class="muted-txt">—</span>',
                     who, state_pill(rec, stale)])
    parts.append(card("路由", table(["规则（群名/发送人）", "命中", "未处理", "接管会话", "状态"],
                                    rows) or empty("route 表是空的"),
                      f"{len(route)} 条规则"))

    # ④ 待接管 —— **只列还有未处理的**。
    # 判据是「这一行能不能让人现在做点什么」：未处理为 0 就什么也做不了，
    # 挂在那儿只会让人以为有事（原来靠「总量」挑候选，结果哨兵一清完队列，
    # 整区就变成一排「量大，可考虑分出去」在刷存在感）。
    # 「总量」留作背景数，只在这条确实有未处理时才带出来。
    cands = sorted([(cv, d) for cv, d in unrouted.items()
                    if cv not in low_pri and d["open"] > 0],
                   key=lambda x: -x[1]["open"])
    if cands:
        rows = []
        for cv, d in cands[:12]:
            hint = ("正在堆积 → 该配 route 给专属会话" if d["open"] >= threshold
                    else "哨兵接得住")
            rows.append([esc(cv), esc(d["open"]),
                         f'<span class="muted-txt">{esc(d["n"])}</span>',
                         pill(hint, "bad" if d["open"] >= threshold else "muted")])
        parts.append(card("待接管（没配路由，全靠哨兵接）",
                          table(["钉钉会话", "未处理", "累计", "建议"], rows),
                          f"{len(cands)} 个"))

    # ⑤ 队列
    rows = []
    for sid, n in sorted(undeliv.items(), key=lambda x: -x[1]):
        rec = live.get(sid) or {}
        if not rec:
            how = "查不到这个会话，核一下 route 表的 sid"
        elif rec.get("known") == "gone":
            how = "pane 已关，得换个会话接"
        elif rec["state"] == "busy":
            how = "它收尾时会自动接"
        elif rec.get("known") == "live" and rec["age"] > stale:
            how = f"该 wake：fleet.py wake {rec.get('project') or sid[:8]}"
        else:
            how = "下次收尾自动接"
        rows.append([esc(fleet.disp_of(rec, short=True)) if rec else f"（sid {esc(sid[:8])}…）",
                     f"{esc(n)} 条", state_pill(rec, stale),
                     pill(how, "bad" if "wake" in how or "查不到" in how else "")])
    parts.append(card("队列（归它但还没投递）",
                      table(["会话", "条数", "状态", "怎么办"], rows)
                      or empty("没有待投递的"), f"{sum(undeliv.values())} 条"))

    # ⑥ 台账 / 卡住
    rows = []
    for sid, n, rec in stuck:
        rows.append([pill("卡住", "bad"),
                     esc(fleet.disp_of(rec, short=True)) if rec else "（会话已关）",
                     esc(f"名下 {n} 条没投递"),
                     esc(f"静默 {fleet.fmt_age(rec['age'])}") if rec else ""])
    for t in pend[:12]:
        rows.append([pill("未派", "warn"), esc(t.get("target") or "unassigned"),
                     esc(t.get("text", "")), esc(t["id"])])
    for t in disp[:12]:
        try:
            age = fleet.fmt_age(int((fleet.now() -
                                     fleet.parse_ts(t.get("ts", ""))).total_seconds()))
        except (ValueError, TypeError):
            age = "?"
        rows.append([pill("待回", ""), esc(t.get("target", "")),
                     esc(t.get("text", "")), esc(f"{t['id']} · {age}")])
    parts.append(card("台账 / 卡住", table(["", "谁", "什么", "备注"], rows)
                      or empty("台账空，也没有会话卡住"),
                      f"未派 {len(pend)} · 待回 {len(disp)} · 卡住 {len(stuck)}"))

    # ⑦ 心跳 —— 最后一句完整显示（终端版为了对齐只能取第一句）
    rows = []
    order = sorted(live.values(), key=lambda r: (r["state"] != "busy", r["age"]))
    awake = [r for r in order
             if r.get("known") in ("live", "transcript") and r["age"] <= stale]
    for r in order:
        note = fleet.strip_md(r.get("note", ""))
        age = (fleet.fmt_age(r["age"]) if r.get("known") in ("live", "transcript")
               else "—")
        rows.append([esc(fleet.disp_of(r)), state_pill(r, stale), esc(age),
                     f'<span class="note">{esc(note)}</span>'])
    parts.append(card("心跳", table(["会话", "状态", "多久没动", "最后一句"], rows,
                                    "heartbeat") or empty("没有会话"),
                      f"{len(live)} 个 · {len(awake)} 个活跃"))

    # ⑧ 调度流水
    rows = []
    for when, kind, who, what in fleet.dispatch_flow(flow_n):
        rows.append([esc(when[11:19]), pill(kind, "flow"), esc(who),
                     esc(fleet.strip_md(what))])
    parts.append(card("调度流水（方括号里是判据）",
                      table(["时间", "动作", "从 → 到", "内容"], rows, "flow")
                      or empty("最近没有调度动作"), f"最近 {len(rows)} 条"))

    return "\n".join(parts), now


SHELL_VER = "shell-v2"

SHELL = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dtwatch board</title>
<!-- """ + SHELL_VER + """ -->
<style>
:root {
  --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --fg:#1c1f23; --dim:#6b7280;
  --warn:#b45309; --warn-bg:#fef3c7; --bad:#b91c1c; --bad-bg:#fee2e2;
  --ok:#15803d; --ok-bg:#dcfce7; --run:#1d4ed8; --run-bg:#dbeafe;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#15171a; --card:#1c1f23; --line:#2b2f36; --fg:#e6e8ea; --dim:#9aa3ad;
    --warn:#fbbf24; --warn-bg:#3a2e10; --bad:#f87171; --bad-bg:#3b1414;
    --ok:#4ade80; --ok-bg:#12301c; --run:#60a5fa; --run-bg:#12233f;
  }
}
* { box-sizing:border-box; }
html, body { background:var(--bg); }   /* 底色跟卡片一致，任何时候都不会露白 */
body {
  margin:0; padding:18px 18px 40px; color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;
}
.verdict {
  display:flex; align-items:baseline; gap:14px; padding:14px 18px;
  border-radius:10px; margin-bottom:16px; border:1px solid var(--line);
}
.verdict.warn { background:var(--warn-bg); border-color:var(--warn); }
.verdict.ok   { background:var(--ok-bg);   border-color:var(--ok); }
.verdict .big { font-size:20px; font-weight:650; }
.verdict.warn .big { color:var(--warn); }
.verdict.ok   .big { color:var(--ok); }
.verdict time { margin-left:auto; color:var(--dim); font-variant-numeric:tabular-nums; }
.card {
  background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin-bottom:14px;
}
.card h2 {
  margin:0 0 10px; font-size:14px; font-weight:650; letter-spacing:.02em;
  display:flex; align-items:center; gap:10px;
}
.count {
  font-weight:400; font-size:12px; color:var(--dim);
  border:1px solid var(--line); border-radius:20px; padding:1px 9px;
}
.count.warn { color:var(--warn); border-color:var(--warn); }
.scroll { overflow-x:auto; }          /* 窄窗口时表格自己滚，页面不横向滚 */
table { width:100%; border-collapse:collapse; font-size:13px; }
th {
  text-align:left; font-weight:500; color:var(--dim); padding:6px 10px 6px 0;
  border-bottom:1px solid var(--line); white-space:nowrap;
}
td { padding:7px 10px 7px 0; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:none; }
td:first-child, th:first-child { padding-left:2px; }
table.heartbeat td:nth-child(4) { color:var(--dim); }
table.flow td:nth-child(1) { color:var(--dim); font-variant-numeric:tabular-nums;
  white-space:nowrap; }
table.flow td:nth-child(3) { white-space:nowrap; }
.note, .muted-txt { color:var(--dim); }
.pill {
  display:inline-block; padding:1px 8px; border-radius:20px; font-size:12px;
  border:1px solid var(--line); color:var(--dim); white-space:nowrap;
}
.pill.bad  { color:var(--bad);  background:var(--bad-bg);  border-color:transparent; }
.pill.warn { color:var(--warn); background:var(--warn-bg); border-color:transparent; }
.pill.ok   { color:var(--ok);   background:var(--ok-bg);   border-color:transparent; }
.pill.run  { color:var(--run);  background:var(--run-bg);  border-color:transparent; }
.pill.muted { opacity:.75; }
.msg { border-bottom:1px solid var(--line); padding:10px 0; }
.msg:last-child { border-bottom:none; }
.msg .meta { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap;
  margin-bottom:4px; }
.msg .meta .conv { color:var(--dim); font-size:12px; }
.msg .meta time { color:var(--dim); font-size:12px; margin-left:auto;
  font-variant-numeric:tabular-nums; }
.msg .body { white-space:pre-wrap; word-break:break-word; }
/* 缩略图：默认小，点击才放大 —— 原尺寸截图会把别的信息挤没 */
.thumb {
  max-width:120px; max-height:90px; object-fit:cover; margin-top:6px;
  border:1px solid var(--line); border-radius:6px; cursor:zoom-in;
  display:inline-block; vertical-align:top;
}
dialog#lightbox {
  border:none; padding:0; background:transparent; max-width:96vw; max-height:96vh;
}
dialog#lightbox::backdrop { background:rgba(0,0,0,.72); }
dialog#lightbox img { max-width:96vw; max-height:92vh; border-radius:8px;
  display:block; cursor:zoom-out; }
.empty { color:var(--dim); margin:2px 0; }
#stamp {
  position:fixed; right:12px; bottom:10px; font-size:11px; color:var(--dim);
  background:var(--card); border:1px solid var(--line); border-radius:20px;
  padding:2px 10px; font-variant-numeric:tabular-nums; opacity:.85;
}
</style></head>
<body>
<main id="board"><p class="empty">正在读取…</p></main>
<div id="stamp">—</div>
<dialog id="lightbox" onclick="this.close()"><img id="lightbox-img" alt=""></dialog>
<script>
// 为什么不用 fetch：这个页面是 file:// 打开的，fetch/XHR 会被 CORS 挡死
// （浏览器把 file:// 当 opaque origin）。script 标签不受这个限制，
// 所以数据走 data.js，加载完回调换 DOM —— 不整页重载就不会闪、也不丢滚动位置。
function onBoardData() {
  if (!window.BOARD) return;
  document.getElementById('board').innerHTML = window.BOARD.html;
  document.getElementById('stamp').textContent = '更新于 ' + window.BOARD.at.slice(11);
}
function load() {
  var s = document.createElement('script');
  s.src = 'data.js?t=' + Date.now();          // 带时间戳，绕开缓存
  s.onload = function () { s.remove(); };
  s.onerror = function () {
    document.getElementById('stamp').textContent = '读不到 data.js';
    s.remove();
  };
  document.head.appendChild(s);
}
function zoom(src) {
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').showModal();
}
load();
setInterval(load, 30000);                      // 30 秒一次，够用又不吵
document.addEventListener('visibilitychange', function () {
  if (!document.hidden) load();                // 切回这个标签页时立刻刷一次
});
</script>
</body></html>
"""


DATA_JS = os.path.join(OUT_DIR, "data.js")


def write_once(args) -> str:
    """壳和数据分开写。

    **为什么不是整页 meta refresh**：整页重载会白屏闪一下、还会丢滚动位置。
    想只换内容区就得在页面里取新数据，而 `fetch()` 在 `file://` 下被 CORS 挡死
    （浏览器把 file:// 当 opaque origin）—— 所以走 `<script>` 标签这条路：
    script 不受 CORS 限制，同目录的 js 在 file:// 下加载得到。
    Python 这边把渲染好的 HTML 塞进 `data.js` 的一个字符串里，
    页面定时重新加载它、替换 DOM。渲染逻辑还留在 Python，不用重写成 JS。
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    body, now = render(args.stale_after, args.threshold, args.min_vol, args.flow)

    payload = json.dumps({"html": body, "at": now}, ensure_ascii=False)
    # 万一正文里出现字面的 </script>，会把这段 js 提前截断 —— 转义掉。
    # （现在的内容都过了 esc()，理论上不会有，但这是一行的保险）
    payload = payload.replace("</", "<\\/")
    tmp = DATA_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("window.BOARD = " + payload + ";\n"
                "if (window.onBoardData) window.onBoardData();\n")
    os.replace(tmp, DATA_JS)      # 原子替换，浏览器不会读到写了一半的

    # 壳只在缺失或版本变了时重写，免得每轮都动它（浏览器可能正开着）
    if not os.path.exists(OUT) or SHELL_VER not in _read(OUT):
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(SHELL)
        os.replace(tmp, OUT)
    return OUT


def _read(p: str) -> str:
    try:
        with open(p, encoding="utf-8") as f:
            return f.read(400)
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop", type=int, default=0, help="每 N 秒重生成一次（0=只跑一次）")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    ap.add_argument("--stale-after", type=int, default=900)
    ap.add_argument("--threshold", type=int, default=8)
    ap.add_argument("--min-vol", type=int, default=12)
    ap.add_argument("--flow", type=int, default=30)
    args = ap.parse_args()

    path = write_once(args)
    if args.open:
        subprocess.run(["open", path], capture_output=True)
    if not args.loop:
        print(path)
        return 0
    print(f"{path}\n每 {args.loop}s 刷新一次，Ctrl-C 停")
    # 按墙钟判周期，不用长 sleep —— macOS 睡眠会把 sleep 冻住
    last = time.time()
    while True:
        time.sleep(1)
        if time.time() - last >= args.loop:
            last = time.time()
            try:
                write_once(args)
            except Exception as e:                    # noqa: BLE001
                print(f"[board_html] 生成失败（继续）：{type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
