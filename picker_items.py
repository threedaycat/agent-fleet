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
    inbox_stats()  待投递队列、积压
    sessions()     心跳/pane/状态
    at_me_open()   要他回的消息 —— **只在 preview/action 用**，
                   list 里已经撤掉了，理由见 cmd_list 的注释
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


def split_queues(live: dict, backlog: dict, taken=(), label=None) -> tuple[list, list]:
    """待投递队列拆成「还能跳过去的」和「孤儿」。**纯函数。**

    孤儿 = 这个 sid 已经不在 `fleet.sessions()` 里，**给不出舰队坐标**。
    以前它照样列出来，标签退化成 `sid[:8]` —— 那既违反 `fleet.disp_of` 写死的
    「不露 session_id 碎片」，也是一行**跳无可跳**的死行：按回车只会得到
    「这个会话已经不在了」。2026-09-03 实测两行孤儿(7ac581c7 / 11f9b87a)
    分别压着 439 和 186 条，而那两个会话最后一次写盘是 8-05 和 8-07。

    所以列表里不给它们位置，但**不能就这么消失**：625 条投不出去的消息是真问题，
    只是不属于「待办」。它们从第二个返回值走「系统」区，见 `system_lines`。
    """
    label = label or (lambda rec: fleet.disp_of(rec, short=True))
    taken = set(taken)
    ok, orphan = [], []
    for sid, n in (backlog or {}).items():
        if not n or sid in taken:
            continue
        # 判据是**叫不叫得出名字**，不是「记录在不在」。
        # 第一版写的 `(live or {}).get(sid)`，被自己的用例打了：`{}` 是 falsy，
        # 一个存在但记录为空的会话会被判成孤儿；反过来，记录在、但 disp_of
        # 给不出坐标的，照样会渲染成一行空白标签。两种都是错的。
        rec = (live or {}).get(sid)
        (ok if rec is not None and label(rec) else orphan).append((sid, n))
    ok.sort(key=lambda x: -x[1])
    orphan.sort(key=lambda x: -x[1])
    return ok, orphan


def role_coords(decl: dict) -> set:
    """声明键 `sess:win#idx` → 运行期坐标 `sess:win.idx`。

    跟 `console.role_report` 里那一行是同一个换算。那边是为了查表，这边是为了
    **只给这几个 pane 抓上下文** —— 全部 31 个 pane 抓一遍 0.13s，
    只抓声明出来的 3 个是 0.01s，而 `list` 有 2 秒死线(超了会把附加条目整片丢掉)。
    """
    out = set()
    for key in (decl or {}):
        sess, _, rest = key.partition(":")
        win, _, idx = rest.partition("#")
        if sess and win and idx:
            out.add(f"{sess}:{win}.{idx}")
    return out


def heal_why() -> dict:
    """「这个状态该不该重启」的说明，**从 fleet_up 借**，不在这儿再写一份。
    借不到就给空字典 —— 少一行说明，好过两处说法不一致。"""
    try:
        import fleet_up
        return dict(getattr(fleet_up, "HEAL_SKIP_WHY", {}) or {})
    except Exception:                                   # noqa: BLE001
        return {}


def session_file(sid: str):
    """那个会话的 transcript 路径，找不到给 None。

    只用来回答「它最后一次写盘是什么时候」——判断一个孤儿队列是昨天掉的
    还是一个月前掉的，这两件事的处理方式完全不同。
    """
    root = os.path.expanduser("~/.claude/projects")
    try:
        for d in os.listdir(root):
            f = os.path.join(root, d, f"{sid}.jsonl")
            if os.path.exists(f):
                return f
    except OSError:
        pass
    return None


def role_alerts(report: list) -> list:
    """只留「要他看一眼」的角色。**纯函数。**

    `live` 不出现——正常的东西不该占位置。
    `unknown` 也不出现：读不出**不是结论**，把它排进待办会让他去修一个
    可能根本不存在的问题。要看全量(含 unknown)按回车进 dash。
    """
    order = {"missing": 0, "never": 1, "stale": 2}
    return sorted((r for r in (report or []) if r.get("state") in order),
                  key=lambda r: (order[r["state"]], r.get("role") or ""))


# ---------------------------------------------------------------- list

def console_mod():
    """懒导 console。**失败不静默** —— 静默会让「系统状态读不出」看起来像一切正常。
    dtcc.dtwatch_mod 那次就是被 `except: pass` 藏了三天。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import console
    return console


ROLE_MARK = {"missing": (RED, "[✗]"), "never": (YEL, "[○]"), "stale": (YEL, "[!]")}


def collect_role_report(at: float) -> list:
    """采角色现状。**只做 IO**，判据全在 `console.role_state`，不在这儿复制一份。

    只给**声明出来的**那几个 pane 抓上下文。全部 31 个 pane 抓一遍实测 0.134s，
    3 个只要 0.01s —— 差别本身不大，但 `list` 有 2 秒死线且超时会把**整片**
    附加条目丢掉，所以能省的就省。

    上下文必须抓：不抓的话 `role_state` 一律给 `unknown`（读不出≠没干活，
    那个函数拒绝猜），整个角色区就全是问号，等于没有。
    """
    c = console_mod()
    panes = c.collect_panes(with_ctx=False)
    decl = c.declared_roles()
    want = role_coords(decl)
    for p in panes:
        if p.get("coord") in want:
            try:
                p["ctx"] = fleet.ctx_usage(p.get("pane"))
            except Exception:                           # noqa: BLE001
                p["ctx"] = None                         # 读不出就是 None，别拿 0 冒充
    return c.role_report(panes, decl, at)


def system_lines(orphans=(), alerts=()) -> list[str]:
    """「系统状态」区 —— 舰队自己的健康,不是待办。

    ⚠️ **这里只允许放便宜的判据。** picker 侧给 `list` 的死线是
    `CLAUDE_TMUX_EXTRA_TIMEOUT`（默认 2 秒），超时 `run_with_deadline` 会把
    **全部**附加条目一起丢掉 —— 不只是这一条。
    2026-09-03 重测：`list` 基线 0.23s（原注释写的 0.5–0.8s 是撤掉 @我 扫描之前的数），
    加上只给声明角色抓上下文约 +0.02s。原注释说「每个 pane 抓屏 0.23 秒」也是错的，
    实测 31 个 pane 全抓共 0.134s。**数字重测过才敢往里加东西。**
    """
    try:
        c = console_mod()
        checks, svcs = c.collect_checks(), c.collect_services()
        ok_c = sum(1 for x in checks if x["ok"])
        ok_s = sum(1 for x in svcs if c.TONE.get(x["state"]) == "ok")
        bad = ok_c != len(checks) or ok_s != len(svcs)
        mark = f"{YEL}[*]{OFF}" if bad else f"{DIM}[*]{OFF}"
        body = f"自检 {ok_c}/{len(checks)}   服务 {ok_s}/{len(svcs)}"
    except Exception as e:                              # noqa: BLE001
        mark, body = f"{RED}[*]{OFF}", f"读不出（{type(e).__name__}: {e}）"

    lines = [header(f"{DIM}▾ 系统{OFF}"),
             row(f"  {mark} {col('系统状态', 22)}  {DIM}{body}{OFF}", "system:dash")]

    for a in alerts:
        tone, glyph = ROLE_MARK.get(a.get("state"), (DIM, "[?]"))
        # 坐标优先 —— 声明了却没 pane 的角色根本没有坐标，那时才退回角色名。
        who = a.get("coord") or f"{a.get('role')}（没有 pane）"
        lines.append(row(
            f"  {tone}{glyph}{OFF} {col(who, 22)}  "
            f"{DIM}{a.get('role')} {clip(a.get('note') or '', 34)}{OFF}",
            f"role:{a.get('role')}"))

    if orphans:
        n = sum(x[1] for x in orphans)
        lines.append(row(
            f"  {YEL}[Q]{OFF} {col('孤儿队列', 22)}  "
            f"{DIM}{len(orphans)} 个会话 · {n} 条投不出去{OFF}", "orphans"))
    return lines


def cmd_list() -> int:
    """picker 的第一需求是**切 Claude**，附加条目只配当配角。

    所以这里只放「跟某个会话有关、且要他动手」的东西：卡住的、有队列没消费的。
    @我的钉钉消息**不进 picker**（2026-09-01 撤掉）—— 它只有两条关闭路径
    （`dtwatch.py --status done`、在钉钉上给那条贴表情，见 dtwatch.sweep_acks），
    日常刷过去的两条都不走，于是 7 月底的还挂在「未处理」里，实测 396 条。
    那不是待办，是一条只进不出的流水；而且 picker 侧本来就会把它折叠掉，
    等于花 ~1 秒扫 20MB 的 inbox.ndjson 算出一堆看不见的行。
    要看积压走 `fleet.py atme`，数据一条没删。
    """
    live = fleet.sessions()
    backlog = session_backlog()
    stuck = stuck_sessions(live, backlog)
    queues, orphans = split_queues(live, backlog, taken=[s for s, *_ in stuck])

    # 角色采不到就当没有 —— 不能让「舰队体检读不出」把整个 picker 拖没了。
    try:
        alerts = role_alerts(collect_role_report(time.time()))
    except Exception:                                   # noqa: BLE001
        alerts = []

    total = len(stuck) + len(queues)
    sysx = system_lines(orphans=orphans, alerts=alerts)
    # 「待办」区没事就整个不出现；「系统」区**永远出现** ——
    # 「现在没事」正是最该能一眼确认系统本身还活着的时候。
    if not total:
        print("\n".join(sysx))
        return 0

    lines = list(sysx) + [header(f"{RED}▾ 待办 · {total}{OFF}")]

    for sid, rec, age, n in stuck:
        lines.append(row(
            f"  {YEL}[!]{OFF} {col(fleet.disp_of(rec, short=True), 22)}  "
            f"静默 {col(fmt_age(age), 6)}  {DIM}积压 {n} 条{OFF}",
            f"stuck:{sid}"))

    for sid, n in queues:
        # rec 一定在（split_queues 已经把没坐标的挑走了），所以这里不需要
        # `else sid[:8]` 那个退路 —— 那个退路正是「会话显示」的来源。
        lines.append(row(
            f"  {DIM}[Q]{OFF} {col(fleet.disp_of(live[sid], short=True), 22)}  "
            f"{DIM}{n} 条待投递{OFF}",
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

    if kind == "role":
        try:
            rep = collect_role_report(time.time())
        except Exception as e:                          # noqa: BLE001
            print(f"{RED}角色读不出{OFF}：{type(e).__name__}: {e}")
            return 0
        cur = [r for r in rep if r.get("role") == key]
        if not cur:
            print("（这个角色的声明已经不在了）")
            return 0
        for r in cur:
            print(f"{r.get('role')}  {r.get('coord') or '(没有 pane)'}   {r.get('state')}")
            print(f"{DIM}{r.get('note') or ''}{OFF}")
        print(f"{DIM}{'─' * 46}{OFF}")
        print(heal_why().get(cur[0].get("state"), ""))
        pane = cur[0].get("pane")
        if pane and fleet.pane_alive(pane):
            print(f"\n{DIM}—— 它的画面 ——{OFF}")
            print(fleet.pane_tail(pane, lines=20) or "(空)")
        print(f"\n{DIM}回车 = 跑一次 heal 的 dry-run（只报不做）{OFF}")
        return 0

    if item_id == "orphans":
        live, backlog = fleet.sessions(), session_backlog()
        _, orphans = split_queues(live, backlog)
        if not orphans:
            print("（没有孤儿队列了）")
            return 0
        print("这些会话已经不在编队里，给不出坐标，所以不进「待办」——")
        print("但它们名下的消息**投不出去**，不是 0 条。")
        print(f"{DIM}{'─' * 46}{OFF}")
        for sid, n in orphans:
            f = session_file(sid)
            when = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(f))) \
                if f else "查不到会话文件"
            print(f"  {sid[:8]}  {col(str(n) + ' 条', 8)}  最后写盘 {when}")
        print(f"\n{DIM}回车 = 打印它们的完整 sid，好让你自己决定怎么处理{OFF}")
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

    if kind == "role":
        # **只跑 dry-run。** heal --apply 会建 pane、发消息，那是有副作用的动作，
        # 不能挂在一个「按回车看看」的键上。
        rc, out = fleet.sh([sys.executable, os.path.join(HERE, "fleet_up.py"), "heal"],
                           timeout=30)
        print(out or f"(heal 没有输出，退出码 {rc})")
        print(f"\n{DIM}要真动手：python3 {os.path.join(HERE, 'fleet_up.py')} heal --apply{OFF}")
        return 0

    if item_id == "orphans":
        live, backlog = fleet.sessions(), session_backlog()
        _, orphans = split_queues(live, backlog)
        if not orphans:
            print("（没有孤儿队列了）")
            return 0
        for sid, n in orphans:
            print(f"{sid}\t{n}")
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
