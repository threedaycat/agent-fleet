#!/usr/bin/env python3
"""duty 派活器 —— 把「值班」这个角色真正接上活。

## 为什么需要它

2026-08-28 实测：声明六个常驻角色，`duty` / `watch` / `doctor` 上下文 **0k**
（一句话都没说过），`remote` 的 pane 根本不存在。根因不是"死掉了"，是
**从来没有被接上过** —— 角色名在 `fleet.py` / `dtcc.py` / `dtwatch.py` 的派活逻辑里
出现 **0 次**。`roles/*.md` 只在开 pane 那一刻喂进去当首条消息，之后没有任何东西会找它们。

## 为什么是无状态 `claude -p`，不是往那个 pane 发消息

两条路都试得通，选前者：

| | 往 pane 发（`fleet.py wake`） | 无状态 `claude -p`（本文件） |
|---|---|---|
| 结果怎么收 | 抓屏或等 hook，没有契约 | **结构化 JSON，能验证** |
| 能不能测 | 要真 tmux | **判据全是纯函数** |
| pane 挂了 | 整条链路停 | 不受影响 |
| 那个 pane 的上下文 | 会涨（尺子会动） | **仍然 0k** |

最后一行不是缺陷，是发现：**`duty` 本来就不该占一个常驻 pane。**
它的职责是"定期过一遍队列"——那是定时任务的形状，不是常驻会话的形状。
`claude -p --resume` 也不行：它会 fork 出新 session id，够不着那个活着的进程，
所以既拿不到 pane 的上下文、也不会让 pane 的上下文涨。

## 怎么验证它「干对了」

这是 harness 最容易糊过去的一环。两条判据，都是纯函数，都能测：

1. **不许编造 id** —— 上报的每个 id 必须来自这一批输入。模型幻觉出一个
   不存在的消息 id，是最典型的失效模式。
2. **不许悄悄漏** —— 输入的每一条都必须在 `escalate` 或 `absorbed` 里出现一次。
   少一条就是"看漏了"，而看漏和"判断为不用报"在结果上一模一样。

任何一条不过 → 整轮记为失败，**不写报告**。宁可没有结果，也不要一个假结果。

    python3 duty.py run              # 跑一轮
    python3 duty.py run --dry-run    # 只拼提示词，不真调模型
    python3 duty.py show             # 看最近几轮报了什么
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fleet                                                     # noqa: E402

DATA = os.path.join(HERE, "data")
REPORTS = os.path.join(DATA, "duty_reports.ndjson")
STATE = os.path.join(DATA, "duty_state.json")
LOCK = os.path.join(DATA, "duty.lock")

BATCH = 12                    # 一轮最多给它看几条 —— 320 条全塞进去会爆上下文
FRESH_SLOTS = 4               # 这 12 个名额里留几个给**最新**的，见 pick_batch
INTERVAL_MINUTES = 60
# 实测：12 条一轮跑 2 分 42 秒（2026-08-28，sonnet-5）。420 是留一倍余量 ——
# 240 的时候只剩 78 秒margin，机器一忙就会超时，而超时那一轮是白花的。
TIMEOUT_SECONDS = 420
TEXT_CHARS = 200              # 每条正文截到多少字
MODEL = "claude-sonnet-5"

# 输出契约里允许出现的键。**白名单** —— 模型多给的字段一概不收，
# 免得下游哪天开始依赖一个没约定过的字段。
REPORT_KEYS = ("escalate", "absorbed")
ITEM_KEYS = ("id", "why", "suggest")


# ---------------------------------------------------------------- 纯判据


def project(rec: dict) -> dict:
    """把一条台账记录压成喂给模型的最小形状。**白名单**：只导出这几个字段。

    不直接把 `read_inbox()` 的记录丢过去，两个原因：上游加字段时不会悄悄流出去；
    以及正文要截断 —— 12 条不截的群消息就能顶掉几万 token。
    """
    text = " ".join((rec.get("text") or "").split())
    return {
        "id": rec.get("id"),
        "time": rec.get("time"),
        "from": rec.get("sender") or "?",
        "where": "私聊" if rec.get("single") else (rec.get("conv") or "?"),
        "text": text[:TEXT_CHARS] + ("…" if len(text) > TEXT_CHARS else ""),
    }


def pick_batch(items: list[dict], reported: set, cap: int = BATCH,
               fresh_slots: int = FRESH_SLOTS) -> list[dict]:
    """挑这一轮给 duty 看哪些。**纯函数。**

    滤掉报过的之后，名额**分两份**：

    - `fresh_slots` 个给**最新**的；
    - 剩下的给**最旧**的。

    为什么不能只按一个方向排 —— 两个方向各有一个失效模式，实测都会发生：

    - **只从旧到新**：积压 320 条、一轮 12 条、每小时一轮 → 一条刚到的紧急消息
      排在第 321 位，**要等 27 小时才被看到**。
    - **只从新到旧**：最旧的永远排在队尾、永远轮不到，而"已经错过时效"本身
      就是最该上报的事。

    所以两头都取。返回的顺序仍然按时间升序（读起来是时间线）。
    """
    pending = [r for r in items if r.get("id") not in reported]
    pending.sort(key=lambda r: r.get("time") or "")
    if len(pending) <= cap:
        return pending
    n_fresh = max(0, min(fresh_slots, cap))
    newest = pending[len(pending) - n_fresh:] if n_fresh else []
    oldest = pending[:cap - n_fresh]
    return sorted(oldest + newest, key=lambda r: r.get("time") or "")


def build_prompt(role_text: str, triage_text: str, batch: list[dict]) -> str:
    """拼提示词。角色定义**原样**喂进去 —— 那是它的口径，我不改写。"""
    ids = [b["id"] for b in batch]
    return "\n".join([
        role_text.strip(),
        "",
        "---",
        "",
        "## 本机巡检口径（以这份为准）",
        "",
        triage_text.strip() or "（没有本机口径，按上面的通用判据）",
        "",
        "---",
        "",
        "## 这一轮要过的队列",
        "",
        json.dumps(batch, ensure_ascii=False, indent=1),
        "",
        "---",
        "",
        "## 输出格式（只输出这个 JSON，不要任何别的文字）",
        "",
        json.dumps({
            "escalate": [{"id": "…", "why": "一句话说清是什么事、谁要的、什么时候要",
                          "suggest": "你建议怎么办"}],
            "absorbed": ["…"],
        }, ensure_ascii=False, indent=1),
        "",
        "硬要求，任何一条不满足这一轮就作废：",
        f"- `escalate` 和 `absorbed` 里的 id **必须**来自这 {len(ids)} 个，不许出现别的；",
        "- 这些 id **每一个**都要恰好出现一次，在 escalate 或 absorbed 里，不许漏；",
        "- `why` 一句话，不要分点、不要小标题、不要把原始记录整段贴回来。",
    ])


def parse_report(text: str, batch_ids: list[str]) -> tuple[dict | None, str]:
    """解析模型输出。返回 `(报告, 失败原因)` —— 成功时原因是空串。

    **解析不出来就返回 None，绝不返回一个"尽力而为"的部分结果。**
    半个报告比没有报告更糟：下游没法知道少的那半是"判断为不用报"还是"根本没看"。
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        d = json.loads(raw)
    except ValueError as e:
        return None, f"输出不是 JSON：{e}"
    if not isinstance(d, dict):
        return None, "输出不是一个对象"

    esc = d.get("escalate")
    abs_ = d.get("absorbed")
    if not isinstance(esc, list) or not isinstance(abs_, list):
        return None, "escalate / absorbed 必须都是数组"

    out_esc = []
    for it in esc:
        if not isinstance(it, dict) or not it.get("id"):
            return None, "escalate 里有条目没有 id"
        out_esc.append({k: it.get(k) for k in ITEM_KEYS})
    out_abs = [str(x) for x in abs_]

    seen = [it["id"] for it in out_esc] + out_abs
    want = set(batch_ids)
    extra = [i for i in seen if i not in want]
    if extra:
        # 最典型的幻觉：编一个不存在的消息 id 出来。
        return None, f"编造了不存在的 id：{extra[:3]}"
    missing = sorted(want - set(seen))
    if missing:
        return None, f"漏了 {len(missing)} 条没给结论：{missing[:3]}"
    dup = len(seen) != len(set(seen))
    if dup:
        return None, "同一个 id 出现了多次"

    return {"escalate": out_esc, "absorbed": out_abs}, ""


def should_run(state: dict, at: dt.datetime, interval_minutes: int = INTERVAL_MINUTES
               ) -> tuple[bool, str]:
    """到点了吗。**纯函数。** 不到点也要给理由 —— 静默跳过看起来像"跑过了没事"。"""
    last = (state or {}).get("last_run")
    # ⚠️ 不能写 `if not last` —— `{}` / `0` / `[]` 都是假值，会被当成「没跑过」。
    # **「键不存在」和「有值但读不出」是两件事**：前者真的没跑过，后者是台账坏了。
    # 后者当成没跑过 → 每次都跑 → 一个坏时间戳让它每分钟叫一次模型。
    if last is None or last == "":
        return True, "还没跑过"
    try:
        prev = dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        # 读不出上次时间**不等于**没跑过。当成刚跑过，宁可晚一轮，
        # 也不要因为一个坏时间戳每分钟叫一次模型。
        return False, f"上次运行时间读不出（{last!r}），这轮跳过"
    gap = (at - prev).total_seconds() / 60
    if gap < interval_minutes:
        return False, f"上次 {gap:.0f} 分钟前，间隔 {interval_minutes} 分钟"
    return True, f"上次 {gap:.0f} 分钟前"


# ---------------------------------------------------------------- IO


def now() -> dt.datetime:
    return dt.datetime.now()


def read_state() -> dict:
    return fleet.load_json(STATE, {})


def write_state(d: dict) -> None:
    fleet.save_json(STATE, d)


def reported_ids() -> set:
    """所有报过的 id。**append-only ndjson**，不用 save_json —— 那个是
    原子替换但没有锁，两个写者会互相盖掉（outbox 一期踩过）。"""
    out = set()
    if not os.path.isfile(REPORTS):
        return out
    with open(REPORTS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            for it in r.get("escalate") or []:
                if it.get("id"):
                    out.add(it["id"])
            for i in r.get("absorbed") or []:
                out.add(i)
    return out


def append_report(rep: dict) -> None:
    with open(REPORTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rep, ensure_ascii=False) + "\n")


def role_and_triage() -> tuple[str, str]:
    """角色定义 + 本机巡检口径。本机那份存在就以它为准（`duty.md` 自己这么写的）。"""
    role = ""
    p = os.path.join(HERE, "roles", "duty.md")
    if os.path.isfile(p):
        role = open(p, encoding="utf-8").read()
    triage = ""
    for name in ("_local-TRIAGE.md", "TRIAGE.md"):
        q = os.path.join(HERE, name)
        if os.path.isfile(q):
            triage = open(q, encoding="utf-8").read()
            break
    return role, triage


def queue_items() -> list[dict]:
    """待处理队列。现在只有一个来源：点过名还没处理的。"""
    import dtwatch
    return [project(r) for r in dtwatch.stale_at_me(None)]


def ask_claude(prompt: str, timeout: int = TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """调 headless claude。**只读**：一个工具都不给它，它只需要看数据然后回 JSON。

    没有 API key 也能跑 —— 走 keychain 里的 OAuth（2026-08-28 验过）。
    """
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--allowedTools", ""]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"超过 {timeout}s 没返回"
    except OSError as e:
        return 127, "", str(e)


# ---------------------------------------------------------------- 命令


def push_to_outbox(escalate: list[dict], at: dt.datetime) -> int:
    """把上报的事送进 outbox，走 `kind="ask"`。返回送了几条。

    为什么不做成草稿：outbox 的草稿是**待发消息**（有收件人、有拟好的正文），
    而 duty 产出的是「要他一句判断」—— 没有收件人，`suggest` 是给他的建议不是
    给对方的回复。硬塞成草稿会让 console 那行「待他拍板的草稿 N 条」变成谎话。

    `duty.md` 的边界写着「不代他回任何消息」，所以这里**不拟回复稿**。

    落盘失败不抛 —— 报告已经写进 `duty_reports.ndjson` 了，推送失败不该让整轮作废。
    但**要报出去**（返回 -1），静默失败会让他以为"没有事要处理"。
    """
    if not escalate:
        return 0
    try:
        import dtwatch
        entries = dtwatch.read_outbox()          # 已经折叠过了
        fresh = []
        for it in escalate:
            # 编号要连着排，所以每次都把这一轮已经造出来的也算进去 ——
            # 只传 `entries` 的话三条 ask 会拿到同一个 id。
            fresh.append(dtwatch.make_ask(entries + fresh, at,
                                          about_text=it.get("why") or "",
                                          suggest=it.get("suggest") or "",
                                          about_id=it.get("id") or "", by="duty"))
        dtwatch.append_outbox(fresh)
        return len(fresh)
    except Exception as e:
        print(f"（送 outbox 失败：{type(e).__name__}: {e}）", file=sys.stderr)
        return -1


def notify_outbox():
    """把 outbox 里还没推给他的推到手机。返回推了几条，失败返回 -1。

    不自己拼推送文本、不自己发 —— 全走 `dtwatch.push_outbox`，那条是**唯一**
    的提醒通道，免打扰时段和最小间隔对它同样生效。被挡掉时它不标 notified，
    所以留着下次再推，不会静默丢掉。
    """
    try:
        import dtwatch
        # ⚠️ 必须传真配置。给 `{}` 的话免打扰时段和最小间隔全部失效 ——
        # 那两条约束正是「不因为是自动化就绕过」的那一条口径。
        cfg = dtwatch.load_json(dtwatch.CONFIG_PATH, None)
        if not cfg:
            print(f"（读不到 {dtwatch.CONFIG_PATH}，不推）", file=sys.stderr)
            return -1
        res = dtwatch.push_outbox(cfg, now())
        return res.get("pushed", 0) if res.get("ok") else -1
    except Exception as e:
        print(f"（推送失败：{type(e).__name__}: {e}）", file=sys.stderr)
        return -1


def cmd_run(args) -> int:
    os.makedirs(DATA, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"ok": False, "skipped": "上一轮还在跑"}, ensure_ascii=False))
        return 0

    at = now()
    state = read_state()
    if not args.force:
        go, why = should_run(state, at, args.interval)
        if not go:
            print(json.dumps({"ok": True, "skipped": why}, ensure_ascii=False))
            return 0

    items = queue_items()
    batch = pick_batch(items, reported_ids(), args.batch)
    res = {"ok": True, "at": at.strftime("%Y-%m-%d %H:%M:%S"),
           "queue": len(items), "batch": len(batch)}
    if not batch:
        res["skipped"] = "队列里没有没报过的"
        print(json.dumps(res, ensure_ascii=False))
        return 0

    role, triage = role_and_triage()
    prompt = build_prompt(role, triage, batch)
    res["prompt_chars"] = len(prompt)

    if args.dry_run:
        res["dry_run"] = True
        res["ids"] = [b["id"] for b in batch]
        print(json.dumps(res, ensure_ascii=False, indent=1))
        if args.verbose:
            print("\n" + "=" * 60 + "\n" + prompt)
        return 0

    rc, out, err = ask_claude(prompt, args.timeout)
    if rc != 0:
        res.update(ok=False, error=f"claude 退出 {rc}", stderr=err.strip()[:300])
        print(json.dumps(res, ensure_ascii=False))
        return 1

    rep, why = parse_report(out, [b["id"] for b in batch])
    if rep is None:
        # **不写报告。** 一个解析不出来的输出不许变成"这一轮没事"。
        res.update(ok=False, error=f"输出不合契约：{why}", raw_head=out.strip()[:200])
        print(json.dumps(res, ensure_ascii=False))
        return 1

    rep.update(at=res["at"], batch=[b["id"] for b in batch])
    append_report(rep)
    res["to_outbox"] = push_to_outbox(rep["escalate"], at)
    # 写进 outbox 还不够：`push_outbox` 全仓只有手敲 `outbox notify` 会调到，
    # 没有任何定时任务碰它 —— 又是「写了命令没人调」。duty 自己推一下。
    # 免打扰时段和最小间隔由 send_reminder 管，被挡掉就留着下次，不会丢。
    if res["to_outbox"] > 0:
        res["pushed"] = notify_outbox()
    state["last_run"] = res["at"]
    write_state(state)
    res.update(escalate=len(rep["escalate"]), absorbed=len(rep["absorbed"]))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    for it in rep["escalate"]:
        print(f"  ↑ {it.get('why')}   → {it.get('suggest')}")
    return 0


def cmd_show(args) -> int:
    if not os.path.isfile(REPORTS):
        print("还没有任何报告。")
        return 0
    with open(REPORTS, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    for r in rows[-args.n:]:
        print(f"{r.get('at')}  上报 {len(r.get('escalate') or [])} / "
              f"消化 {len(r.get('absorbed') or [])}")
        for it in r.get("escalate") or []:
            print(f"  ↑ {it.get('why')}\n    → {it.get('suggest')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="duty 值班派活器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="跑一轮")
    r.add_argument("--dry-run", action="store_true", help="只拼提示词，不调模型")
    r.add_argument("--verbose", "-v", action="store_true", help="dry-run 时打印提示词")
    r.add_argument("--force", action="store_true", help="忽略间隔")
    r.add_argument("--batch", type=int, default=BATCH, help=f"一轮几条（默认 {BATCH}）")
    r.add_argument("--interval", type=int, default=INTERVAL_MINUTES, help="间隔分钟")
    r.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS, help="等模型多少秒")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("show", help="看最近的报告")
    s.add_argument("-n", type=int, default=3)
    s.set_defaults(func=cmd_show)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
