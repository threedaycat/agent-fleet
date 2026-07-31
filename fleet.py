#!/usr/bin/env python3
"""fleet —— 让多个 Claude Code 会话彼此看得见、能被叫醒。

dtwatch 解决的是「钉钉的消息怎么进来、派给谁」，这个文件解决的是另一半：

  - **心跳**：每个会话收尾时报一句「我是谁、在哪个 tmux、在干什么、闲没闲」。
    不报心跳的话，「谁在忙」这个问题永远只能靠人去每个 pane 里看一眼。
  - **唤醒**：Claude Code 没有「往正在运行的会话推一条消息」的接口。
    现有的投递只发生在目标会话**一轮活干完**的时候（Stop hook 拉一次），
    所以完全空闲的会话会一直排着不动。要主动叫醒它，只有 tmux send-keys 这条路。
  - **事件日志**：只追加，一行一条。谁、什么时候、干了什么、产出在哪。
    这是多会话之间「记忆互通」的唯一真相 —— 各会话自己按 project 过滤、
    自己压缩成自己的视图，而不是所有人读写同一份状态文件（那样必然互相污染）。

设计上刻意不引入服务、队列、数据库：规模上限是几十个会话，文件 + 原子替换就够。

用法：
    fleet.py beat [--state idle|busy|blocked] [--note "..."] [--project X]
    fleet.py list [--stale-after 900]
    fleet.py wake <sid|项目名> --task "..." [--dry-run]
    fleet.py log [--tail 30] [--project X]
    fleet.py event --what "..." [--where "..."] [--project X]
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
# 会话实况的**真相**：claude-tmux-sessions 那套通用工具维护的状态文件。
# 20 个 pane 全在里面（pane/session/window/cwd/status/session_id/updated_at）。
# fleet 不再自己维护一份 —— 两套会话追踪器必然打架，谁对谁错说不清。
STATUS_FILE = os.path.expanduser("~/.claude/tmux-claude-status.json")

# fleet.json 退化成**薄 sidecar**：只补上面那个文件没有的三样东西。
# 键是 session_id，值只有 {project, note, last_wake}。不再存 state/tmux/cwd/heartbeat。
FLEET = os.path.join(DATA, "fleet.json")
EVENTS = os.path.join(DATA, "events.ndjson")      # 只追加的事件日志

# 任务台账。补的是「idle 会话不会自动去拉自己名下的积压」这个洞 ——
# board 上出现过「某项目 19 条未处理、会话却还静默」，因为投递只在那个会话
# 自己收尾时才触发，它闲着就没人推。台账让每件活有名有主、能看出谁没交。
# 一条 = 一件活的一次状态变更，**只追加**；同一个 id 取最后一条为准，
# 这样几个会话同时写也不会互相覆盖（跟 events.ndjson 一个哲学）。
TASKS = os.path.join(DATA, "tasks.ndjson")
CONFIG_PATH = os.path.join(BASE, "config.json")

TS_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> dt.datetime:
    return dt.datetime.now()


def ts(d: dt.datetime) -> str:
    return d.strftime(TS_FMT)


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, TS_FMT)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def append_event(rec: dict):
    """事件日志只追加。

    并发安全靠「只 append 一行」这个语义本身 —— 单行 write 在
    O_APPEND 下是原子的，所以几十个会话同时写也不需要锁。
    """
    os.makedirs(DATA, exist_ok=True)
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def env_sid() -> str:
    """本会话 id。Claude Code 把它放在 CLAUDE_CODE_SESSION_ID 里，
    hook 那条路走 payload 的 session_id，两边都能落到同一个值。"""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID") or "").strip()


def tmux_send(pane: str, text: str) -> bool:
    """往指定 pane 发一段文字并回车。**唯一允许发 send-keys 的地方。**

    三条防护，每条都是踩过的：
      1. **pane 为空一律拒发**。`tmux send-keys` 不带 `-t` 会打到「当前活动 pane」——
         2026-07-30 自测时就因为取 pane-id 的命令写错、target 变成空字符串，
         把一条命令打进了别人正在看的 shell 里并执行了。宁可不发，不能乱发。
      2. **只接受 pane-id（`%NN`）**，不接受 `session:window` 名 —— 会话改名后
         按名字发会静默打空（`journal` 被改成 `OS` 那次就是）。
      3. **文字和回车之间停 0.4 秒**。`-l` 把文字塞进输入框是异步的，
         紧跟着发 Enter 会跟文字挤在一起，任务停在输入框里没提交，文本越长越容易中。
    """
    if not pane or not pane.startswith("%"):
        return False
    if not pane_alive(pane):
        return False
    code, _ = sh(["tmux", "send-keys", "-t", pane, "-l", text])
    if code != 0:
        return False
    time.sleep(0.4)
    code, _ = sh(["tmux", "send-keys", "-t", pane, "Enter"])
    return code == 0


def pending_input(pane: str) -> str:
    """输入框里有没有已经打进去、但还没敲回车提交的文字。

    背景：`/compact` 送进输入框是**下一轮开头才真正执行**的——中间只要再
    wake 一次，新任务会占掉那一轮，压缩指令就被冲掉了，会话继续涨，谁都
    不知道（2026-07-31 晚上真实发生两次：077601b8 送了 /compact 又被派活，
    401k 才被自己发现；297b11d9 585k 时 /compact 发出但数字没降）。
    靠派活的人记得先看一眼不可靠，所以在 wake 里机械挡一道。

    Claude Code 的输入框是个方框，提示符是 "❯ "；正常时提示符后面啥都
    没有。方框上边框是带标题的 "──── 项目名 ──"，下边框是纯 "─" 一行。
    有文字排着没提交时,提示符和下边框之间会有内容——扫到下边框为止,
    别只看提示符那一行,免得长文本换行漏看。
    """
    if not pane or not pane.startswith("%"):
        return ""
    code, out = sh(["tmux", "capture-pane", "-t", pane, "-p", "-S", "-20"])
    if code != 0:
        return ""
    lines = out.splitlines()
    prompt_i = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("❯"):
            prompt_i = i          # 取最后一次出现，避免历史输出里凑巧带这个符号
    if prompt_i is None:
        return ""
    buf = []
    first = lines[prompt_i].strip()[1:].strip()
    if first:
        buf.append(first)
    for ln in lines[prompt_i + 1:]:
        s = ln.strip()
        if not s:
            continue
        if re.fullmatch(r"─+", s):        # 下边框：到此为止
            break
        buf.append(s)
    return " ".join(buf).strip()


def capture_tail(pane: str, n: int = 6) -> str:
    """只拿 pane 最后 n 行——不管当前可见区多高、滚动到哪，永远是"此刻
    最新"这几行。

    2026-08-01 5380 实测：`ctx_usage` 原来带 `-S -15` 往上翻 scrollback，
    抓到过旧页脚（报 89k 实际 336k，报 658k 实际 126k）——拿这个字段
    判断该不该催压缩，字段错了调度就是错的，是三个问题里最要紧那条。

    **`-S` 的行号是相对可见区顶部数的，不是相对底部**——一开始想用
    `-S -{n} -E -` 精确取尾部，结果 pane 矮的时候看着像只拿了尾部
    （凑巧），pane 高的时候照样把整个可见区（实测一个 46 行高的 pane）
    都带回来，验证时当场露馅。改成"整块可见区先原样拿回来，Python 这边
    自己切最后 n 行"，就不受 pane 高度影响了。
    """
    if not pane or not pane.startswith("%"):
        return ""
    code, out = sh(["tmux", "capture-pane", "-t", pane, "-p"])
    if code != 0:
        return ""
    return "\n".join(out.splitlines()[-n:])


def awaiting_choice(pane: str) -> bool:
    """对面是不是正停在一个交互选择框上（AskUserQuestion 那种），不是在等文字输入。

    2026-08-01 5380 指出：这跟 `pending_input` 防的不是一回事——那个防的是
    "输入框有字没提交"，这个防的是"根本不在输入框，Claude 正等你在选择框
    里按键"。硬发文字进去会被当成对选择框的按键响应，可能替他确认了一件
    本该他自己点头的事（发消息确认、权限确认、危险命令确认——机主的硬
    规矩正是这些必须他本人点头）。这不是体验问题，是会踩红线的坑。

    检测特征直接抄 `desk_push.sh` 的 `desk_busy()`——那套在生产里跑了很久，
    同一套系统里 desk 那条注入路径早就防了这个，`fleet.py wake` 没防，
    没防的这条还是天天在用的那条，没有理由自己重新发明一遍。
    """
    if not pane or not pane.startswith("%"):
        return False
    code, out = sh(["tmux", "capture-pane", "-t", pane, "-p"])
    if code != 0:
        return False
    return bool(re.search(r"Enter to select|↑/↓ to navigate|Esc to cancel", out))


def ctx_usage(pane: str):
    """从页脚那行解析上下文占用，形如 "529k (53%)"。

    页脚格式参考：`[Opus 5 (1M context)] 项目名  ▓▓▓░░ 53% (529k)  ⚠⚠ /compact now`。
    解析不到就返回 None——**不猜、不报错**，页脚格式以后要是变了，这里
    应该老实说"不知道"，不该给一个可能是错的数字出去。

    只看 `capture_tail`（最后几行），不带 `-S` 往上翻 scrollback——见
    `capture_tail` 文档里 2026-08-01 那次"读到滚动区旧页脚"的实测
    （报 89k 实际 336k，报 658k 实际 126k）。就算尾部里凑巧出现不止
    一个百分比，取最后一个匹配——那个才是离当前最近的。
    """
    out = capture_tail(pane)
    if not out:
        return None
    matches = re.findall(r"(\d+)%\s*\((\d+k)\)", out)
    if not matches:
        return None
    pct, kk = matches[-1]
    return f"{kk} ({pct}%)"


def sh(args: list[str], timeout=5) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


# ---------------------------------------------------------------- tmux

def live_panes() -> set:
    """tmux 里**现在真实存在**的 pane-id 集合。一次拿全，不逐个查（有 68 个）。"""
    code, out = sh(["tmux", "list-panes", "-a", "-F", "#{pane_id}"])
    return set(out.split()) if code == 0 else set()


def sessions(include_remembered: bool = True) -> dict:
    """session_id -> 会话实况。

    **分三层，因为单看哪一层都会判错**（2026-07-31 12:01 踩过）：

    1. `~/.claude/tmux-claude-status.json` —— Claude 的**状态**（running/input/done）
       和 session_id ↔ pane 的映射。但它**不是全量快照**：那套工具会重建它，
       重建后只剩「最近有活动的 pane」。12:01 它从 23 条掉到 2 条，
       于是所有路由目标一夜之间全被判成「已关闭」—— 那是误报。
    2. `tmux list-panes -a` —— pane **是否真的存在**。这才是存活的硬真相。
    3. sidecar 的 `last_seen` —— 我自己记的「上次见到这个 session 在哪个 pane」。
       状态文件缩水时靠它把会话认回来，再用第 2 层验证那个 pane 还在不在。

    所以一个会话有三种状态：**实时**（1 有）、**在但状态未知**（1 没有、2 还在）、
    **真的关了**（1 没有、2 也没有）。
    """
    raw = load_json(STATUS_FILE, {})
    side = load_json(FLEET, {})
    panes = live_panes()
    out = {}
    seen_now = set()
    for pane, r in (raw.items() if isinstance(raw, dict) else []):
        sid = (r.get("session_id") or "").strip()
        if not sid:
            continue
        st = r.get("status") or ""
        sc = side.get(sid) or {}
        cwd = r.get("cwd") or ""
        out[sid] = {
            "sid": sid,
            "pane": pane,
            "tmux": f"{r.get('session','')}:{r.get('window','')}.{r.get('pane_index','')}",
            "window_name": (r.get("window_name") or "").strip(),
            "cwd": cwd,
            # input=在等输入（闲）、running=在跑（不能打断）、done=刚干完
            "state": {"input": "idle", "done": "idle", "running": "busy",
                      "blocked": "blocked"}.get(st, st or "?"),
            "raw_status": st,
            "age": int(now().timestamp() - (r.get("updated_at") or 0)),
            # 优先级必须跟 beat() 一致：**config 映射 > sidecar 存的旧值**。
            # 反过来会出事：会话换了 cwd（比如从项目目录切回工作区根），
            # sidecar 里还留着旧的项目短名，于是 wake 那个项目时匹配到两个候选，
            # 按状态排序猜一个 —— 真这么把任务打进过错误的 pane。
            "project": project_of(cwd) or sc.get("project", ""),
            "note": sc.get("note", ""),
            "last_wake": sc.get("last_wake", ""),
            "known": "live",              # 状态文件里有，实时
        }
        seen_now.add(sid)

    # 记住「见过」，供状态文件缩水时把会话认回来
    dirty = False
    for sid in seen_now:
        r = out[sid]
        prev = side.get(sid) or {}
        if prev.get("last_seen", {}).get("pane") != r["pane"]:
            prev["last_seen"] = {"pane": r["pane"], "tmux": r["tmux"],
                                 "window_name": r["window_name"],
                                 "cwd": r["cwd"], "at": ts(now())}
            side[sid] = prev
            dirty = True
    if dirty:
        save_json(FLEET, side)

    if not include_remembered:
        return out

    # 第三层：sidecar 记的 last_seen —— 状态文件缩水时靠它把会话认回来，
    # 再用 tmux 那份真相（panes）验证那个 pane 还在不在。
    # **这段曾经因为下面第四层末尾提前 return 而变成死代码，从没生效过**
    # （2026-07-31 发现，见 fix commit）：受影响的会话统统被第四层接管，
    # 第四层不给 pane、也分不出「pane 还在」和「pane 真关了」，
    # 于是它们要么在 `fleet.py list` 上被标成看着能唤醒的「idle」
    # （实际那个 pane 早就没了，`wake` 会莫名其妙地报"pane 不在了"），
    # 要么该显示"关了"却显示成"闲着"，糊弄看板的人。
    # 顺序很重要：这层必须跑在第四层前面，抢到的 sid 第四层会跳过。
    for sid, sc in side.items():
        if sid in out:
            continue
        ls = sc.get("last_seen") or {}
        pane = ls.get("pane", "")
        if not pane:
            continue
        alive = pane in panes
        out[sid] = {
            "sid": sid, "pane": pane, "tmux": ls.get("tmux", ""),
            "window_name": ls.get("window_name", ""), "cwd": ls.get("cwd", ""),
            "state": "unknown" if alive else "closed",
            "raw_status": "", "age": 10 ** 6,
            "project": project_of(ls.get("cwd", "")) or sc.get("project", ""),
            "note": sc.get("note", ""), "last_wake": sc.get("last_wake", ""),
            "known": "remembered" if alive else "gone",
            "last_seen_at": ls.get("at", ""),
        }

    # 第四层：transcript 的 mtime。状态文件缩水、又没有 last_seen 时，
    # 「这个会话的 transcript 刚刚还在写」就是它活着的铁证 ——
    # 有个会话被报成「查不到」，实际它 94 秒前还在写盘。
    # 这一层只用来判**活没活、多久没动**，不给 pane：
    # 同一个 cwd 可能开着好几个 pane，猜 pane 会把活派错地方（上面那次教训）。
    nowt = now().timestamp()
    for sid, sc in side.items():
        if sid in out:
            continue
        tp = transcript_of(sid)
        if not tp:
            continue
        try:
            age = int(nowt - os.path.getmtime(tp))
        except OSError:
            continue
        cwd = sc.get("cwd") or os.path.basename(os.path.dirname(tp)).replace("-", "/")
        out[sid] = {
            "sid": sid, "pane": "", "tmux": "",
            "window_name": "", "cwd": sc.get("cwd", ""),
            "state": "idle" if age < 3600 else "stale",
            "raw_status": "", "age": age,
            "project": sc.get("project") or "",
            "note": sc.get("note", ""), "last_wake": sc.get("last_wake", ""),
            "known": "transcript",
        }
    return out


def clean_name(t: str) -> str:
    """去掉 tmux 窗口名里的活动指示符（✳ ⠂ ⠐ 这些是 Claude Code 加的动画字符）。"""
    return str(t).lstrip("✳⠂⠐⠄⠆⠇⠋⠙⠸⠰⠠★●○◐◓ \t").strip()


def disp_of(rec: dict, short: bool = False) -> str:
    """给**人**看的会话名。原则：不露 session_id 碎片（"我是人类，人类不看 id"）。

    形如 `dtwatch · OS:3`：前半是 tmux 窗口名（认得出是哪个活），
    后半是坐标（知道去哪个窗口看）。窗口名跟项目短名重复时不重复显示。
    """
    win = clean_name(rec.get("window_name", ""))
    proj = (rec.get("project") or "").strip()
    coord = rec.get("tmux", "")
    if coord.endswith(".1"):
        coord = coord[:-2]                 # 单 pane 的窗口不必显示 .1，省一列
    # 项目短名放前面 —— 那是人最认得的（项目A / 项目B / dtwatch）。
    # 窗口名只在它「短、且跟项目名不重复」时才补上：实测有些窗口名是
    # 「查看 dws 并分配任务」这种任务描述，拼进来只会把列撑爆、反而更难认。
    name = proj or win
    # short=True 给窄列用（路由表、队列）：只要 `项目 坐标`，不补窗口名。
    # 宽列（心跳）才补，因为同一个项目可能开着好几个窗口，那时窗口名是唯一区分。
    if not short and proj and win and proj.lower() not in win.lower() and dw(win) <= 12:
        name = f"{proj}·{win}"
    return f"{name} {coord}".strip() if coord else name


def label_of(rec: dict) -> str:
    """兼容旧调用（dtcc 用过），等同 disp_of。"""
    return disp_of(rec)


def tmux_target() -> str:
    """本会话所在的 tmux 目标（session:window.pane）。

    $TMUX_PANE 是 tmux 自己塞进环境的 pane id（形如 %7），
    hook 进程能继承到。拿它反查完整坐标，这样即便窗口被移动过，
    唤醒时用的还是当前真实位置。
    """
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return ""
    code, out = sh(["tmux", "display-message", "-p", "-t", pane,
                    "#{session_name}:#{window_index}.#{pane_index}"])
    return out if code == 0 else ""


def pane_alive(target: str) -> bool:
    """target 传 **pane-id（%NN）** 最稳。

    别用 `session:window` 那种名字型 target —— tmux 会话随时会被改名
    （2026-07-30 `journal` 就被改成了 `OS`），名字一变，按名字发的 send-keys
    直接打空，而且不报错。pane-id 改名、挪窗口、换 index 都不变。
    """
    if not target:
        return False
    code, out = sh(["tmux", "display-message", "-p", "-t", target, "#{pane_id}"])
    return code == 0 and out.startswith("%")


def pane_tail(target: str, lines=4) -> str:
    code, out = sh(["tmux", "capture-pane", "-p", "-t", target])
    if code != 0:
        return ""
    return "\n".join(out.splitlines()[-lines:])





# ---------------------------------------------------------------- 收尾话的全文

# 会话之间互相打招呼用的标记，形如 `@7ac5`（对方会话号）或 `@main`（项目短名）。
# 这东西是**中断线**：主会话靠它知道"这句话在叫我"。所以哪怕收尾话很长、
# 标记落在第 300 字，也必须进事件 —— 截断把它切掉，中断线就静默失灵了。
MARK_RE = re.compile(r"@(?:[0-9a-f]{4,8}|[A-Za-z][\w-]{1,14})")


def tail_text(path: str, kb: int = 512) -> list[str]:
    """只读文件末尾若干 KB 的完整行。transcript 能有几十 MB，不能整读。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > kb * 1024:
                f.seek(size - kb * 1024)
                f.readline()                      # 丢掉可能被切断的半行
            raw = f.read().decode("utf-8", errors="replace")
        return raw.splitlines()
    except OSError:
        return []


def transcript_of(sid: str) -> str:
    """按 session_id 找 transcript 文件。"""
    import glob
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else ""


def last_assistant_text(sid: str, limit: int = 400) -> str:
    """这个会话最后说的那段话，**自己去 transcript 里取**。

    为什么 fleet 要自己读：Stop hook 那侧传进来的 note 是硬截到 100 字的
    （实测 194 条事件里 112 条正好被切在 100，58%）。标记落在 100 字之后就丢了。
    这里不依赖调用方给多少，自己取全文再按下面的规则收。
    """
    path = transcript_of(sid)
    if not path:
        return ""
    for ln in reversed(tail_text(path)):
        ln = ln.strip()
        if not ln or '"assistant"' not in ln:
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "assistant":
            continue
        parts = [c.get("text", "") for c in
                 ((o.get("message") or {}).get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        txt = " ".join(t for t in parts if t).strip().replace("\n", " ")
        if txt:
            return keep_marks(txt, limit)
    return ""


def keep_marks(text: str, limit: int) -> str:
    """截断，但**先把全文里的标记捞出来钉在开头** —— 标记比正文重要。

    两层保险：上限从 100 提到 400（够放完整一段结论），
    再加上「标记前置」——哪怕正文比 400 还长，`@7ac5` 也一定在。
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    marks = []
    for m in MARK_RE.findall(text):
        tag = "@" + m if not m.startswith("@") else m
        if tag not in marks:
            marks.append(tag)
    head = ("[标记 " + " ".join(marks[:4]) + "] ") if marks else ""
    return head + text[:max(40, limit - len(head))] + "…"


# ---------------------------------------------------------------- 任务台账

def task_id() -> str:
    return "t" + uuid.uuid4().hex[:6]


def tasks_load() -> dict:
    """读台账，按 id 折叠成当前状态（同 id 取最后一条）。"""
    out = {}
    if not os.path.exists(TASKS):
        return out
    with open(TASKS, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("id"):
                out[r["id"]] = {**out.get(r["id"], {}), **r}
    return out


def task_write(rec: dict):
    os.makedirs(DATA, exist_ok=True)
    with open(TASKS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 回执闭环
#
# 背景：派活的回执和普通噪音过滤共用同一条「要不要上报主会话」的判断通道，
# 靠的是下级会话自觉在收尾文本里打 @5380 标记。2026-07-31 实测：601 条事件
# 只有 94 条带标记，一次真实的「值班查到了实锤但忘了打标记」直接导致主会话
# 收不到答案。可靠性不能建在「记得打标记」这个约定上，所以改成机械判断：
# wake 派活时记下「等哪个 sid 的下一次收尾」，那次收尾时不管会话自己写了什么，
# 无条件加 @5380 强制放行，回传一次就销账，后续收尾照旧走原来的过滤。

# 强制转发的开关/阈值挂载点。机主原话「如果回传的太多，我们再想办法加一层过滤」——
# 现在按他的要求先不做过滤，保持 None 就是「pending_receipt 命中就转发」。
# 以后要限流/按类型挑着转，就在这里换成 Callable[[dict], bool]，返回 False 就不强制。
RECEIPT_FILTER = None


def pending_receipt(sid: str):
    """这个 sid 名下有没有「5380 派活、等它这次收尾回执」还没销的账。

    同一个会话可能同时压着不止一条 wake（少见但可能），取最早那条——
    先派的先收，别让后面新派的把老的回执顺序打乱。
    """
    tasks = tasks_load()
    hits = [t for t in tasks.values()
            if t.get("target_sid") == sid and t.get("receipt") == "pending"]
    if not hits:
        return None
    hits.sort(key=lambda t: t.get("ts", ""))
    return hits[0]


def mark_receipted(tid: str):
    """回执已经强制转发出去了，销账——不然同一个会话下一次普通收尾又会被强制转发。"""
    task_write({"id": tid, "receipt": "done", "ts": ts(now())})


def cmd_task(args):
    if args.op == "add":
        rec = {"id": task_id(), "text": args.text,
               "target": args.target or "unassigned",
               "status": "pending", "reason": args.reason or "", "ts": ts(now())}
        task_write(rec)
        print(json.dumps({"ok": True, **rec}, ensure_ascii=False))
        return 0

    tasks = tasks_load()
    if args.op in ("dispatch", "done"):
        tid = args.id or ""
        cur = tasks.get(tid)
        if not cur:
            # 允许用 id 前缀，手打全 id 太累
            hits = [t for k, t in tasks.items() if k.startswith(tid)] if tid else []
            if len(hits) == 1:
                cur, tid = hits[0], hits[0]["id"]
            else:
                print(json.dumps({"ok": False,
                                  "error": f"找不到任务：{tid or '(空)'}"
                                           + ("（前缀匹配到多条）" if len(hits) > 1 else ""),
                                  "hint": "fleet.py board 看台账"}, ensure_ascii=False))
                return 1

    if args.op == "dispatch":
        target = args.target or cur.get("target") or "unassigned"
        rec = {"id": tid, "text": cur.get("text", ""), "target": target,
               "status": "dispatched", "reason": args.reason or "", "ts": ts(now())}
        task_write(rec)
        if args.wake and target != "unassigned":
            sid, live = resolve_target(target)
            if live.get("ambiguous"):
                rec["woke"] = "跳过（目标名匹配到多个会话，请用 sid 前缀）"
                sid = ""
            if sid and live.get("state") == "idle":
                if tmux_send(live.get("pane", ""),
                             f"【台账任务 {tid}】{cur.get('text','')}"):
                    mark_wake(sid, cur.get("text", ""))
                    rec["woke"] = disp_of(live)
        print(json.dumps({"ok": True, **rec}, ensure_ascii=False))
        return 0

    if args.op == "done":
        rec = {"id": tid, "text": cur.get("text", ""),
               "target": cur.get("target", ""), "status": "done",
               "reason": args.reason or "", "ts": ts(now())}
        task_write(rec)
        append_event({"who": (env_sid() or "?")[:8], "when": ts(now()),
                      "kind": "task-done", "project": cur.get("target", ""),
                      "what": f"交活 {tid}：{cur.get('text','')[:100]}",
                      "where": args.where or ""})
        print(json.dumps({"ok": True, **rec}, ensure_ascii=False))
        return 0
    return 1



def color_on() -> bool:
    """只在真终端上色。watch 下是 tty，重定向到文件时自动素色。"""
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, kind: str = "") -> str:
    """给一段文字上色。**只用于不参与列对齐的位置** ——
    ANSI 码会让 len()/dw() 算错宽度，对齐就歪了。"""
    if not kind or not color_on():
        return text
    codes = {"warn": "33;1", "bad": "31;1", "ok": "32", "dim": "2"}
    return f"\033[{codes.get(kind, '0')}m{text}\033[0m"


MEDIA_RE = re.compile(r"\[(图片|语音|视频|文件)消息\]\(mediaId=([^)\s]+)\)")


def split_media(text: str) -> tuple:
    """把「[图片消息](mediaId=xxx) 注意：如需下载…」拆成 (人话, [mediaId...])。

    图片**是能读的** —— `dws chat message download-media` 下得下来。
    所以 board 上不该再印一串裸 mediaId（那既占地方又看不懂），
    印「[图片]」加上随图的文字就行，真要看图用 `fleet.py atme` 拿现成命令。
    """
    t = str(text or "")
    ids = [m.group(2) for m in MEDIA_RE.finditer(t)]
    t = MEDIA_RE.sub(lambda m: f"[{m.group(1)}]", t)
    t = re.sub(r"注意：如需下载使用[^\n]*", "", t)
    return re.sub(r"\s+", " ", t).strip(), ids


def strip_ats(text: str) -> str:
    """剥掉消息开头那一串 @某某。board 上已经标了「@你」，正文再带一串 @ 全是噪音。"""
    t = re.sub(r"^(?:\s*@[^\s@]+(?:\([^)]*\))?\s*)+", "", str(text or ""))
    return re.sub(r"\s+", " ", t).strip() or str(text or "").strip()


def at_me_open(limit: int = 6) -> list:
    """@ 他本人、还没处理掉的消息。**这是最该打断他的一类**（硬规矩：
    @他的消息任何会话都无权自判闭环），所以 board 上排在最前面。"""
    triage = load_json(os.path.join(DATA, "triage.json"), {})
    out = []
    try:
        with open(os.path.join(DATA, "inbox.ndjson"), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if "at_me" not in (r.get("flags") or []):
                    continue
                if (triage.get(r["id"]) or {}).get("status") in ("done", "ignored"):
                    continue
                out.append(r)
    except OSError:
        pass
    return out[-limit:] if limit else out



# ---------------------------------------------------------------- 调度流水
#
# 他要的不只是「编队现在什么状态」，还要看懂「**这套东西是怎么调度的**」——
# 谁把什么派给了谁、凭什么派给这个会话、机器自己做了哪些动作。
# 所以下面尽量把**判据**也解出来（quote / default→main / belongs-to），
# 光显示结果他就没法据此改进系统。
#
# 数据源都是现成的日志，不额外埋点：
#   data/cc/cc.log     [push] target= / [route] take|skip / [routed] / [send]
#   data/events.ndjson who=fleet|push-loop 的唤醒、kind=task-done 的交活

CC_LOG = os.path.join(DATA, "cc", "cc.log")

FLOW_PATS = [
    # (正则, 动作短词, 取「从→到」和内容的函数)
    (re.compile(r"\[push\] target=(\S+?)\((\w[^)]*)\) state=(\w+) :: (.*)"),
     "路由", lambda m: (f"手机 → {m.group(1)}", f"[{m.group(2)}·{m.group(3)}] {m.group(4)}")),
    (re.compile(r"\[route\] take\(([^)]*)\) sid=(\w+) :: (.*)"),
     "接单", lambda m: (f"手机 → {m.group(2)}", f"[{m.group(1)}] {m.group(3)}")),
    (re.compile(r"\[route\] skip\(([^)]*)\) sid=(\w+) :: (.*)"),
     "让出", lambda m: (f"{m.group(2)} 不接", f"[{m.group(1)}] {m.group(3)}")),
    (re.compile(r"\[routed\] 注入 sid=(\w+) (\d+) 条"),
     "投递", lambda m: (f"钉钉 → {m.group(1)}", f"{m.group(2)} 条归它的消息")),
    (re.compile(r"\[send\] 【CC[·)]?([^】]*)】(.*)"),
     "急推", lambda m: (f"{m.group(1) or 'CC'} → 手机", m.group(2))),
    (re.compile(r"\[push\] 遥控窗口(开|关)"),
     "窗口", lambda m: ("遥控", "开了，开始盯自聊天" if m.group(1) == "开"
                        else "关了，停止轮询")),
]


def sid_name(tag: str, live: dict) -> str:
    """把日志里的 4 位短标签（如 `7ac5`）翻回人读的项目名。"""
    if "/" in tag:                     # 已经是 `项目名/7ac5` 这种
        return tag.split("/")[0]
    for sid, r in live.items():
        if sid.startswith(tag):
            return (r.get("project") or tag)
    return tag


def clip_word(t: str, width: int) -> str:
    """按显示宽度截断，且**在词/标点边界断，不切半个词**。

    中文没有空格分词，所以优先找标点（。！？；，）当断点；英文找空格。
    找不到合适边界才硬截 —— 但 clip() 保证不会切开一个宽字符。
    """
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    if dw(t) <= width:
        return t
    cut = clip(t, width).rstrip("…")
    tail = cut[-16:]
    for sep in "。！？；，、,.;! ":
        i = tail.rfind(sep)
        if i >= 4:                       # 太靠前就不值得断，宁可硬截
            return cut[:len(cut) - len(tail) + i + 1].rstrip("，, ") + "…"
    return cut + "…"


def strip_md(t: str) -> str:
    """剥掉 markdown 标记。note 是从会话的收尾话里抓的，常带 ** ` # >，
    这些在终端里全是噪音 —— 他嫌 board「难看」，这也是一种难看。"""
    t = re.sub(r"\*\*|__|`{1,3}|^#{1,6}\s*|^>\s*", "", str(t or ""))
    return re.sub(r"\s+", " ", t).strip()


def first_sentence(t: str) -> str:
    """取第一句。心跳那栏一行一个会话，塞整段 note 会撑爆，
    但硬截又会切半句 —— 取第一个完整句子最自然。"""
    t = strip_md(t)
    m = re.search(r"[。！？\n]", t)
    return (t[:m.end()] if m else t).strip()


def dispatch_flow(limit: int = 8) -> list:
    """最近的编队内部动作，时间倒序。一条 = (时间, 动作, 从→到, 内容)。"""
    live = sessions()
    rows = []
    for ln in tail_text(CC_LOG, kb=64):
        if len(ln) < 20:
            continue
        when, rest = ln[:19], ln[20:]
        for pat, kind, fn in FLOW_PATS:
            m = pat.search(rest)
            if m:
                who, what = fn(m)
                # 短标签翻成项目名（看短 id 不如看项目名直观）。
                # 先把已经是「项目/短id」的压成「项目」，免得翻出 `项目名/项目名`。
                who = re.sub(r"([^\s/]+)/[0-9a-f]{4}\b", r"\1", who)
                for tag in re.findall(r"\b[0-9a-f]{4}\b", who):
                    who = who.replace(tag, sid_name(tag, live))
                rows.append((when, kind, who, what))
                break
    for ln in tail_text(EVENTS, kb=64):
        try:
            e = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        what, who = e.get("what", ""), e.get("who", "")
        if who in ("fleet", "push-loop") and what.startswith("唤醒"):
            rows.append((e.get("when", ""), "唤醒", f"{who} → " +
                         sid_name(what[2:10].split("：")[0].strip(), live),
                         what.split("：", 1)[-1]))
        elif e.get("kind") == "task-done":
            rows.append((e.get("when", ""), "交活",
                         f"{sid_name(who, live)} → 台账", what))
        elif e.get("kind") == "turn-done" and "@" in what[:24]:
            rows.append((e.get("when", ""), "交活",
                         f"{sid_name(who, live)} → 主会话", what))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------- 排版小工具
#
# 中文在终端里占两格，用 len() 对齐必歪。board 是要挂 watch 一直看的，
# 歪一列就很难读，所以这几个函数按显示宽度算。

def dw(t: str) -> int:
    """字符串的显示宽度：东亚宽字符和 emoji 都算 2 格。

    不把 emoji 算两格的话，带 ⚠️ ✅ 的行右对齐会多算一格、把时间戳挤出边界。
    """
    import unicodedata
    w = 0
    for ch in str(t):
        if unicodedata.east_asian_width(ch) in "WF" or ord(ch) > 0x1F000:
            w += 2
        elif unicodedata.combining(ch) or ch in "\ufe0f\u200d":
            w += 0                      # 变体选择符/零宽连接符本身不占位
        else:
            w += 1
    return w


def clip(t: str, width: int) -> str:
    """按显示宽度截断，不会把宽字符切一半。"""
    t = str(t).replace("\n", " ")
    if dw(t) <= width:
        return t
    out, w = "", 0
    for c in t:
        cw = dw(c)
        if w + cw > width - 1:
            return out + "…"
        out += c
        w += cw
    return out


def pad(t: str, width: int) -> str:
    t = clip(t, width)
    return t + " " * max(0, width - dw(t))


def fmt_age(sec: int) -> str:
    if sec < 0:
        return "?"
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    if sec < 172800:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def term_width(default=100) -> int:
    try:
        import shutil
        return max(72, min(shutil.get_terminal_size((default, 24)).columns, 200))
    except Exception:                              # noqa: BLE001
        return default


def rule(title: str, width: int) -> str:
    head = f"── {title} "
    return head + "─" * max(0, width - dw(head))


# ---------------------------------------------------------------- 命令

def project_of(cwd: str) -> str:
    """这个目录属于哪个项目，用**项目自己的短名**，不是目录名。

    目录名是 `dingtalk-watch`，但这套东西叫 `dtwatch`；别的项目同理 ——
    目录叫 `xxx-service`，日常说的是「项目A」。手机上看署名要的是后者。
    所以映射写在 config.json 的 `fleet.projects` 里（cwd 前缀 → 短名），
    集中一处配，不用往每个项目仓库里塞标识文件。配不到才退回目录名。
    """
    cwd = os.path.abspath(cwd or os.getcwd())
    cfg = load_json(CONFIG_PATH, {})
    table = ((cfg.get("fleet") or {}).get("projects") or {})
    best, best_len = "", -1
    for path, name in table.items():
        p = os.path.abspath(os.path.expanduser(path))
        if (cwd == p or cwd.startswith(p + os.sep)) and len(p) > best_len:
            best, best_len = name, len(p)
    return best or os.path.basename(cwd)


def beat(sid: str, state="idle", project="", cwd="", note="", where="") -> dict:
    """写一次 sidecar（顺带记事件日志）。dtcc 的 Stop hook 每轮收尾会调它。

    **注意这里不再记「状态/在哪个 pane/心跳时间」** —— 那些由
    `~/.claude/tmux-claude-status.json` 负责，它比这边全（20 个 pane 都在）
    也比这边准（tmux 那侧实时更新，不用等会话收尾）。
    这里只留那个文件没有的三样：项目短名、最后干完的一句话、上次被唤醒的时间。
    `state` 参数保留是为了兼容老调用，实际不落盘。
    """
    if not sid:
        return {"ok": False, "error": "no sid"}
    side = load_json(FLEET, {})
    cwd = cwd or os.getcwd()
    prev = side.get(sid, {})
    rec = {
        # 顺序要紧：显式传的 > config 声明的 > 上次记的。
        # config 排在 prev 前面，改了映射下一次就生效，不用清 fleet.json。
        "project": project or project_of(cwd) or prev.get("project", ""),
        "note": note or prev.get("note", ""),
        "last_wake": prev.get("last_wake", ""),
    }
    # 调用方（Stop hook）给的 note 是硬截到 100 字的，标记落在后面就丢了。
    # 所以只要能自己从 transcript 取到更完整的一段，就用自己取的那份。
    full = last_assistant_text(sid)
    if full and (not note or len(full) > len(note)):
        note = full
        rec["note"] = note
    side[sid] = rec
    save_json(FLEET, side)
    # **note 为空也要写事件。** 收尾停在工具调用上时没有文本，
    # 原来 `if note:` 会把整条事件静默跳过 —— 那个会话在 board 上看着就像死了
    # （实测 a1ebe6e7 / 1031001a 就是这样：hook 跑了、note 空、events 一条没有）。
    # kind=turn-done：这一轮活干完了。board 的「谁交活了」看的就是它，
    # 主会话不用轮询谁在忙 —— 收尾时自己会报。
    what = note or "(收尾无文本)"
    receipt = pending_receipt(sid)
    if receipt and (RECEIPT_FILTER is None or RECEIPT_FILTER(receipt)):
        # 机械回执：不管这段收尾文本本身有没有 @5380，这次一律强制放行，
        # 派活时等的就是「它这次收尾」，不能靠它自己想起来打标记。
        what = "@5380 " + what
        mark_receipted(receipt["id"])
    append_event({"who": sid[:8], "when": ts(now()), "kind": "turn-done",
                  "project": rec["project"],
                  "what": what, "where": where})
    live = sessions().get(sid) or {}
    return {"ok": True, "sid": sid[:8], "project": rec["project"],
            "tmux": live.get("tmux", "?"), "state": live.get("state", "?")}


def cmd_beat(args):
    out = beat(args.sid or env_sid(), args.state, args.project,
               args.cwd, args.note, args.where)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_event(args):
    """只写一条事件，不动心跳。给「我刚产出了什么」用。"""
    rec = {"who": (args.sid or env_sid())[:8],
           "when": ts(now()), "project": args.project or "",
           "what": args.what, "where": args.where or ""}
    append_event(rec)
    print(json.dumps({"ok": True}, ensure_ascii=False))
    return 0


def cmd_list(args):
    """谁在忙、谁闲着、谁静默很久了。状态取自 tmux 那份真相。"""
    live = sessions()
    if not live:
        print("（读不到 ~/.claude/tmux-claude-status.json，或里面没有带 session_id 的 pane）")
        return 0
    rows = sorted(live.values(), key=lambda r: r["age"])
    print(f"{'sid':10} {'项目':16} {'tmux':16} {'状态':12} 最后一句")
    for r in rows:
        mark = {"idle": "○ 闲着", "busy": "● 在跑"}.get(r["state"], r["state"])
        if r["age"] > args.stale_after:
            mark = f"· 静默{fmt_age(r['age'])}"
        print(f"{r['sid'][:8]:10} {pad(r['project'], 16)} {pad(r['tmux'], 16)} "
              f"{pad(mark, 12)} {r['note'][:38]}")
    return 0


def resolve_target(key: str) -> tuple[str, dict]:
    """按 sid 前缀、项目名或 tmux 窗口名找会话。

    **歧义时绝不猜。** 同一个项目开着两个会话时，按状态/年龄排序选一个看着聪明，
    实际是把活派进错误的 pane（真发生过：一个项目短名匹配到两个会话，
    选中的那个 cwd 根本不在那个项目目录）。宁可让调用方指定，也不要静默选错。
    返回 ("", {"ambiguous": [...]}) 表示歧义。
    """
    live = sessions()
    for sid, r in live.items():
        if sid == key or sid.startswith(key):
            return sid, r
    exact = [(sid, r) for sid, r in live.items() if (r.get("project") or "") == key]
    fuzzy = [(sid, r) for sid, r in live.items()
             if key in (r.get("project") or "") or key in r.get("window_name", "")]
    cands = exact or fuzzy
    if not cands:
        return "", {}
    if len(cands) > 1:
        return "", {"ambiguous": [
            {"sid": sid[:8], "who": disp_of(r), "cwd": r.get("cwd", ""),
             "state": r.get("state", "")} for sid, r in cands]}
    return cands[0]


def cmd_wake(args):
    """把任务打进目标会话的 tmux pane，叫它接着干。

    为什么是 send-keys：Claude Code 没有对外的「注入一条消息」接口，
    而这些常驻会话的价值恰恰在它攒下的上下文里，用 `claude -p` 另起一个
    进程就丢了那些上下文。

    两个踩过的坑，都在下面的代码里防住了：
      1. **必须用 pane-id（%NN）当 target**，不能用 `session:window` ——
         tmux 会话被改名（`journal` → `OS`）之后按名字发就打空，而且不报错。
      2. **文字和回车之间要停一下**。`-l` 把文字塞进输入框是异步的，
         紧跟着发 Enter 会跟文字挤在一起，结果任务停在输入框里没提交 ——
         文本越长越容易中。0.4s 足够，也不影响体感。

    代价是它会打断正在输出的会话，所以 `state=busy` 一律拒绝，
    状态取自 tmux 那份真相而不是自己记的心跳。
    """
    sid, rec = resolve_target(args.target)
    if not sid and rec.get("ambiguous"):
        print(json.dumps({"ok": False, "error": f"「{args.target}」匹配到多个会话，"
                          "请改用 sid 前缀指定（不猜，免得打错 pane）",
                          "candidates": rec["ambiguous"]}, ensure_ascii=False,
                         indent=1))
        return 1
    if not sid:
        print(json.dumps({"ok": False, "error": f"找不到会话：{args.target}",
                          "hint": "fleet.py list 看有哪些"}, ensure_ascii=False))
        return 1
    pane = rec.get("pane", "")               # %NN，稳定标识
    if not pane_alive(pane):
        print(json.dumps({"ok": False, "target": disp_of(rec), "pane": pane,
                          "error": "pane 不在了，这个会话已经关掉"},
                         ensure_ascii=False))
        return 1
    if rec.get("state") == "busy" and not args.force:
        print(json.dumps({"ok": False, "target": disp_of(rec), "state": "busy",
                          "error": "它正在跑，现在打字会打断它；真要插队加 --force"},
                         ensure_ascii=False))
        return 1
    choice_override = False
    if awaiting_choice(pane):
        # 护栏三：对面停在交互选择框上，硬发文字会被当成按键响应，
        # 可能替他确认了一件本该他自己点头的事——这是红线,不是体验问题,
        # 所以放在 pending_input 前面单独判、单独报,状态值也跟它区分开。
        if not args.force:
            print(json.dumps({"ok": False, "target": disp_of(rec),
                              "state": "awaiting-choice",
                              "error": "对面停在一个交互选择框上（Enter to select / "
                                       "↑↓ 导航 / Esc 取消），硬发文字会被当成对这个"
                                       "选择框的按键响应，可能替他确认了本该他自己"
                                       "点头的操作；确认要覆盖加 --force"},
                             ensure_ascii=False))
            return 1
        choice_override = True
    pending = pending_input(pane)
    if pending and not args.force:
        # 护栏一：输入框里排着没提交的东西（尤其 /compact）就别再往里塞字了——
        # 新文字会跟它拼在一起或把它冲掉，压缩指令就这么悄悄没执行过。
        # 误判的代价是"这次没派进去"，比"静默冲掉压缩、会话继续涨"轻得多，
        # 所以宁可严一点，--force 留给确实要插队的时候。
        print(json.dumps({"ok": False, "target": disp_of(rec), "state": "pending-input",
                          "error": f"输入框里有未执行的内容（{pending[:60]}），"
                                   "现在打字会把它冲掉；确认要覆盖加 --force"},
                         ensure_ascii=False))
        return 1
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "target": disp_of(rec),
                          "pane": pane, "task": args.task}, ensure_ascii=False))
        return 0
    if not tmux_send(pane, args.task):
        print(json.dumps({"ok": False, "error": "send-keys 失败（pane 为空/不是 %NN/已关）",
                          "pane": pane}, ensure_ascii=False))
        return 1
    mark_wake(sid, args.task)
    # **派出去的活自动进台账。** 不然台账永远是死的 —— 2026-07-31 发现它最后一次
    # 写入停在前一天 21:45，正是因为派活都绕过了这里（裸 tmux send-keys）。
    # 派活的人不该还要记得手动补一次 `task add`；这条自动记，收工用 `task done <id>` 销号。
    tid = task_id()
    task_write({"id": tid, "text": args.task,
                "target": rec.get("project") or sid[:8],
                "target_sid": sid, "dispatcher": env_sid()[:8] or "unknown",
                "receipt": "pending",
                "status": "dispatched", "reason": "fleet.py wake 直接派的",
                "ts": ts(now())})
    append_event({"who": "fleet", "when": ts(now()), "kind": "wake",
                  "project": rec.get("project", ""),
                  "what": f"唤醒 {disp_of(rec)}：{args.task[:120]}", "where": pane})
    # 护栏二：把这次派活时目标会话的上下文占用亮出来，派活的人一眼就能看见
    # "这个会话快满了"，不用等它交活才发现——2026-07-31 晚上主会话派活前
    # 从来不看对方上下文，直接给 529k 的会话又派了新活。
    result = {"ok": True, "target": disp_of(rec), "pane": pane,
              "task": tid, "ctx": ctx_usage(pane)}
    if choice_override:
        # --force 绕过护栏三时必须在返回里显式喊出来，不能只在拒绝时警告、
        # 覆盖成功时却安安静静——那等于让人更容易忽略自己刚做了什么。
        result["warning"] = ("已用 --force 覆盖一个正等待人工确认的交互选择框，"
                              "确认这不是替他做了本该他自己点头的决定")
    print(json.dumps(result, ensure_ascii=False))
    return 0


def mark_wake(sid: str, task: str):
    """只在 sidecar 上记「上次被唤醒」。不写状态 —— 状态归 tmux 那份真相。"""
    side = load_json(FLEET, {})
    rec = side.get(sid) or {}
    rec["last_wake"] = ts(now())
    rec["note"] = f"被唤醒：{task[:60]}"
    side[sid] = rec
    save_json(FLEET, side)


def inbox_stats() -> tuple[dict, dict, dict, dict]:
    """从 dtwatch 的队列里算 board 要的四组数。只读文件，不打接口 ——
    board 是挂 watch 每几秒刷一次的，不能有网络调用。

    ⚠️ 这里要分清两种"被路由"：`route_of` 先按**会话名**匹配，再按**发送人名**匹配。
    所以「全员群」里某个 route 表里的人发的日报也会被路由走（因为发送人匹配上了），
    但那不等于"全员群归那个项目会话管"。按会话名统计会得出「全员群 → 项目A」这种荒谬结论，
    所以下面按 **route 表的键** 统计命中量，而"待接管"只看**一条路由都没匹配上**的消息。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dtwatch", os.path.join(BASE, "dtwatch.py"))
    dtw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dtw)
    cfg = dtw.load_json(dtw.CONFIG_PATH, {})
    triage = dtw.load_json(dtw.TRIAGE, {})
    ledger = dtw.load_json(dtw.INJECTED, {})
    route = (cfg.get("route") or {})
    order = {"high": 0, "normal": 1, "low": 2}

    hits = {k: {"n": 0, "open": 0} for k in route}   # route 表每个键命中多少
    unrouted = {}      # 会话名 -> {"n","open"}  一条路由都没匹配上的
    undeliv = {}       # 目标 sid -> 归它但还没投递
    for r in dtw.read_inbox():
        conv, sender = r.get("conv") or "?", r.get("sender") or ""
        sid, _ = dtw.route_of(r, cfg)
        t = triage.get(r["id"], {})
        is_open = (t.get("status") not in ("done", "ignored")
                   and order.get(r.get("level", "low"), 2) <= 1)
        if sid:
            key = conv if conv in route else (sender if sender in route else "")
            if key:
                hits[key]["n"] += 1
                hits[key]["open"] += 1 if is_open else 0
            if is_open and r["id"] not in ledger:
                undeliv[sid] = undeliv.get(sid, 0) + 1
        else:
            d = unrouted.setdefault(conv, {"n": 0, "open": 0})
            d["n"] += 1
            d["open"] += 1 if is_open else 0
    return hits, unrouted, undeliv, cfg


def cmd_board(args):
    """board 已经改成网页版了 —— 这里只负责生成并告诉他在哪打开。

    2026-07-31 退役终端版：等宽字符画出来的表格，中英文混排永远差一格，
    长文本要么截半截要么撑爆行；换成 HTML 之后这些限制都不存在了，
    还能把图片直接显示出来。**数据层没变**，还是这个文件里的
    sessions() / inbox_stats() / tasks_load() / dispatch_flow()，
    所以网页和别的命令看到的永远是同一份数。
    """
    import subprocess
    cmd = [sys.executable, os.path.join(BASE, "board_html.py")]
    if args.loop:
        cmd += ["--loop", str(args.loop)]
    if args.open:
        cmd += ["--open"]
    return subprocess.call(cmd)


def cmd_atme(args):
    """把还没处理的「@我」消息**全文**打出来。

    board 上一行只能放摘要，他看了知道有事却不知道是什么事，还得再问一遍 ——
    这个命令就是补那一步：时间、谁、哪个群、完整正文，一条一段。
    带图的直接给现成的下载命令，复制粘贴就能看图。
    """
    rows = at_me_open(limit=args.limit or 0)
    if not rows:
        print("  ✅ 没有未处理的 @我 消息")
        return 0
    W = min(term_width(), 100)
    print(c(f"  未处理的 @你 消息：{len(rows)} 条", "warn"))
    for i, r in enumerate(rows, 1):
        body, media = split_media(r.get("text", ""))
        print()
        print(c(f"  {i}. {r['time']}  {r.get('sender','?')}｜{r.get('conv','?')}", "warn"))
        for line in (body or "(无文字)").split("\n"):
            # 正文按终端宽度折行，不截断 —— 这个命令的意义就是让他读全
            while dw(line) > W - 6:
                print(f"     {clip(line, W - 6).rstrip('…')}")
                cut = len(clip(line, W - 6).rstrip("…"))
                line = line[cut:]
            if line:
                print(f"     {line}")
        for mid in media:
            print(c(f"     图片 → 复制这条命令看图：", "dim"))
            print(f"     dws chat message download-media --type mediaId \\\n"
                  f"       --resource-id '{mid}' \\\n"
                  f"       --message-id '{r['id']}' \\\n"
                  f"       --open-conversation-id '{r.get('cid','')}' \\\n"
                  f"       --output ~/Downloads/atme_{i}.png")
    print()
    print(c("  处理完用：python3 dtwatch.py mark <id> --status done --note \"...\"", "dim"))
    return 0


def cmd_log(args):
    """看事件日志。这是各会话之间互相知道对方干了什么的地方。"""
    if not os.path.exists(EVENTS):
        print("（还没有事件）")
        return 0
    rows = []
    with open(EVENTS, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if args.project and args.project not in (r.get("project") or ""):
                continue
            rows.append(r)
    for r in rows[-args.tail:]:
        where = f"  → {r['where']}" if r.get("where") else ""
        print(f"{r.get('when','')}  [{(r.get('project') or '-')[:14]:14}] "
              f"{(r.get('who') or '?'):9} {r.get('what','')}{where}")
    if not rows:
        print("（没有匹配的事件）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="fleet", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("beat", help="报心跳（给 Stop hook 用）")
    p.add_argument("--sid", default="")
    p.add_argument("--state", default="idle",
                   choices=["idle", "busy", "blocked"])
    p.add_argument("--project", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--note", default="", help="一句话：刚干完什么")
    p.add_argument("--where", default="", help="产出落在哪（文件/commit/消息 id）")
    p.set_defaults(fn=cmd_beat)

    p = sub.add_parser("event", help="只写一条事件日志")
    p.add_argument("--what", required=True)
    p.add_argument("--where", default="")
    p.add_argument("--project", default="")
    p.add_argument("--sid", default="")
    p.set_defaults(fn=cmd_event)

    p = sub.add_parser("list", help="谁在忙谁闲着")
    p.add_argument("--stale-after", type=int, default=900,
                   help="心跳多久没更新算静默（秒，默认 900）")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("wake", help="叫醒一个会话，给它派活")
    p.add_argument("target", help="sid 前缀，或项目名")
    p.add_argument("--task", required=True, help="要它干什么（会被打进 pane）")
    p.add_argument("--force", action="store_true", help="它在跑也插队")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_wake)

    p = sub.add_parser("board", help="生成网页版 board（终端版已退役）")
    p.add_argument("--loop", type=int, default=0, help="每 N 秒重生成一次（常驻）")
    p.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("task", help="任务台账：add / dispatch / done")
    p.add_argument("op", choices=["add", "dispatch", "done"])
    p.add_argument("id", nargs="?", default="", help="任务 id（支持前缀），add 不用给")
    p.add_argument("--text", default="", help="add 用：这件活是什么")
    p.add_argument("--target", default="", help="派给哪个会话（项目名或 sid 前缀）")
    p.add_argument("--reason", default="", help="为什么还没派 / 备注")
    p.add_argument("--where", default="", help="done 用：产出落在哪")
    p.add_argument("--wake", action="store_true",
                   help="dispatch 时若目标闲着就顺手叫醒它")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("atme", help="打印未处理的「@我」消息全文（含图片下载命令）")
    p.add_argument("--limit", type=int, default=0, help="最多几条（0=全部）")
    p.set_defaults(fn=cmd_atme)

    p = sub.add_parser("log", help="看事件日志")
    p.add_argument("--tail", type=int, default=30)
    p.add_argument("--project", default="")
    p.set_defaults(fn=cmd_log)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
