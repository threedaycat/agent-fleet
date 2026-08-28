#!/usr/bin/env python3
"""给 claude-tmux-sessions 那个 picker 供「附加条目」。

picker 那边是通用可分享的 MIT 仓库，**它不知道这些条目是什么**——
它只认「一行字 + 一个不透明 id」，拿 id 回来问我要预览、让我执行动作。
所有钉钉/公司相关的东西都关在这个文件里，那边一个字都不许出现。
契约见 PICKER-PLAN.md 第三节。

    picker_items.py list            每行一个条目，TAB 分隔
    picker_items.py preview <id>    那条的完整信息
    picker_items.py action  <id>    回车动作

数据全部走 fleet.py 现成的函数，不自己重算一套：
    at_me_open()   要他回的消息（已经带全文和图片下载命令）
    inbox_stats()  待投递队列、积压
    sessions()     心跳/pane/状态
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fleet                                            # noqa: E402

# 卡住的判定：静默超过这个秒数、且名下还有没投出去的活。
# 不硬编码在 picker 侧 —— 写入端语义变了，这里改一个数就对齐。
STUCK_AFTER = int((fleet.load_json(fleet.CONFIG_PATH, {}).get("picker") or {})
                  .get("stuck_after_seconds", 900))

# 颜色克制：只给「要他处理」和「异常」上色，其余一律不上。
# picker 开了 --ansi，这些码会被正常渲染。
RED = "\033[1;31m"
YEL = "\033[33m"
DIM = "\033[2m"
OFF = "\033[0m"


def fmt_age(sec: int) -> str:
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分"
    if sec < 86400:
        return f"{sec // 3600}小时"
    return f"{sec // 86400}天"


def clip(t: str, n: int) -> str:
    """按显示宽度裁剪。直接用 fleet 那套（中文算两列），别自己再写一份。"""
    return fleet.clip(" ".join(str(t or "").split()), n)


def col(t: str, n: int) -> str:
    """裁到 n 列再补齐到 n 列。**必须用 fleet.pad 而不是 f-string 的 `:<n`**
    —— 后者按字符数补，中文一个字占两列，列就全歪了。"""
    return fleet.pad(clip(t, n), n)


def row(display: str, item_id: str) -> str:
    """一条附加条目。列的含义见 PICKER-PLAN.md：
    $1 显示 / $2 pane(空) / $3 session(空) / $4 行号(空) / $5 extra / $6 id"""
    return f"{display}\t\t\t\textra\t{item_id}"


def header(text: str) -> str:
    """区头。$2/$3/$5 都空 —— 按 picker 现有规则这就是一个 header 行，
    pane 模式下光标跳过它，session 模式下选中它会安全退出。"""
    return f"{text}\t\t"


# ---------------------------------------------------------------- 数据

def atme_items() -> list:
    """要他回的消息。

    在 fleet.at_me_open() 之上多做两件事：
      - **按 id 去重**：inbox.ndjson 是只追加的，同一条会重复出现，
        取最后一条（reclassify 重打标之后写在后面）。
      - **跳过贴过表情的**：`acked:<表情>` 是这个仓库一贯的「已处理」口径
        （sweep_acks 见到表情直接写 triage=done，但它只扫 level>low，
        所以贴过表情的低优先条目只有 flag 没有 triage 记录）。
        不跳的话这里会列出他已经 OK 过的事。
    """
    seen = {}
    for r in fleet.at_me_open(limit=0):
        seen[r["id"]] = r
    out = []
    for r in seen.values():
        if any(str(f).startswith("acked:") for f in (r.get("flags") or [])):
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("time", ""))
    return out


def session_backlog() -> dict:
    """{sid: 还没投出去的条数}。inbox_stats 的第三个返回值。"""
    try:
        _, _, undeliv, _ = fleet.inbox_stats()
        return undeliv or {}
    except Exception:                                   # noqa: BLE001
        return {}


def norm_age(rec: dict) -> int:
    """心跳静默了多久。防住写入端把 updated_at 从秒改成毫秒的情况
    —— 那会让静默时长算错 1000 倍，而且不报错（见 PICKER-PLAN.md 第六节）。"""
    age = rec.get("age")
    if not isinstance(age, (int, float)):
        return 0
    return int(age / 1000) if age > 1e9 else int(age)


def stuck_sessions(live: dict, backlog: dict) -> list:
    """卡住 = 静默够久 + 名下还有没投出去的活。

    只有「静默」不算卡住 —— 他手边闲着的 pane 一大堆，那是正常的。
    有活压着又没动静，才值得他看一眼。
    """
    out = []
    for sid, rec in live.items():
        n = backlog.get(sid, 0)
        if not n:
            continue
        # status 词表变了就当未知，不据此判卡住（宁可不报，也别乱报）
        if rec.get("state") not in ("idle", "blocked", "?"):
            continue
        age = norm_age(rec)
        if age < STUCK_AFTER:
            continue
        out.append((sid, rec, age, n))
    out.sort(key=lambda x: -x[2])
    return out


# ---------------------------------------------------------------- list

def console_mod():
    """懒导 console。**失败不静默** —— 静默会让「系统状态读不出」看起来像一切正常。
    dtcc.dtwatch_mod 那次就是被 `except: pass` 藏了三天。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import console
    return console


def system_lines() -> list[str]:
    """「系统状态」区。

    ⚠️ **这里只允许放便宜的判据。** picker 侧给 `list` 的死线是
    `CLAUDE_TMUX_EXTRA_TIMEOUT`（默认 2 秒），超时 `run_with_deadline` 会把
    **全部**附加条目一起丢掉 —— 不只是这一条。实测 `list` 本身已经用掉
    0.5–0.8 秒，而每个 pane 抓一次屏要 0.23 秒。所以角色和上下文（都要抓屏）
    留给 `preview`，它是选中才跑、没有死线。
    """
    try:
        c = console_mod()
        checks, svcs = c.collect_checks(), c.collect_services()
        ok_c = sum(1 for x in checks if x["ok"])
        ok_s = sum(1 for x in svcs if c.TONE.get(x["state"]) == "ok")
        bad = ok_c != len(checks) or ok_s != len(svcs)
        mark = f"{YEL}[*]{OFF}" if bad else f"{DIM}[*]{OFF}"
        body = f"自检 {ok_c}/{len(checks)}   服务 {ok_s}/{len(svcs)}"
    except Exception as e:
        mark, body = f"{RED}[*]{OFF}", f"读不出（{type(e).__name__}: {e}）"
    return [header(f"{DIM}▾ 系统{OFF}"),
            row(f"  {mark} {col('系统状态', 22)}  {DIM}{body}{OFF}", "system:dash")]


def cmd_list() -> int:
    live = fleet.sessions()
    backlog = session_backlog()
    atme = atme_items()
    stuck = stuck_sessions(live, backlog)
    queues = [(sid, n) for sid, n in backlog.items()
              if not any(sid == s for s, *_ in stuck)]
    queues.sort(key=lambda x: -x[1])

    total = len(atme) + len(stuck) + len(queues)
    # 「待办」区没事就整个不出现；「系统」区**永远出现** ——
    # 「现在没事」正是最该能一眼确认系统本身还活着的时候。
    if not total:
        print("\n".join(system_lines()))
        return 0

    # 系统区排在**最前面**：实测「待办」区有 396 条（@我的积压全量倒进来），
    # 系统状态放后面就落在第 397 行 —— 等于看不见。它只占两行，值得占最上面。
    lines = list(system_lines()) + [header(f"{RED}▾ 待办 · {total}{OFF}")]

    for r in atme:
        text, _ = fleet.split_media(r.get("text") or "")
        text = fleet.strip_ats(text)
        where = "私聊" if r.get("single") else (r.get("conv") or "")
        lines.append(row(
            f"  {RED}[@]{OFF} {(r.get('time') or '')[5:16]}  "
            f"{col(r.get('sender') or '?', 12)}  "
            f"{DIM}{col(where, 16)}{OFF}  {clip(text, 42)}",
            f"atme:{r['id']}"))

    for sid, rec, age, n in stuck:
        lines.append(row(
            f"  {YEL}[!]{OFF} {col(fleet.disp_of(rec, short=True), 22)}  "
            f"静默 {col(fmt_age(age), 6)}  {DIM}积压 {n} 条{OFF}",
            f"stuck:{sid}"))

    for sid, n in queues:
        rec = live.get(sid) or {}
        who = fleet.disp_of(rec, short=True) if rec else sid[:8]
        lines.append(row(
            f"  {DIM}[Q]{OFF} {col(who, 22)}  {DIM}{n} 条待投递{OFF}",
            f"queue:{sid}"))

    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------- preview

def find_atme(mid: str):
    for r in atme_items():
        if r["id"] == mid:
            return r
    return None


def cmd_preview(item_id: str) -> int:
    kind, _, key = item_id.partition(":")

    if kind == "system":
        # 重活都在这儿：抓屏、角色、上下文。preview 是选中才跑，没有死线。
        try:
            import dash
            snap = dash.snapshot(with_ctx=True)
            for text, tone in dash.compose(snap, 58):
                print(dash.paint(text, tone, color=True))
        except Exception as e:
            print(f"{RED}系统状态读不出{OFF}：{type(e).__name__}: {e}")
        return 0

    if kind == "atme":
        r = find_atme(key)
        if not r:
            print("（这条已经处理掉了）")
            return 0
        text, media = fleet.split_media(r.get("text") or "")
        where = "私聊" if r.get("single") else (r.get("conv") or "")
        print(f"{r.get('sender')}  ·  {where}  ·  {r.get('time')}")
        print(f"{DIM}{'─' * 46}{OFF}")
        print(fleet.strip_ats(text) or "(空)")
        if media:
            print(f"\n{DIM}图片/文件，下载命令：{OFF}")
            for m in media:
                print(f"  dws chat message download-media --media-id {m}")
        print(f"\n{DIM}回车 = 看全文并转给 desk 决定怎么回{OFF}")
        return 0

    if kind in ("stuck", "queue"):
        rec = fleet.sessions().get(key) or {}
        if not rec:
            print("（这个会话已经不在了）")
            return 0
        n = session_backlog().get(key, 0)
        print(f"{fleet.disp_of(rec)}   pane {rec.get('pane')}   "
              f"{rec.get('state')}   静默 {fmt_age(norm_age(rec))}")
        print(f"{DIM}cwd {rec.get('cwd', '')}{OFF}")
        print(f"{DIM}{'─' * 46}{OFF}")
        if rec.get("note"):
            print(f"最后一句：{clip(rec['note'], 300)}\n")
        print(f"名下还有 {n} 条没投出去。")
        pane = rec.get("pane", "")
        if pane and fleet.pane_alive(pane):
            print(f"\n{DIM}—— 它的画面 ——{OFF}")
            print(fleet.pane_tail(pane, lines=25) or "(空)")
        else:
            print(f"\n{YEL}pane 已经不在了{OFF}")
        print(f"\n{DIM}回车 = 叫醒它去消费队列{OFF}")
        return 0

    print(f"（不认识的条目：{item_id}）")
    return 0


# ---------------------------------------------------------------- action

def wake(sid: str, task: str) -> int:
    rec = fleet.sessions().get(sid) or {}
    if not rec:
        print("这个会话已经不在了。")
        return 1
    pane = rec.get("pane", "")
    who = fleet.disp_of(rec)
    if not pane or not fleet.pane_alive(pane):
        print(f"{who} 的 pane 已经关了，叫不醒。")
        return 1
    if rec.get("state") == "busy":
        print(f"{who} 正在跑，现在打字会打断它。先不动。")
        return 1
    try:
        ans = input(f"叫醒 {who}（pane {pane}）去消费队列？[y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        print("没动。")
        return 0
    if not fleet.tmux_send(pane, task):
        print("send-keys 失败。")
        return 1
    fleet.mark_wake(sid, task)
    fleet.append_event({"who": "picker", "when": fleet.ts(fleet.now()),
                        "project": rec.get("project", ""),
                        "what": f"从 picker 唤醒 {who}", "where": pane})
    print(f"已叫醒 {who}。")
    return 0


def cmd_action(item_id: str) -> int:
    kind, _, key = item_id.partition(":")

    if kind == "system":
        # 开一个常驻窗口，而不是在弹窗里跑 —— 弹窗一关就没了。
        # 已经有同名窗口就跳过去，不重复开。
        target = os.environ.get("CALLER_PANE") or ""
        sess = fleet.sh(["tmux", "display-message", "-p", "-t", target,
                         "#{session_name}"])[1] if target else ""
        cmd = ["tmux", "new-window"] + (["-t", sess] if sess else []) + \
              ["-n", "dash", f"{sys.executable} {os.path.join(HERE, 'dash.py')}"]
        rc, out = fleet.sh(cmd)
        print("已开一个 dash 窗口" if rc == 0 else f"开窗失败：{out}")
        return 0

    if kind == "atme":
        r = find_atme(key)
        if not r:
            print("（这条已经处理掉了）")
            return 0
        text, media = fleet.split_media(r.get("text") or "")
        where = "私聊" if r.get("single") else (r.get("conv") or "")
        print(f"{r.get('sender')}  ·  {where}  ·  {r.get('time')}\n")
        print(fleet.strip_ats(text) or "(空)")
        if media:
            print("\n图片/文件下载命令：")
            for m in media:
                print(f"  dws chat message download-media --media-id {m}")
        # 第二版才做二级菜单（转 desk / 标忽略）。现在只保证他看得到全文，
        # **不代他回任何消息** —— 对外发消息一律要他本人先看过。
        print("\n（转 desk / 标忽略 第二版再加）")
        try:
            input("\n回车关闭…")
        except EOFError:
            pass
        return 0

    if kind in ("stuck", "queue"):
        return wake(key, "【picker】你名下还有没消费的钉钉队列，"
                         "跑一下 dtwatch.py for-session 看看。")

    print(f"（不认识的条目：{item_id}）")
    return 1


# ---------------------------------------------------------------- main

def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__.strip())
        return 2
    verb = argv[0]
    if verb == "list":
        return cmd_list()
    if verb in ("preview", "action"):
        if len(argv) < 2:
            print(f"用法：picker_items.py {verb} <id>")
            return 2
        return (cmd_preview if verb == "preview" else cmd_action)(argv[1])
    print(f"不认识的动词：{verb}（只有 list / preview / action）")
    return 2


if __name__ == "__main__":
    t0 = time.time()
    try:
        rc = main()
    finally:
        # list 串在 picker 启动路径上，超 200ms 要能看见。写 stderr，
        # 不污染 stdout（那是给 picker 解析的）。
        if os.environ.get("PICKER_ITEMS_TIMING"):
            print(f"[timing] {(time.time() - t0) * 1000:.0f}ms", file=sys.stderr)
    sys.exit(rc)
