#!/usr/bin/env python3
"""workOS 控制台 —— 一屏看清「这套系统本身现在什么状况」。

跟 board 的分工别混：
  board_html.py   面向**消息**：我该处理什么事、谁在等我、哪个群热起来了
  console.py      面向**系统**：会话活着没、角色对不对、服务装没装、配置跟现实一不一致

出问题时先看这里：它把「声明」和「现实」摆在一起，不一致的地方直接标出来。
在此之前定位一个问题要人肉跑四五条命令（tmux list-panes / launchctl list /
fleet_up check / tail events.ndjson），还得自己在脑子里对账。

用法：
    python3 console.py                 # 生成一次
    python3 console.py --open          # 生成并用浏览器打开
    python3 console.py --loop 10       # 每 10 秒重生成（页面自己会重载）

输出：data/console/index.html —— 静态文件，浏览器开一个标签页常驻即可，不需要起服务。
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fleet_up  # noqa: E402  单一真相：状态判定逻辑不在这里复制一份

try:
    import fleet  # noqa: E402  只用它的 capture/解析工具
except Exception:
    fleet = None

OUT_DIR = os.path.join(BASE, "data", "console")
OUT = os.path.join(OUT_DIR, "index.html")
EVENTS = os.path.join(BASE, "data", "events.ndjson")
STATUS_JSON = os.path.expanduser("~/.claude/tmux-claude-status.json")


def esc(t) -> str:
    return html.escape(str(t if t is not None else ""))


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 采数

def declared_roles() -> dict[str, str]:
    """从 fleet 配置里读出「哪个 session:window 的第几个 pane 该是什么角色」。

    键用 `session:window#index`。用不上 pane-id——配置是声明，pane-id 是运行期产物，
    两者对不上正是这个页面要显示的东西。
    """
    out: dict[str, str] = {}
    try:
        cfg = fleet_up.load_cfg()
    except SystemExit:
        return out
    for s in cfg.get("sessions") or []:
        for w in s.get("windows") or []:
            for i, p in enumerate(w.get("panes") or [], start=1):
                if p.get("role"):
                    out[f"{s['name']}:{w.get('name')}#{i}"] = p["role"]
    return out


def claude_status() -> dict[str, dict]:
    try:
        with open(STATUS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def collect_panes(with_ctx: bool) -> list[dict]:
    """列出所有 Claude pane 及其状态。

    `with_ctx` 会对每个 pane 跑一次 tmux capture 解析上下文占用——几十个 pane 时
    这是整页最慢的一步，所以给了开关。
    """
    fmt = ("#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}"
           "\t#{pane_id}\t#{pane_current_command}\t#{pane_current_path}")
    out = fleet_up.tmux("list-panes", "-a", "-F", fmt, check=False)
    known = claude_status()
    roles = declared_roles()
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        sname, widx, wname, pidx, pid, pcmd, pcwd = parts[:7]
        is_claude = pid in known or bool(fleet_up.CLAUDE_CMD_RE.match(pcmd))
        if not is_claude:
            continue
        clean_w = fleet_up.clean_window_name(wname)
        rec = known.get(pid, {})
        ctx = None
        if with_ctx and fleet is not None:
            try:
                ctx = fleet.ctx_usage(pid)
            except Exception:
                ctx = None
        rows.append({
            "coord": f"{sname}:{clean_w}.{pidx}",
            "pane": pid,
            "session": sname,
            "window": clean_w,
            "role": roles.get(f"{sname}:{clean_w}#{pidx}"),
            "status": rec.get("status") or "?",
            "ctx": ctx,
            "cwd": pcwd.replace(os.path.expanduser("~"), "~"),
            "seen": rec.get("updated_at"),
        })
    rows.sort(key=lambda r: (r["session"], r["window"], r["coord"]))
    return rows


def collect_services() -> list[dict]:
    out = []
    for s in fleet_up.load_svc():
        st, note = fleet_up.svc_status(s)
        when = ("每 %ss" % s["interval"] if s.get("interval")
                else "常驻" if s.get("keepalive")
                else fleet_up.fmt_calendar(s.get("calendar")) if s.get("calendar")
                else "手动")
        out.append({"name": s.get("name", "?"), "state": st,
                    "when": when, "note": note})
    return out


def collect_topology() -> list[dict]:
    """声明的 session 与实际活着的 session 对账。"""
    try:
        cfg = fleet_up.load_cfg()
    except SystemExit:
        return []
    want = {s["name"]: s for s in cfg.get("sessions") or []}
    live = set(fleet_up.live_sessions())
    rows = []
    for name in sorted(set(want) | live):
        if name in want and name in live:
            st, note = fleet_up.session_diff(cfg, name)
            rows.append({"name": name, "state": st, "note": note})
        elif name in want:
            rows.append({"name": name, "state": "缺",
                         "note": f"配置里有但没跑 → fleet_up.py up --session {name}"})
        else:
            rows.append({"name": name, "state": "野生",
                         "note": "机器上有、配置里没有（项目会话通常就该是这样）"})
    return rows


def collect_memory() -> list[dict]:
    """个人工作记忆有没有备份。

    放在控制台里的理由：拓扑、服务、角色出问题都能从版本库重建，唯独记忆不能。
    它最该被盯着，也最容易因为「一直没事」而被忘掉。
    """
    out = []
    for m in fleet_up.load_mem():
        i = fleet_up.mem_info(m)
        out.append({
            "name": i["name"], "state": i["state"], "what": i.get("what", ""),
            "where": fleet_up.shrink(i["path"]) if i["path"] else "（没找到）",
            "note": i["note"],
        })
    return out


def collect_events(n: int) -> list[dict]:
    """events.ndjson 末尾 n 条，倒序。

    只读文件尾部：这个文件一直在追加，读全量会越来越慢。
    """
    if not os.path.isfile(EVENTS):
        return []
    size = os.path.getsize(EVENTS)
    span = min(size, 256 * 1024)
    with open(EVENTS, "rb") as f:
        f.seek(size - span)
        raw = f.read().decode("utf-8", "replace")
    lines = raw.splitlines()
    if span < size and lines:
        lines = lines[1:]          # 第一行多半被截断了
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()
    return out


def collect_checks() -> list[dict]:
    """doctor 那套自检，做成结构化的。"""
    import shutil
    rows = []

    def add(ok, label, note=""):
        rows.append({"ok": bool(ok), "label": label, "note": note})

    for b in ("tmux", "claude", "dws"):
        add(shutil.which(b), f"依赖 {b}")
    add(os.path.isfile(fleet_up.LOCAL_CFG), "_local-fleet.yaml", "缺就跑 capture 或复制模板")
    add(os.path.isfile(os.path.join(BASE, "config.json")), "config.json")
    add(os.path.isfile(fleet_up.LOCAL_SVC), "_local-services.yaml")
    try:
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
            add("dtcc.py" in f.read(), "Claude hooks（心跳上报）")
    except OSError:
        add(False, "Claude hooks（心跳上报）", "读不到 ~/.claude/settings.json")

    missing = []
    for coord, role in declared_roles().items():
        if not fleet_up.role_path(role):
            missing.append(role)
    add(not missing, "角色文件齐全",
        ("缺：" + "、".join(sorted(set(missing)))) if missing else "")
    return rows


def collect_collector() -> list[dict]:
    """采集器的运行痕迹：最近一次 poll 是什么时候、队列有多深。"""
    rows = []
    st = os.path.join(BASE, "data", "state.json")
    if os.path.isfile(st):
        age = time.time() - os.path.getmtime(st)
        rows.append({
            "k": "上次采集",
            "v": f"{int(age // 60)} 分 {int(age % 60)} 秒前",
            "warn": age > 900,     # poll 默认 300s，超过 15 分钟就该起疑
        })
    q = os.path.join(BASE, "data", "desk_queue.ndjson")
    if os.path.isfile(q):
        with open(q, encoding="utf-8") as f:
            n = sum(1 for line in f if line.strip())
        rows.append({"k": "desk 待送队列", "v": f"{n} 条", "warn": n > 0})
    inbox = os.path.join(BASE, "data", "inbox.ndjson")
    if os.path.isfile(inbox):
        rows.append({"k": "inbox 大小",
                     "v": f"{os.path.getsize(inbox) / 1048576:.1f} MB", "warn": False})
    return rows


# ---------------------------------------------------------------- 渲染

TONE = {
    "OK": "ok", "托管": "ok", "常驻": "ok", "不备份": "mute",
    "差异": "warn", "漂移": "warn", "缺": "warn", "未装": "warn",
    "只在本机": "bad", "未纳入版本控制": "bad", "没找到": "bad",
    "野生": "mute", "不一致": "bad", "配置错": "bad", "读不了": "bad",
}


def pill(text: str) -> str:
    return f'<span class="pill {TONE.get(text, "mute")}">{esc(text)}</span>'


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="empty">（无）</p>'
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def section(title: str, note: str, body: str) -> str:
    n = f'<span class="note">{esc(note)}</span>' if note else ""
    return f'<section><h2>{esc(title)}{n}</h2>{body}</section>'


def render(args) -> str:
    panes = collect_panes(with_ctx=not args.fast)
    svcs = collect_services()
    topo = collect_topology()
    checks = collect_checks()
    coll = collect_collector()
    mems = collect_memory()
    events = collect_events(args.events)

    bad_checks = [c for c in checks if not c["ok"]]
    bad_svcs = [s for s in svcs if s["state"] not in ("OK", "托管")]
    bad_topo = [t for t in topo if t["state"] in ("缺", "差异")]
    warn_coll = [c for c in coll if c.get("warn")]
    bad_mem = [m for m in mems if m["state"] not in ("OK", "不备份")]
    trouble = (len(bad_checks) + len(bad_svcs) + len(bad_topo)
               + len(warn_coll) + len(bad_mem))

    head = (f'<span class="{"bad" if trouble else "ok"}">'
            f'{"有 %d 处要看" % trouble if trouble else "一切正常"}</span>'
            f'　·　{len(panes)} 个 Claude　·　{len(svcs)} 个服务　·　'
            f'{len([t for t in topo if t["state"] == "OK"])} 个会话按声明跑着')

    # ── 会话 ──
    prows = []
    for p in panes:
        role = p["role"] or '<span class="mute">—</span>'
        seen = ""
        if p["seen"]:
            age = time.time() - float(p["seen"])
            seen = f"{int(age // 60)} 分前" if age >= 60 else "刚刚"
            if age > 3600:
                seen = f'<span class="mute">{int(age // 3600)} 小时前</span>'
        ctx = p["ctx"] or '<span class="mute">—</span>'
        prows.append([f'<code>{esc(p["coord"])}</code>', f'<code>{esc(p["pane"])}</code>',
                      role, esc(p["status"]), ctx, seen,
                      f'<span class="mute">{esc(p["cwd"])}</span>'])

    # ── 服务 ──
    srows = [[pill(s["state"]), esc(s["name"]), esc(s["when"]),
              f'<span class="mute">{esc(s["note"])}</span>'] for s in svcs]

    # ── 拓扑 ──
    trows = [[pill(t["state"]), esc(t["name"]),
              f'<span class="mute">{esc(t["note"])}</span>'] for t in topo]

    # ── 自检 ──
    crows = [[('<span class="ok">✓</span>' if c["ok"] else '<span class="bad">✗</span>'),
              esc(c["label"]), f'<span class="mute">{esc(c["note"])}</span>']
             for c in checks]

    # ── 采集器 ──
    grows = [[esc(c["k"]),
              f'<span class="{"bad" if c.get("warn") else ""}">{esc(c["v"])}</span>']
             for c in coll]

    # ── 事件 ──
    erows = [[f'<span class="mute">{esc(e.get("when"))}</span>',
              esc(e.get("who")), esc(e.get("kind")), esc(e.get("project")),
              esc(e.get("what"))[:300]] for e in events]

    body = "".join([
        section("会话与角色", "声明的角色对不上就说明这个 pane 不是 fleet_up 起的",
                table(["坐标", "pane", "角色", "状态", "上下文", "心跳", "cwd"], prows)),
        section("服务", "「漂移」= 装了，但 plist 内容跟声明对不上",
                table(["", "名字", "触发", "说明"], srows)),
        section("拓扑对账", "「野生」对项目会话是正常的",
                table(["", "session", "说明"], trows)),
        section("个人工作记忆", "别的都能从版本库重建，这个不能",
                table(["", "名字", "内容", "位置", "说明"],
                      [[pill(m["state"]), esc(m["name"]),
                        f'<span class="mute">{esc(m["what"])}</span>',
                        f'<code>{esc(m["where"])}</code>',
                        f'<span class="mute">{esc(m["note"])}</span>'] for m in mems])),
        section("自检", "", table(["", "项", "说明"], crows)),
        section("采集器", "", table(["", ""], grows)),
        section("事件流", f"events.ndjson 最近 {len(events)} 条，会话之间的共享记忆",
                table(["时间", "谁", "类型", "项目", "内容"], erows)),
    ])

    return SHELL.replace("{{HEAD}}", head).replace("{{BODY}}", body) \
                .replace("{{TIME}}", now_str()).replace("{{RELOAD}}", str(args.loop or 0))


SHELL = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>workOS 控制台</title>
<style>
:root{
  --bg:#fbfaf8; --fg:#1c1a17; --mute:#8b857c; --line:#e5e0d8; --card:#fff;
  --ok:#2f7a4d; --warn:#a8760d; --bad:#b3341f; --accent:#3a5f8a;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#14161a; --fg:#dfe3e8; --mute:#7c8590; --line:#262a30; --card:#181b20;
        --ok:#5fb87f; --warn:#d6a23c; --bad:#e0705a; --accent:#7aa5d2;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;padding:20px}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;
  padding-bottom:12px;border-bottom:1px solid var(--line);margin-bottom:18px}
h1{font-size:15px;margin:0;letter-spacing:.04em}
.stamp{margin-left:auto;color:var(--mute);font-size:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:6px;
  padding:12px 14px;margin-bottom:14px}
h2{font-size:12px;margin:0 0 9px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent);font-weight:600}
.note{text-transform:none;letter-spacing:0;color:var(--mute);font-weight:400;
  margin-left:10px;font-size:11px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th{text-align:left;color:var(--mute);font-weight:500;padding:3px 12px 5px 0;
  border-bottom:1px solid var(--line);font-size:11px}
td{padding:3px 12px 3px 0;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
td:last-child,th:last-child{white-space:normal}
code{font:inherit;color:var(--accent)}
.mute{color:var(--mute)} .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.pill{display:inline-block;padding:0 7px;border-radius:9px;font-size:11px;
  border:1px solid currentColor;opacity:.9}
.empty{color:var(--mute);margin:2px 0}
</style></head><body>
<header>
  <h1>workOS 控制台</h1>
  <div>{{HEAD}}</div>
  <div class="stamp">{{TIME}}</div>
</header>
{{BODY}}
<script>
// 重载前记住滚动位置，否则常驻页面每次刷新都跳回顶部，根本没法盯着某一行看
const KEY='workos-console-scroll';
addEventListener('DOMContentLoaded',()=>{const y=sessionStorage.getItem(KEY);if(y)scrollTo(0,+y)});
addEventListener('beforeunload',()=>sessionStorage.setItem(KEY,scrollY));
const n={{RELOAD}};
if(n>0)setTimeout(()=>location.reload(),n*1000);
</script>
</body></html>
"""


# ---------------------------------------------------------------- main

def write_once(args) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render(args))
    os.replace(tmp, OUT)     # 原子替换，浏览器不会读到写了一半的文件
    return OUT


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop", type=int, default=0, help="每 N 秒重生成一次（0=只跑一次）")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    ap.add_argument("--events", type=int, default=40, help="事件流显示多少条")
    ap.add_argument("--fast", action="store_true",
                    help="跳过逐 pane 抓上下文占用（几十个 pane 时明显更快）")
    args = ap.parse_args()

    path = write_once(args)
    print(path)
    if args.open:
        subprocess.run(["open", path])
    if args.loop <= 0:
        return 0

    print(f"每 {args.loop} 秒重生成，Ctrl-C 退出")
    try:
        while True:
            time.sleep(args.loop)
            write_once(args)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
