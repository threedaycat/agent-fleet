#!/usr/bin/env python3
"""dtcc —— 用钉钉单聊遥控这台电脑上的 Claude Code。

出：Claude 干完一段活 / 卡住 / 要做选择时，往「我自己」的钉钉单聊推一条。
入：我在手机上回一条 `>xxx`，Claude 下一次收尾时把它当成新指令继续干。

这个通道跟 dtwatch 共用同一个自聊天窗口，但互不干扰：
dtwatch 的采集器明确跳过「自己发的消息」，所以这里的来回永远不会进它的待办队列。

会话里所有消息的 sender 都是我自己（脚本发的和手机打的一模一样），
所以靠「文本前缀」区分方向：脚本发出去的一律带 【CC…】/【dtwatch】 前缀，
手机发进来的指令一律以 > 开头。
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Protocol

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
DATA = os.path.join(HERE, "data", "cc")
STATE = os.path.join(DATA, "state.json")
LOG = os.path.join(DATA, "cc.log")
CLAIMS = os.path.join(DATA, "claims")   # 一条指令一个锁文件，防多会话重复执行
PUSHPID = os.path.join(DATA, "push.pid")   # push-loop 的 pid，由 remote on 写、off 清
PUSHLOG = os.path.join(DATA, "push.log")   # remote on 拉起来的那个 push-loop 的输出

# 脚本发出去的消息前缀。收指令时凡是以这些开头的一律跳过，
# 否则自己发的播报会被当成用户指令，来回自激。
OUT_MARKERS = ("【CC", "【dtwatch", "【dtcc")
# 手机上发指令的前缀，半角全角都收。
# `/` 也算 —— 他手机上习惯打 `/xxx`（跟 Claude Code 里的斜杠命令一个手感）。
# 注意：**现在不带前缀也算指令**（见 collect），这几个前缀留着只是让他能显式表达，
# 以及兼容以前的习惯。
CMD_PREFIX = (">", "＞", "》", "/")

# 便签前缀：贴表情表示收到，但**不派给任何会话、不唤醒任何人**。
# 2026-07-30 定的后路 —— 既然裸消息都当指令了，他真想记一笔时得有地方记，
# 而且照样要有回执，不能让他觉得石沉大海。
NOTE_PREFIX = (".", "。", "#", "＃")

DEFAULTS = {
    "broadcast_on_stop": True,     # 遥控窗口开着时，每轮收尾自动播报最后一段话
    "stop_wait_seconds": 0,        # 遥控窗口关着时收尾只探一次，不等（0=不等）
    "remote_wait_seconds": 600,    # 遥控模式下，收尾时最多挂着等多久
    "default_remote_minutes": 90,  # `remote on` 不带参数时的窗口
    "poll_interval": 6,            # 等指令的轮询间隔
    "broadcast_max_chars": 700,
    # ---- 多会话路由（tmux 里开一堆 pane，每个 pane 一个 Claude）----
    "route_stale_seconds": 240,    # 指令被指定给某个会话、但它一直不收尾，
                                   # 超过这个时间就让当前发言的会话兜底捡走
    "broadcast_ring": 40,          # 记住最近多少条播报，用来把引用回复认回原主
}


# ---------------------------------------------------------------- 基础设施

def now() -> dt.datetime:
    return dt.datetime.now()


def ts(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def logline(msg: str):
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts(now())} {msg}\n")


def cfg_load():
    cfg = load_json(CONFIG, {})
    cc = dict(DEFAULTS)
    cc.update(cfg.get("cc") or {})
    cfg["cc"] = cc
    return cfg


def dws(args: list[str], timeout: int = 60):
    """跑一条 dws，返回 (data, err)。永远不抛。"""
    cmd = ["dws", *args, "--format", "json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "dws not found in PATH"
    if not p.stdout.strip():
        return None, (p.stderr or "empty output").strip()[:300]
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None, p.stdout.strip()[:300]
    if isinstance(d, dict) and d.get("error"):
        return None, str(d["error"].get("message", d["error"]))[:300]
    return d, None


# ---------------------------------------------------------------- 通道

def self_conv_id(cfg) -> str:
    """自聊天的 openConversationId。registry 里有就用，没有就现查一次并缓存。"""
    state = load_json(STATE, {})
    if state.get("conv_id"):
        return state["conv_id"]
    reg = load_json(os.path.join(HERE, "data", "registry.json"), {})
    me = cfg["self"]["name"]
    for cid, v in reg.items():
        if v.get("name") == me and v.get("single"):
            state["conv_id"] = cid
            save_json(STATE, state)
            return cid
    d, err = dws(["chat", "+conversation-list", "--limit", "100"])
    if not err:
        for c in d.get("conversations", []):
            if c.get("conversationName") == me:
                state["conv_id"] = c["openConversationId"]
                save_json(STATE, state)
                return state["conv_id"]
    return ""


def send(cfg, text: str) -> tuple[bool, str]:
    _, err = dws(["chat", "message", "send",
                  "--open-dingtalk-id", cfg["self"]["open_dingtalk_id"],
                  "--text", text])
    if err:
        logline(f"[send] FAILED {err} :: {text[:80]}")
        return False, err
    logline(f"[send] {text[:160]}")
    return True, ""


def fetch(cfg, since: str) -> list[dict]:
    """自聊天里 since 之后的消息，时间升序。

    list_conversation_message_v2 会偶发 AUTH_PERMISSION_DENIED —— 不是配置问题，
    同样的参数隔几秒再打就成了。所以失败重试两次；真取不到就返回空，
    指令留在钉钉里下一轮再拿，不会丢。
    """
    cid = self_conv_id(cfg)
    if not cid:
        return []
    d = None
    for attempt in range(3):
        d, err = dws(["chat", "message", "list", "--group", cid,
                      "--time", since, "--direction", "newer", "--limit", "50"])
        if not err:
            break
        logline(f"[fetch] try{attempt + 1} err={err[:120]}")
        d = None
        if attempt < 2:
            time.sleep(3)
    if d is None:
        return []
    msgs = (d.get("result") or {}).get("messages") or []
    msgs.sort(key=lambda m: m.get("createTime", ""))
    return msgs


def is_outbound(text: str) -> bool:
    return text.lstrip().startswith(OUT_MARKERS)


def strip_prefix(text: str) -> tuple[bool, str]:
    """(是不是指令, 去掉前缀的正文)"""
    t = text.strip()
    for p in CMD_PREFIX:
        if t.startswith(p):
            return True, t[len(p):].strip()
    return False, t


def quoted_of(m: dict) -> dict:
    """他在手机上点「回复」引用的那条原文。

    这个字段是判断「这句话在答哪件事」的唯一依据 —— 同一个窗口里
    可能同时挂着好几件事的播报，光看 `content` 必然对错人。
    """
    q = m.get("quotedMessage") or {}
    text = (q.get("content") or "").strip()
    if not text:
        return {}
    return {"quoted_id": q.get("openMessageId", ""),
            "quoted_time": q.get("createTime", ""),
            "quoted_text": text}


def collect(cfg, since: str, accept_bare: bool = True) -> tuple[list[dict], str]:
    """读新指令。

    `accept_bare` **已经不起作用了**：2026-07-30 之后不带前缀的裸句子一律当指令，
    所以「要不要收裸句子」没得选。参数留着只是为了不动那几个调用点的签名。

    返回 (指令列表, 扫到的最新时间)。**这里不写游标** —— 多会话路由下，
    调用方可能把某条指令让给别的会话，游标必须停在那条之前，
    否则它的主人下次就再也拉不到了。
    """
    state = load_json(STATE, {})
    consumed = set(state.get("consumed", []))
    try:
        self_sends = dtwatch_mod().recent_self_sends()   # 循环外读一次，别每条都读盘
    except Exception:                                    # noqa: BLE001
        self_sends = []
    out = []
    newest = since
    for m in fetch(cfg, since):
        mid = m.get("openMessageId")
        t = (m.get("content") or "").strip()
        ctime = m.get("createTime", "")
        if ctime > newest:
            newest = ctime
        if not mid or mid in consumed or not t or is_outbound(t):
            continue
        # 我们自己推到自聊天的通知，8 秒后会在这里被读回来。不滤掉就会当成
        # 他打的指令派给某个会话并叫醒它——2026-08-28 实测三条推送都这样走了。
        # 按**记账**认（dtwatch.note_self_send 在发送出口记指纹），不按前缀名单认：
        # OUT_MARKERS 是黑名单，dtwatch 那边的【哨兵】/【时效】/【待你拍板】
        # 一个都不在里面，而且每加一种新通知就会再漏一次。
        try:
            dw = dtwatch_mod()
            if dw.is_self_echo(t, self_sends, now()):
                logline(f"[push] 自己的回声，不当指令 :: {t[:50]}")
                continue
        except Exception:                              # noqa: BLE001
            pass      # 问不到就按老行为走（当指令），不因为这层挂掉而丢他的话
        prefixed, body = strip_prefix(t)
        q = quoted_of(m)
        # 引用了 CC 的播报 = 明确在跟我说话
        replying_to_cc = bool(q) and is_outbound(q["quoted_text"])

        # 2026-07-30 定的：**裸句子也是指令。**
        # 以前不带前缀的一律跳过、当成"他写给自己的便签"，于是他打的绝大多数话
        # 一条都没人接（这就是他最大的那条抱怨的根源）。"便签"是我们自己想出来的
        # 概念，不是他的需求 —— 他往这个窗口打的每句话本来就是给主会话的。
        note_only = t.startswith(NOTE_PREFIX)
        if note_only:
            body = t[1:].strip()
        if not body:
            continue
        rec = {"id": mid, "time": ctime, "text": body,
               # 他有没有显式加前缀 / 引用播报。现在不影响「算不算指令」
               # （裸句子也算），只用来看他的表达方式。
               "prefixed": prefixed or replying_to_cc,
               # 便签：贴表情就完事，不派给任何人（push_once 里处理）
               "note_only": note_only}
        rec.update(q)
        out.append(rec)
    return out, newest


def advance_cursor(newest: str, held: list[str]) -> None:
    """写游标。held 是「这轮让给别的会话、没消费掉」的指令时间，
    游标必须停在最早那条之前，它的主人下次才拉得到。"""
    stop_at = newest
    if held:
        earliest = min(held)
        try:
            back = ts(parse_ts(earliest) - dt.timedelta(seconds=1))
            stop_at = min(stop_at, back)
        except ValueError:
            stop_at = min(stop_at, earliest)
    state = load_json(STATE, {})
    cur = state.get("cursor") or ""
    if stop_at and stop_at > cur:          # 只许前进，别被回退卡住
        state["cursor"] = stop_at
        save_json(STATE, state)


def claim(mid: str, sid: str) -> bool:
    """原子认领一条指令。抢到返回 True，被别人抢先返回 False。

    光靠 consume() 挡不住重复执行 —— 它是「读 state.json → 改 → 写回」，
    两个 pane 同一秒进来会各读到同一份 consumed、各自追加、后写的覆盖前写的。
    真这么撞过一次：两个会话同一秒吃掉了同一条指令。

    O_CREAT|O_EXCL 建文件在 POSIX 上是原子的，同一个 mid 只可能有一个赢家。
    路由规则决定「谁该拿」，这里保证「只有一个真拿到」。
    """
    os.makedirs(CLAIMS, exist_ok=True)
    p = os.path.join(CLAIMS, hashlib.sha1(mid.encode()).hexdigest())
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as e:
        logline(f"[claim] 建锁失败，退回先到先得: {e}")
        return True          # 锁机制本身坏了也别把通道锁死
    with os.fdopen(fd, "w") as f:
        f.write(f"{sid}\n{ts(now())}\n{mid}\n")
    prune_claims()
    return True


def unclaim(mid: str) -> None:
    """把认领退回去。

    push-loop 抢到一条指令、但接下来叫醒目标会话失败时必须退锁 ——
    否则这条指令既没人被叫醒去做，又因为锁在那儿使 hook-stop 的 pull
    路径也拿不到，直接消失。
    """
    p = os.path.join(CLAIMS, hashlib.sha1(mid.encode()).hexdigest())
    try:
        os.remove(p)
    except OSError:
        pass


def prune_claims(keep: int = 500) -> None:
    try:
        files = [os.path.join(CLAIMS, n) for n in os.listdir(CLAIMS)]
    except OSError:
        return
    if len(files) <= keep:
        return
    files.sort(key=lambda p: os.path.getmtime(p))
    for p in files[:len(files) - keep]:
        try:
            os.remove(p)
        except OSError:
            pass


def is_meta(body: str) -> bool:
    """是不是内建指令。认领之前得先知道，不然 handle_meta 一跑就把消息发出去了。"""
    low = body.lower().strip()
    return (low in ("help", "?", "？", "帮助", "用法",
                    "status", "状态", "在干什么", "在干嘛",
                    "who", "谁", "谁在听")
            or bool(META_REMOTE_RE.match(low)))


def consume(ids: list[str]):
    state = load_json(STATE, {})
    seen = state.get("consumed", [])
    seen.extend(i for i in ids if i not in seen)
    state["consumed"] = seen[-300:]
    save_json(STATE, state)


def cursor_now(cfg) -> str:
    """把游标推到「现在」，之前的历史消息一概不算新指令。"""
    state = load_json(STATE, {})
    stamp = ts(now() - dt.timedelta(seconds=2))
    state["cursor"] = stamp
    save_json(STATE, state)
    return stamp


def get_cursor(cfg) -> str:
    state = load_json(STATE, {})
    return state.get("cursor") or ts(now() - dt.timedelta(minutes=10))


# ---------------------------------------------------------------- 多会话路由
#
# 问题：Stop hook 配在 ~/.claude/settings.json 里，是全局的。tmux 里开 15 个 pane
# 就有 15 个 Claude，每个收尾时都来抢同一个 consumed 队列 —— 谁先轮到谁吃掉，
# 手机上完全看不出这条指令会落到谁手里。而且每个都按 600s 长轮询，
# 等于 15 路并发打同一个接口，日志里那些 AUTH_PERMISSION_DENIED 就是这么来的。
#
# 规则（不用他在手机上多打一个字）：
#   1. 播报带会话标签【CC·464f】，他一眼能看出是谁在说话
#   2. 点某条播报「回复」 → 只有发那条播报的会话能吃（认回原主）
#   3. 裸 > 指令       → 只有「最后说话的那个会话」能吃（他在回刚跟他说话的人）
#   4. 只有当前发言的会话长轮询等指令，其余会话收尾时只探一次就走
#      —— 这条同时把 15 路并发压回 1 路
#   5. 兜底：指定给某会话的指令超过 route_stale_seconds 还没人收，
#      当前发言的会话捡走，免得那个 pane 关掉了指令就永远卡住

def sid_of(payload: dict) -> str:
    """当前会话的 id。hook payload 里有就用，没有就从 transcript 文件名反推。"""
    sid = (payload.get("session_id") or "").strip()
    if sid:
        return sid
    tp = payload.get("transcript_path") or ""
    if tp:
        return os.path.basename(tp).rsplit(".", 1)[0]
    return ""


def sid_tag(sid: str) -> str:
    """给他看的短标签。uuid 第一段前 4 位，够区分又不占地方。"""
    return sid.split("-")[0][:4] if sid else "????"


def head_of(text: str) -> str:
    """播报正文的指纹。send 只拿到 openTaskId 拿不到 openMessageId，
    所以只能靠文本头去把引用回复认回原主。播报是自己发的，前缀匹配足够稳。"""
    return re.sub(r"\s+", " ", text.strip())[:80]


def note_broadcast(sid: str, text: str, cfg) -> None:
    """记下「这条播报是谁发的」，并把发言权交给它。"""
    state = load_json(STATE, {})
    ring = state.get("broadcasts", [])
    ring.append({"sid": sid, "head": head_of(text), "at": ts(now())})
    state["broadcasts"] = ring[-int(cfg.get("cc", {}).get("broadcast_ring", 40)):]
    state["speaker"] = {"sid": sid, "at": ts(now())}
    save_json(STATE, state)


def speaker_sid(max_age: float = 0) -> str:
    """最后播报的会话。max_age>0 时，太久没说话的当作不存在 ——
    那个 pane 八成已经被关掉了，不能让指令永远指着一个死会话。"""
    spk = load_json(STATE, {}).get("speaker") or {}
    sid = spk.get("sid", "")
    if sid and max_age > 0:
        try:
            if (now() - parse_ts(spk.get("at", ""))).total_seconds() > max_age:
                return ""
        except ValueError:
            return ""
    return sid


# ---------------------------------------------------------------- 路由的 IO 接缝
#
# 「这条指令归谁」要问两处**生产状态**：播报环（`data/cc/state.json`）和会话表
# （`fleet.sessions()`，真相在 `~/.claude/tmux-claude-status.json`）。判据直接
# 长在这两处上面就等于不可测 —— 跟屏幕抓取那层是同一个病，`tmux_probe.py` 已经
# 治过一次。照抄那边的两条：
#
#   A. **判据做成只吃数据的纯函数**（`match_*`），喂什么算什么，不猜、不读盘。
#   B. **IO 藏在 `RouteSource` 后面**，真假两个实现；测试只用假的，
#      所以永远不会读写 `data/`。
#
# 为什么必须是「双后端做全套」而不是「只给播报环做个内存版」：播报环走内存、
# 会话表还读真文件的话，测试之间就会共享本机 tmux 的真实状态 ——
# 今天开了几个会话，`named_session` 的结果就变几次。


class RouteSource(Protocol):
    """路由判据要用到的全部外部状态。真实现读盘，假实现吃内存。"""

    def broadcasts(self) -> list: ...
    def sessions(self) -> dict: ...


class LiveRoutes(object):
    """真实现。"""

    def broadcasts(self):
        return load_json(STATE, {}).get("broadcasts", []) or []

    def sessions(self):
        # 老行为：fleet 导不进来（比如 tmux 状态文件坏了）就当**一个会话都没有**，
        # 不抛给调用方 —— 抛了会把整条遥控通道打死。
        try:
            return fleet_mod().sessions() or {}
        except Exception:                          # noqa: BLE001
            return {}


class FakeRoutes(object):
    """假实现。**测试里唯一该用的东西**，不碰 `data/`、不碰 fleet、不碰 tmux。

    `broadcasts` 按时间顺序给（最新的在最后），跟 `note_broadcast` 往环里
    append 的顺序一致。
    """

    def __init__(self, broadcasts=None, sessions=None):
        self._broadcasts = list(broadcasts or [])
        self._sessions = dict(sessions or {})

    def broadcasts(self):
        return list(self._broadcasts)

    def sessions(self):
        return dict(self._sessions)


def _routes(source):
    return source if source is not None else LiveRoutes()


# ---------------------------------------------------------------- 纯判据

def match_broadcast(broadcasts, quoted_text: str) -> str:
    """在播报环里认出这条引用的主人。**纯函数。**认不出来返回空。

    从后往前找 —— 同一段话可能播报过多次，他引用的总是最近那条。
    """
    h = head_of(quoted_text)
    if not h:
        return ""
    for b in reversed(list(broadcasts)):
        bh = b.get("head", "")
        if not bh:
            continue
        # 钉钉引用会截断长消息，所以两边互为前缀都算命中
        if h.startswith(bh[:40]) or bh.startswith(h[:40]):
            return b.get("sid", "")
    return ""


def routable_sessions(sessions) -> dict:
    """只留「现在真能收件」的会话。**纯函数。**

    **判死这件事不在这里做。** 谁死了是 `fleet.build_sessions()` 拿
    `tmux list-panes` 的硬真相算出来的，结论写在记录的 `known` 字段里
    （`"gone"`）；这里只读那个结论。两处各自去问一遍 tmux 就是两份真相，
    而且等于给一个刚被证明可测的纯判据重新加上 IO。

    判据本身跟 `fleet.routable()` 是同一条，`test_entitlement.py` 里有一个
    用例专门盯着两边别走偏。
    """
    return {sid: r for sid, r in (sessions or {}).items()
            if r.get("pane") and r.get("known") != "gone"}


def match_named_session(session_ids, text: str) -> str:
    """他在这段文字里点名了哪个会话。**纯函数。**认不出返回空。

    判据本身见 `named_session` 的文档 —— 只认 4 位会话标签，且要独立成词。
    """
    if not text:
        return ""
    low = text.lower()
    for sid in session_ids:
        tag = sid[:4].lower()
        # 要独立成词才算：免得别的 uuid 片段或长串里恰好含这 4 个字符
        if re.search(rf"(?<![0-9a-z]){re.escape(tag)}(?![0-9a-z])", low):
            return sid
    return ""


def owner_of_quote(quoted_text: str, source=None) -> str:
    """他引用的那条播报是谁发的。认不出来返回空。"""
    return match_broadcast(_routes(source).broadcasts(), quoted_text)


def entitled(cfg, cmd: dict, sid: str, source=None) -> tuple[bool, str]:
    """这条指令该不该由我这个会话执行。

    跟 target_of 用同一套铁律（2026-07-30 定）：收件人默认永远是主会话，
    只有「他引用了某条播报」和「他文字里点名了某个会话」两种例外。
    **没有 speaker 规则，也没有 stale-rescue 转派** —— 超时了要去叫醒真正的
    主人（push_once 常驻在跑，它会叫），绝不把收件人悄悄换成别人。
    """
    if not sid:
        return True, "no-sid"          # 认不出自己，退回旧行为，不要把通道弄死
    source = _routes(source)
    target, why = target_of(cfg, cmd, source)
    if not target:
        return True, "unrouted"        # 没人可派，谁来谁接，别把通道弄死
    if target == sid:
        return True, f"mine({why})"
    if pane_of(target, source):
        return False, f"belongs-to-{sid_tag(target)}({why})"
    # 主人的 pane 真的没了（status.json 里查不到）才放开，
    # 否则这条指令会永远卡在一个不存在的会话名下。
    return True, f"owner-pane-gone({sid_tag(target)})"


# ---------------------------------------------------------------- 遥控窗口

def remote_active(cfg) -> tuple[bool, str]:
    until = load_json(STATE, {}).get("remote_until")
    if not until:
        return False, ""
    try:
        alive = parse_ts(until) > now()
    except ValueError:
        return False, ""
    return alive, until


def remote_extend(minutes: int) -> str:
    state = load_json(STATE, {})
    until = ts(now() + dt.timedelta(minutes=minutes))
    state["remote_until"] = until
    save_json(STATE, state)
    return until


def remote_close() -> None:
    state = load_json(STATE, {})
    state["remote_until"] = None
    save_json(STATE, state)


# ------------------------------------------------- push-loop 的生死跟着遥控窗口
#
# 改过一次：以前 push-loop 是 launchd 常驻进程（`<label-prefix>.push`），
# 自己读 state.json 门控 —— 窗口关着时它只是空转，不打接口，但**进程一直在**。
# 他不要这个：人在电脑前的时候，系统里根本不该有这个进程。
#
# 现在改成 remote 窗口的附属品：
#   `remote on`  → spawn 一个 push-loop（--managed），pid 记进 push.pid
#   `remote off` → 按 pid 杀掉
#   窗口自然到期 → --managed 的循环自己发现窗口关了就退出（见 cmd_push_loop）
# 三条路径合起来的效果：窗口开着才有进程，窗口一关（不管怎么关的）进程就没了。
#
# 找进程不只看 push.pid，而是 pgrep 全系统扫一遍 —— launchd 那份要是还挂着，
# 或者他手工 `run.sh push-loop` 起过一个，光看自己那个 pid 文件会漏，
# 结果是同时两个循环抢同一批指令。

def push_procs() -> list[int]:
    """系统里现在有哪些 push-loop 进程（不管谁拉起来的），不含自己。"""
    try:
        out = subprocess.run(["pgrep", "-f", r"dtcc\.py push-loop"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                                 # noqa: BLE001
        return []
    me = os.getpid()
    return [p for p in (int(x) for x in out.split() if x.isdigit()) if p != me]


def push_start(interval: int = 8) -> tuple[int, str]:
    """确保有一个 push-loop 在跑。返回 (pid, 'already' | 'spawned' | 'failed')。

    start_new_session=True 是关键：`remote on` 常常是从 Claude Code 的 Bash 工具里
    敲的，不脱离进程组的话，那次工具调用结束时子进程会被一起收走。
    """
    live = push_procs()
    if live:
        save_json(PUSHPID, live[0])
        return live[0], "already"
    cmd = [sys.executable, os.path.join(HERE, "dtcc.py"), "push-loop",
           "--interval", str(interval), "--managed"]
    try:
        with open(PUSHLOG, "a") as log:
            proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log,
                                    start_new_session=True)
    except Exception as e:                            # noqa: BLE001
        logline(f"[push] 拉起失败 {type(e).__name__}: {e}")
        return 0, "failed"
    save_json(PUSHPID, proc.pid)
    logline(f"[push] remote on → 拉起 push-loop pid={proc.pid} interval={interval}s")
    return proc.pid, "spawned"


def push_stop() -> list[int]:
    """杀掉所有 push-loop。返回真的被杀掉的 pid。

    判死活一律用 push_procs()（pgrep），不用 `os.kill(pid, 0)` —— 后者对**僵尸**
    进程也返回成功。`remote on` 和 `remote off` 在同一个进程里跑的时候
    （比如测试脚本、或将来谁 import 这个模块），被 SIGTERM 掉的子进程会先变成
    僵尸挂在父进程下面，用 kill(0) 探就会误判成"还活着、杀不掉"。
    """
    targets = set(push_procs())
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # 给它一点时间自己收尾，赖着不走的再 SIGKILL
    deadline = time.time() + 3
    while time.time() < deadline and targets & set(push_procs()):
        time.sleep(0.3)
    for pid in targets & set(push_procs()):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.3)
    if os.path.exists(PUSHPID):
        try:
            os.remove(PUSHPID)
        except OSError:
            pass
    stubborn = sorted(targets & set(push_procs()))
    killed = sorted(targets - set(stubborn))
    if targets:
        tail = f"（杀不掉的：{stubborn}）" if stubborn else ""
        logline(f"[push] remote off → 杀掉 push-loop {sorted(targets)}{tail}")
    return killed


def push_state_text() -> str:
    live = push_procs()
    if not live:
        return "push-loop：没在跑"
    return "push-loop：pid " + "、".join(str(p) for p in live)


# ---------------------------------------------------------------- 主动推送（push-loop）
#
# 为什么要有这一层：hook-stop 是 **pull** —— 只有某个会话恰好收尾时才会去问
# 「有我的指令吗」。所有会话都闲着的时候，压根没人去问。2026-07-30 00:00→10:00
# 十个小时手机上发的话没人接，就是这个原因（不是 hook 坏了，是没人来拉）。
#
# 所以再加一条 **push**：一个常驻小循环盯着自聊天，一有新指令就
#   ① 立刻回一句极短的文字回执（让他知道电脑收到了，不用干等）
#   ② 算出这条归哪个会话
#   ③ 那个会话闲着 → 立刻 fleet.py wake 把指令打进它的 pane
#      那个会话在跑  → 什么都不做，留给它自己收尾时 pull（不抢、不打断）
#
# 回执**就是贴表情**，而且是采到就贴、不等任何会话（见 push_once 开头那段）。
# 2026-07-30 最终口径：表情=可以，钉钉待办=禁止，另发文字只留给异常。
# （这里以前写的是"不用贴表情"，跟 react() 的文档直接打架 —— 那是旧口径，已作废。）

def react(cfg, mid: str, emoji: str = "OK") -> bool:
    """在他那条指令上贴个表情当「收到」。

    这是**唯一**允许的回执方式（2026-07-30 最终口径）：
      - **表情 = 可以。** 他自己平时就用「贴个 OK」代替回一句话，读起来是"看到了"。
      - **钉钉待办 = 禁止。** 绝对不许用 `dws todo` 之类的东西做回执或提醒 ——
        他明确反感被加进钉钉待办清单（原话「别给我待办清单，钉钉系统里的」）。
        这条没有例外，也不要为了"更醒目"去试。
      - 另发一条文字消息只留给**异常**（没人接 / 叫不醒），那不是回执而是报告问题。
    """
    cid = self_conv_id(cfg)
    if not cid:
        return False
    _, err = dws(["chat", "message", "add-emoji",
                  "--conversation-id", cid, "--msg-id", mid, "--emoji", emoji])
    if err:
        logline(f"[push] 贴表情失败 {err[:100]}")
        return False
    return True


def acked(mid: str) -> bool:
    """这条指令已经回过执了吗。push-loop 是单进程，读改写没有竞态。"""
    return mid in (load_json(STATE, {}).get("acked") or [])


def note_ack(mid: str) -> None:
    state = load_json(STATE, {})
    lst = state.get("acked") or []
    if mid not in lst:
        lst.append(mid)
    state["acked"] = lst[-300:]
    save_json(STATE, state)


def dtwatch_mod():
    """懒加载 dtwatch —— 只为了问一句「这条是不是我们自己刚推出去的」。

    两边互不 import（都懒加载 fleet），这里保持同一个形状，
    免得任何一边的模块级副作用被另一边拖进来。
    """
    if BASE not in sys.path:
        sys.path.insert(0, BASE)
    import dtwatch
    return dtwatch


def fleet_mod():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import fleet
    return fleet


def pane_of(sid: str, source=None) -> str:
    """这个会话在哪个 tmux pane。返回稳定的 `%NN`，查不到返回空。

    用 `%NN` 而不是 `session:window.index` —— 后者会随着窗口移动/改名变，
    fleet.py 里记着 2026-07-30 19:04 就是这么把任务打进了错误的 pane。
    `%NN` 是 tmux 给 pane 的终身 id，`send-keys -t %17` 永远指向同一个。
    """
    if not sid:
        return ""
    sess = routable_sessions(_routes(source).sessions())
    return (sess.get(sid) or {}).get("pane", "") or ""


def target_of(cfg, cmd: dict, source=None) -> tuple[str, str]:
    """这条手机指令归哪个会话。返回 (sid, 判断依据)。

    跟 entitled() 是同一套规则，只是这里要**主动算出目标**而不是回答
    「该不该由我拿」：
      1. 引用了某条播报 → 那条播报的主人（他明确在回它，署名是谁就归谁）
      2. 文字里点名了某个会话（4 位会话标签，如 `7ac5`）→ 那个会话
      3. 其余一律 → **主会话**

    铁律：他只跟主会话沟通，由主会话去调度别人 —— 他不会也不想指定收件人。
    所以默认收件人**永远是主会话**，不是「最后说话的那个」。
    「按上一个说话的会话」这条规则已经整个删掉：它跟消息内容毫无关系、纯靠运气，
    正是错派的病根（实测有两条自检消息就是这么被派给了不相干的项目会话）。
    """
    source = _routes(source)
    main = (cfg.get("cc") or {}).get("main_session") or ""
    quoted = cmd.get("quoted_text") or ""
    if quoted:
        owner = owner_of_quote(quoted, source)
        if owner:
            return owner, "quote"
    named = named_session(cfg, cmd.get("text") or "", source)
    if named:
        return named, "named"
    if main:
        return main, "default→main"
    logline("[push] config 里没配 cc.main_session，这条没人可派")
    return "", "unrouted"


def named_session(cfg, text: str, source=None) -> str:
    """他在文字里点名了某个会话吗。**只认 4 位会话标签**（如 `7ac5`）。

    收紧过一次：原来也认项目短名（「项目A 那边先停」→ 项目A 会话），
    结果他天天提项目名 —— 「项目A 那边怎么样」「项目B 推了吗」都是在跟主会话
    聊天，不是要派活给那个会话。项目名当收件人 = 他随口一提就被劫走，
    正是他最恼火的「派错人」的变种（实测「项目A 那个先别推」就被劫给了那个会话）。
    会话标签是他专门打出来指人的，无歧义；项目名不是。

    认不出返回空 —— 走默认（主会话），不猜。
    """
    if not text:                       # 老行为：文字为空就不去问会话表，省一次读盘
        return ""
    return match_named_session(routable_sessions(_routes(source).sessions()), text)


def push_seen(mid: str, sig: str) -> bool:
    """这条指令的处置结果跟上次完全一样吗。一样返回 True（调用方就别再吵了）。

    为什么必须有这层：push-loop 每 8 秒扫一遍，而**没被消费掉的指令每一拍都会
    再走一遍**（目标在忙、认不出归谁、pane 没了，都属于这种）。不做幂等的话：
      - 日志每 8 秒重复一行；
      - 更糟的是「叫不醒」那句异常提示**每 8 秒往他钉钉发一条**。
    后者是 2026-07-30 我把 react() 提到循环开头时引入的回归 —— 原先那句 send
    在 `if not acked(mid)` 里面，一挪出来就变成每拍都发。
    叫醒成功的那条会被 consume 掉，不走这里；claim() 另外保证跨进程不重复叫醒。
    """
    st = load_json(STATE, {})
    seen = st.get("push_seen") or {}
    if seen.get(mid) == sig:
        return True
    seen[mid] = sig
    if len(seen) > 300:                    # 只留最近的，别让 state 无限长
        for k in list(seen)[:len(seen) - 300]:
            seen.pop(k, None)
    st["push_seen"] = seen
    save_json(STATE, st)
    return False


def push_once(cfg, wake: bool = True) -> int:
    """扫一遍新指令，回执 + 该叫的叫醒。返回处理条数。"""
    since = get_cursor(cfg)
    cmds, newest = collect(cfg, since, accept_bare=False)
    held, done = [], 0
    for c in cmds:
        mid, text = c["id"], c["text"]

        # ★ 回执必须是**这个循环里的第一件事**，在路由、叫醒、内建指令之前。
        # 2026-07-30 19:xx 他在手机上明确抱怨「一条都没有及时回复，连个收到的
        # emoji 都没有」。根因有两个，这是第二个：以前 react 写在叫醒之后，
        # 而内建指令那条 `continue` 干脆走在 react 前面 —— 所以 `>remote 90`
        # 这类从来不贴表情。现在不管这条后面怎么走、走不走得通，先把「收到」贴上。
        # 这是纯机器动作：不判断内容、不等任何 Claude 会话。
        if not acked(mid):
            react(cfg, mid)
            note_ack(mid)

        # 便签（`.` / `#` 开头）：回执已经贴了，到此为止 —— 不派给任何会话、
        # 不唤醒任何人。直接消费掉，免得它一直躺在队列里等一个不会来的人。
        if c.get("note_only"):
            consume([mid])
            logline(f"[push] 便签（只回执不派活）:: {text[:60]}")
            continue

        # 内建指令（>status />remote />help）是全局设置，交给 hook-stop 那条路，
        # push 这边不碰 —— 它们会发消息，重复执行的观感很差
        if is_meta(text):
            held.append(c["time"])
            continue

        sid, why = target_of(cfg, c)
        # 会话实况（在哪个 pane、闲没闲）**问 fleet，别自己读 fleet.json** ——
        # 那个文件 07-30 之后只是薄 sidecar（project/note/last_wake），
        # 状态和 tmux 位置的真相在 ~/.claude/tmux-claude-status.json 里。
        try:
            rec = fleet_mod().sessions().get(sid) or {}
        except Exception:                          # noqa: BLE001
            rec = {}
        proj = rec.get("project") or ""
        state = rec.get("state", "")
        label = f"{proj}/{sid_tag(sid)}" if proj else sid_tag(sid)

        # note 只在「异常」时才发文字；正常情况一律只贴表情（见 react 的注释）
        note = ""
        if not sid:
            pass                      # 认不出归谁很常见，不值得发消息吵他，贴表情就够
        elif state == "busy":
            pass                      # 它收尾就会接，正常路径
        elif not wake:
            pass
        else:
            # 先抢锁再叫醒：叫醒成功了才算它的，失败要退锁，
            # 否则这条指令 push 没送到、pull 又被锁挡住 —— 两头都拿不到
            if not claim(mid, "push-loop"):
                if not push_seen(mid, "lost-race"):
                    logline(f"[push] lost-race :: {text[:60]}")
                held.append(c["time"])
                continue
            ok = wake_session(cfg, sid, c)
            if ok:
                consume([mid])
                done += 1
            else:
                unclaim(mid)
                held.append(c["time"])
                # 这条是真异常：他以为有人在做，其实那个 pane 已经没了
                note = f"{label} 叫不醒（pane 可能关了），等有会话收尾接。"

        # 没被 push 消费掉的，游标必须停在它前面，pull 那边才拉得到
        if mid not in (load_json(STATE, {}).get("consumed") or []):
            held.append(c["time"])
        # 处置结果没变就闭嘴 —— 不重复记日志，更不重复发那句异常提示。
        # 状态**变了**才再说一次（比如目标从 busy 变成 idle、或者 pane 真没了）
        if not push_seen(mid, f"{label}|{why}|{state}|{bool(note)}"):
            if note:                      # 只有异常才另发一句文字（回执早贴过了）
                send(cfg, f"【CC·{who_tag()}】{note}")
            logline(f"[push] target={label or '-'}({why}) state={state or '-'} "
                    f":: {text[:60]}")
    advance_cursor(newest, held)
    return done


def wake_session(cfg, sid: str, cmd: dict) -> bool:
    """把指令打进目标会话的 pane。

    走 fleet.wake 那条路（tmux send-keys），因为 Claude Code 没有
    「往运行中的会话推一条消息」的接口。**不加 --force** —— 正在输出的会话
    被打断比晚几分钟接到指令糟得多。
    """
    quoted = cmd.get("quoted_text") or ""
    task = "【钉钉手机指令】" + cmd["text"]
    if quoted:
        task += f"（他引用的是这条，别套到别的事上：{quoted[:160]}）"
    task = task.replace("\n", " ").strip()
    try:
        fleet = fleet_mod()
        rec = fleet.sessions().get(sid) or {}
        # 用稳定的 %NN，不用 `session:window.index`（窗口一动就指错，见 pane_of）
        target = rec.get("pane") or rec.get("tmux", "")
        if not target or not fleet.pane_alive(target):
            return False
        if rec.get("state") == "busy":
            return False
        code, _ = fleet.sh(["tmux", "send-keys", "-t", target, "-l", task])
        if code != 0:
            return False
        fleet.sh(["tmux", "send-keys", "-t", target, "Enter"])
        fleet.mark_wake(sid, cmd["text"])
        fleet.append_event({"who": "push-loop", "when": ts(now()),
                            "project": rec.get("project", ""),
                            "what": f"叫醒 {sid_tag(sid)}：{cmd['text'][:100]}",
                            "where": target})
        return True
    except Exception as e:                        # noqa: BLE001
        logline(f"[push] wake 失败 {type(e).__name__}: {e}")
        return False


def cmd_push_loop(cfg, args):
    """盯自聊天的常驻循环：采到就贴表情回执 + 叫醒该接的会话。

    **不再绑遥控窗口，永远轮询。** 2026-07-30 19:xx 改的，起因是他手机上抱怨
    「一条都没有及时回复，连个收到的 emoji 都没有」。根因第一条就在这里：
    之前这个循环只在 `remote on` 时才打接口，他在家/在桌前发的话根本没人采，
    回执和指令都得等某个会话恰好收尾（Stop hook）才被拉走 —— 他 19:02 那条
    「全都干了吧」就这么等了 886 秒。

    所以遥控窗口现在**只管 hook-stop 那边挂不挂着等**，不再管这条采集通道。
    这条通道只轮他自己那一个自聊天会话（`self_conv_id`），不碰任何群，
    8~10 秒一拍的代价可以接受 —— 换来的是回执秒级。

    注意别把 dtwatch 那条 300 秒的 poll 也改快，那条是拉**别人**消息的，
    量大得多，保持不动。

    间隔用墙钟判断，不用长 sleep —— macOS 睡眠会把 sleep 冻住（实测 `sleep 300`
    睡醒后跑到 37 分钟），那样这个循环就白搭了。
    """
    interval = max(5, int(args.interval))
    save_json(PUSHPID, os.getpid())
    logline(f"[push] loop start pid={os.getpid()} interval={interval}s（常轮询）")
    last = 0.0
    while True:
        wall = time.time()
        if wall - last >= interval:
            last = wall
            try:
                push_once(cfg, wake=not args.no_wake)
            except Exception as e:                  # noqa: BLE001
                logline(f"[push] once 异常（继续跑）{type(e).__name__}: {e}")
        time.sleep(min(2, interval))
        if args.once:
            return 0


# ---------------------------------------------------------------- 内建指令

META_HELP = """【CC】遥控台用法
两条路都算指令，满足一条就行：
  1) 点某条【CC】消息的「回复」，然后随便说 —— 不用加前缀
  2) 用 > 开头，例如 >继续 / >停，先别改了 / >第二个方案
裸发一句话、又没引用【CC】的，当成你写给自己的便签，不会执行。
引用别人的消息也不算，那种要加 > 。
多个 pane 同时开着 Claude 时，播报头上的【CC·xxxx】就是会话标签：
  点某条播报「回复」→ 一定回到发那条的会话手里
  裸 > 指令        → 交给最后跟你说话的那个会话
内建指令（整句就是它才算）：
  >status    现在在干什么
  >who       现在谁在听我的裸指令
  >remote 60 开遥控窗口 60 分钟（收尾时会挂着等你）
  >remote off 关掉遥控窗口
  >help      这条"""


# 内建指令必须「整句就是它」才算。用 startswith 会把正常说话吃掉 ——
# 引用播报回一句「遥控台挺好用的」不该变成开窗口指令，那句话得原样交给 Claude。
META_REMOTE_RE = re.compile(r"^(remote|遥控)\s*(on|off|开|关|关闭)?\s*(\d+)?\s*(分钟|min|m)?$")


def handle_meta(cfg, body: str) -> bool:
    """内建指令当场答掉，返回 True 表示不用交给 Claude。"""
    low = body.lower().strip()
    if low in ("help", "?", "？", "帮助", "用法"):
        send(cfg, META_HELP)
        return True
    if low in ("status", "状态", "在干什么", "在干嘛"):
        send(cfg, f"【CC】{status_text(cfg)}")
        return True
    if low in ("who", "谁", "谁在听"):
        send(cfg, f"【CC】{who_text(cfg)}")
        return True
    m = META_REMOTE_RE.match(low)
    if m:
        arg = f"{m.group(2) or ''}{m.group(3) or ''}".strip()
        if arg in ("off", "关", "关闭", "0"):
            remote_close()       # 只关「收尾挂着等」，不动常驻的回执通道
            send(cfg, "【CC】遥控窗口已关闭。收尾时不再挂着等你，但消息照旧秒回执。")
            return True
        try:
            mins = int(re.sub(r"[^0-9]", "", arg) or cfg["cc"]["default_remote_minutes"])
        except ValueError:
            mins = cfg["cc"]["default_remote_minutes"]
        until = remote_extend(mins)
        push_start()             # 手机上开的窗口同样要有人盯着，不然只剩 pull 那条路
        send(cfg, f"【CC】遥控窗口开到 {until}。这段时间我每轮干完会挂着等你的指令。")
        return True
    return False


def who_text(cfg) -> str:
    """现在的指令会落到谁手里。"""
    state = load_json(STATE, {})
    main = (cfg.get("cc") or {}).get("main_session") or ""
    bits = []
    if main:
        proj = ""
        try:
            proj = (fleet_mod().sessions().get(main) or {}).get("project") or ""
        except Exception:                          # noqa: BLE001
            pass
        where = f"（{proj}，pane {pane_of(main) or '?'}）" if proj else ""
        bits.append(f"默认收件人 → 主会话 {sid_tag(main)}{where}")
    else:
        bits.append("⚠️ config 里没配 cc.main_session，默认收件人是空的")
    bits.append("例外只有两种：①点某条播报的「回复」→ 归发那条的会话；"
                "②话里点名（会话标签或项目名）→ 归它")
    recent, seen = [], set()
    for b in reversed(state.get("broadcasts", [])):
        t = sid_tag(b.get("sid", ""))
        if t not in seen:
            seen.add(t)
            recent.append(f"{t}@{(b.get('at') or '')[11:16]}")
        if len(recent) >= 6:
            break
    if recent:
        bits.append("最近播报过的：" + "、".join(recent))
    return "\n".join(bits)


def status_text(cfg) -> str:
    state = load_json(STATE, {})
    alive, until = remote_active(cfg)
    bits = []
    last = state.get("last_broadcast_at")
    if last:
        bits.append(f"上次播报 {last}")
    if state.get("last_broadcast"):
        bits.append(f"当时在做：{state['last_broadcast'][:200]}")
    bits.append(f"遥控窗口：{'开到 ' + until if alive else '关'}")
    bits.append(push_state_text())
    q = state.get("pending_question")
    if q:
        bits.append(f"有个问题在等你答：{q[:160]}")
    return " / ".join(bits) or "没有记录"


# ---------------------------------------------------------------- 等待

def wait_for_command(cfg, timeout: int, accept_bare: bool,
                     since=None, sid: str = ""):
    """轮询直到收到一条指令，或超时。内建指令当场消化掉、继续等。

    sid 非空时按「多会话路由」过滤：不归我的指令一律不碰 —— 不消费、不返回，
    让它留在钉钉里等它真正的主人。
    """
    interval = max(2, int(cfg["cc"]["poll_interval"]))
    since = since or get_cursor(cfg)
    deadline = time.monotonic() + max(0, timeout)
    first = True
    while first or time.monotonic() < deadline:
        first = False
        cmds, newest = collect(cfg, since, accept_bare)
        real, held = [], []
        for c in cmds:
            # 便签只该被 push-loop 贴个表情然后归档，任何会话都不该把它当活捡走。
            # push-loop 常驻在跑，正常情况轮不到这里；这是兜底。
            if c.get("note_only"):
                if claim(c["id"], sid):
                    consume([c["id"]])
                    logline(f"[route] 便签，不当活 :: {c['text'][:60]}")
                continue
            # 内建指令（>status / >remote / >help / >who）是全局设置，
            # 不参与路由，谁先认领到谁答
            if is_meta(c["text"]):
                if claim(c["id"], sid):
                    handle_meta(cfg, c["text"])
                    consume([c["id"]])
                continue
            ok, why = entitled(cfg, c, sid)
            if not ok:
                logline(f"[route] skip({why}) sid={sid_tag(sid)} :: {c['text'][:60]}")
                held.append(c["time"])
                continue
            # 够格 ≠ 拿得到：冷启动、或超时兜底时可能有两个会话同时够格，
            # 这一步保证只有一个真执行
            if not claim(c["id"], sid):
                logline(f"[route] lost-race sid={sid_tag(sid)} :: {c['text'][:60]}")
                continue
            logline(f"[route] take({why}) sid={sid_tag(sid)} :: {c['text'][:60]}")
            real.append(c)
        advance_cursor(newest, held)
        if real:
            consume([c["id"] for c in real])
            # 多条连发合成一条，但保留最后一条的引用上下文；
            # 如果只有前面某条带引用，也别丢，挂到 merged 上
            merged = dict(real[-1])
            if len(real) > 1:
                merged["text"] = "\n".join(c["text"] for c in real)
                if not merged.get("quoted_text"):
                    for c in reversed(real[:-1]):
                        if c.get("quoted_text"):
                            merged.update({k: c[k] for k in
                                           ("quoted_id", "quoted_time", "quoted_text")})
                            break
            return merged
        if time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(1, deadline - time.monotonic())))
    return None


# ---------------------------------------------------------------- transcript

def last_assistant_text(transcript_path: str, max_chars: int) -> str:
    """从 transcript 里抠出我最后说给人看的那段话，当播报正文。"""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines[-400:]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content") or []
        if isinstance(content, str):
            chunks = [content]
        else:
            chunks = [c.get("text", "") for c in content
                      if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(c for c in chunks if c.strip()).strip()
        if text:
            return text[:max_chars]
    return ""


# ---------------------------------------------------------------- 命令

def who_tag(sid: str = "") -> str:
    """发消息时署名：`项目/会话短标签`，例如 `agent-fleet/464f`。

    手机上同一个自聊天窗口里挤着好几个会话在说话，只有 4 位 uuid 认不出来是谁；
    带上项目名他一眼就知道该往哪想。项目名优先取 fleet 心跳里记的（那是会话自己报的），
    取不到就退回当前目录名。
    """
    sid = sid or (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    proj = ""
    try:
        import fleet as _f
        proj = ((_f.sessions().get(sid) or {}).get("project") or "")
        if not proj:
            # 这个会话还没报过心跳（比如刚开就 say）—— 直接按 config 的映射算
            if HERE not in sys.path:
                sys.path.insert(0, HERE)
            import fleet
            proj = fleet.project_of(os.getcwd())
    except Exception:                            # noqa: BLE001
        proj = ""
    proj = proj or os.path.basename(os.getcwd())
    tag = sid_tag(sid)
    return f"{proj}/{tag}" if proj else tag


def cmd_say(cfg, args):
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    text = (args.text if args.text.lstrip().startswith(OUT_MARKERS)
            else f"【CC·{who_tag(sid)}】{args.text}")
    ok, err = send(cfg, text)
    if ok and sid:
        # 必须登记进播报环 —— 否则他在手机上「引用这条」回复时，owner_of_quote
        # 认不出主人，指令退化成「先到先得」，落到别的会话手里。
        # 真出过：某个项目会话用 say 推了它那几件待定，他引用它回了一句，
        # 结果指令投给了另一个完全不相干的会话。
        # hook-stop 的播报一直有登记，只有 say 这条路漏了，所以「窗口关着时用 say
        # 主动推」的消息全都认不回主人。
        note_broadcast(sid, text, cfg)
    print(json.dumps({"ok": ok, "error": err or None, "sid": sid_tag(sid)},
                     ensure_ascii=False))
    return 0 if ok else 1


def cmd_ask(cfg, args):
    lines = [f"【CC?·{who_tag()}】{args.question}"]
    opts = [o.strip() for o in (args.options or "").split("|") if o.strip()]
    for i, o in enumerate(opts, 1):
        lines.append(f"{i}. {o}")
    lines.append("——直接回复即可（回数字也行）")
    since = cursor_now(cfg)
    state = load_json(STATE, {})
    state["pending_question"] = args.question
    save_json(STATE, state)
    ok, err = send(cfg, "\n".join(lines))
    if not ok:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1
    got = wait_for_command(cfg, args.timeout, accept_bare=True, since=since)
    state = load_json(STATE, {})
    state["pending_question"] = None
    save_json(STATE, state)
    if not got:
        print(json.dumps({"ok": True, "timed_out": True, "answer": None,
                          "waited_s": args.timeout}, ensure_ascii=False))
        return 0
    answer = got["text"]
    picked = None
    if opts and re.fullmatch(r"[0-9]+", answer.strip()):
        idx = int(answer.strip())
        if 1 <= idx <= len(opts):
            picked = opts[idx - 1]
    res = {"ok": True, "timed_out": False, "answer": answer,
           "option": picked, "at": got["time"]}
    quoted = got.get("quoted_text")
    if quoted:
        res["quoted"] = quoted[:400]
        # 他可能是去回一条更早的播报，而不是我刚问的这个问题 —— 这种时候
        # 别把答案硬套到当前问题上，上层得自己判断
        if args.question[:16] not in quoted:
            res["warning"] = "这条回复引用的不是刚问的那个问题，看 quoted 判断它在答哪件事"
    logline(f"[ask] Q={args.question[:60]} A={answer[:80]}"
            + (f" (quoted: {quoted[:60]})" if quoted else ""))
    print(json.dumps(res, ensure_ascii=False))
    return 0


def cmd_wait(cfg, args):
    since = get_cursor(cfg) if args.include_queued else cursor_now(cfg)
    got = wait_for_command(cfg, args.timeout, accept_bare=args.any, since=since)
    if not got:
        print(json.dumps({"ok": True, "timed_out": True, "command": None},
                         ensure_ascii=False))
        return 0
    logline(f"[wait] {got['text'][:100]}")
    out = {"ok": True, "timed_out": False,
           "command": got["text"], "at": got["time"]}
    if got.get("quoted_text"):
        out["quoted"] = got["quoted_text"][:400]
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_inbox(cfg, args):
    cmds, newest = collect(cfg, get_cursor(cfg), accept_bare=args.any)
    real = []
    for c in cmds:
        if handle_meta(cfg, c["text"]):
            consume([c["id"]])
            continue
        c["routed_to"] = sid_tag(target_of(cfg, c)[0])
        real.append(c)
    if real and not args.peek:
        consume([c["id"] for c in real])
        advance_cursor(newest, [])
    print(json.dumps({"ok": True, "count": len(real), "commands": real},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_remote(cfg, args):
    """遥控窗口开关。push-loop 的生死挂在这上面 —— 见 push_start/push_stop 上面那段。"""
    if args.action == "off":
        # **不杀 push-loop。** 它现在是常驻的回执通道（采到就贴表情），
        # 跟遥控窗口没关系了；关窗口只是「收尾时不再挂着等我」。
        # 2026-07-30 19:xx 之前这里会 push_stop()，那会把秒回执一起关掉。
        remote_close()
        send(cfg, "【CC】遥控窗口已关闭。自聊天照旧秒回执。")
        print(json.dumps({"ok": True, "remote": False,
                          "push_pids": push_procs()}, ensure_ascii=False))
        return 0
    if args.action == "status":
        alive, until = remote_active(cfg)
        print(json.dumps({"ok": True, "remote": alive, "until": until or None,
                          "push_pids": push_procs(), "status": status_text(cfg)},
                         ensure_ascii=False))
        return 0
    mins = args.minutes or cfg["cc"]["default_remote_minutes"]
    until = remote_extend(mins)          # 先写窗口再拉进程，否则它一起来就以为窗口是关的
    pid, how = push_start(int(args.interval))
    send(cfg, f"【CC】遥控窗口开到 {until}。我每轮干完会挂着等你的指令，"
              f"手机上发 >继续 / >停 / >换个思路 都行，>help 看用法。")
    print(json.dumps({"ok": True, "remote": True, "until": until,
                      "push_pid": pid, "push": how}, ensure_ascii=False))
    return 0


def cmd_tail(cfg, args):
    msgs = fetch(cfg, ts(now() - dt.timedelta(hours=args.hours)))
    for m in msgs[-args.limit:]:
        who = "CC " if is_outbound(m.get("content") or "") else "我  "
        text = (m.get("content") or "").replace("\n", " ⏎ ")
        print(f"{m.get('createTime','')} {who} {text[:200]}")
        q = quoted_of(m)
        if q:
            qt = q["quoted_text"].replace("\n", " ⏎ ")
            print(f"{' ' * 19}     ↳ 引用 {q['quoted_time']}：{qt[:120]}")
    return 0


# ---------------------------------------------------------------- hooks

def cmd_hook_stop(cfg, args):
    """Stop hook：一轮活干完时被调用。

    遥控窗口开着 → 播报这一轮说了什么，然后挂着等手机指令。
    遥控窗口关着 → 一句话都不发，只静默探一次队列（不然手机上发的
                   >remote 永远没人接，遥控模式就没法从手机打开）。
    收到指令 → decision=block，Claude 带着这条指令继续干。
    """
    raw = sys.stdin.read()
    payload = load_json_str(raw)
    sid = sid_of(payload)
    # 事件落盘排第一，早于任何可能崩溃或卡住的逻辑（remote_active 读状态、
    # push_procs/push_start 拉子进程、下面的长轮询）。beat_fleet 内部整段包在
    # try 里，绝不会因为报心跳本身失败而挡住收尾——但那挡不住的是*它前面*
    # 的代码炸了。2026-07-28 实测过一次 push_start 附近 27ms 就 exit=1 的
    # 崩溃，那一轮收尾当时在 fleet 里彻底没留痕。心跳/事件先报，慢的和可能崩
    # 的部分放后面，就算后面那段真的炸了或被超时杀掉，这一轮也已经落过账了。
    #
    # （另一桩历史事故——某会话被注入后真干了 1 分半，events.ndjson 却整整
    # 16 分钟没心跳——根因是下面 stop_hook_active 分支当时直接 return、没报
    # 心跳，跟这里无关，早已修过。）
    beat_fleet(sid, payload)
    if payload.get("stop_hook_active"):
        # 已经是被 hook 续过一轮了，先放它停，避免来回顶着不落地。
        # 心跳已经在上面报过了，这里不用再报一次。
        logline(f"[hook-stop] sid={sid_tag(sid)} stop_hook_active=1 "
                f"（上一轮注入过活）→ 报心跳后放它停")
        return 0

    alive, _ = remote_active(cfg)
    # 自愈：窗口开着但盯自聊天的进程没了（崩了、或者他手工 kill 过），这里补拉一个。
    # push-loop 不再是 launchd 常驻进程，没人帮它重起了，这是唯一的兜底。
    if alive and not push_procs():
        push_start()
    state = load_json(STATE, {})
    spoke = False
    if alive and cfg["cc"]["broadcast_on_stop"]:
        text = last_assistant_text(payload.get("transcript_path", ""),
                                   int(cfg["cc"]["broadcast_max_chars"]))
        if text and text != state.get("last_broadcast"):
            body = f"【CC·{who_tag(sid)}】{text}\n——直接点这条「回复」说下一步就行"
            ok, _ = send(cfg, body)
            if ok:
                note_broadcast(sid, body, cfg)   # 发言权归我，引用回复也认得回来
                spoke = True
            state = load_json(STATE, {})
            state["last_broadcast"] = text
            state["last_broadcast_at"] = ts(now())
            save_json(STATE, state)

    # 只有**主会话**（和刚播报过的那个）挂着长轮询。其余会话收尾时只探一次就走 ——
    # 否则 15 个 pane 各等 600 秒、每 6 秒打一次接口，就是日志里那堆 PERMISSION_DENIED。
    # 挂长轮询的应该是默认收件人，跟着 target_of 的铁律走，不再用 speaker。
    main_sid = (cfg.get("cc") or {}).get("main_session") or ""
    mine = spoke or (sid and sid == main_sid)
    if alive and mine:
        timeout = int(cfg["cc"]["remote_wait_seconds"])
    elif alive:
        timeout = 0
    else:
        timeout = int(cfg["cc"]["stop_wait_seconds"])
    t0 = time.monotonic()
    got = wait_for_command(cfg, timeout, accept_bare=False, sid=sid)
    waited = round(time.monotonic() - t0)
    # 记下真等了多久：hook 被 harness 提前掐断的话，这里能看出来
    logline(f"[hook-stop] sid={sid_tag(sid)} remote={alive} mine={bool(mine)} "
            f"budget={timeout}s waited={waited}s got={'yes' if got else 'no'}")
    if not got:
        # 他没发指令，但可能有按 route 表派给本会话的钉钉消息 —— 直接接着干
        work = routed_work(sid)
        if work:
            lines = [f"- [{w['time']}] {w['conv']}｜{w['sender']}：{w['text'][:220]}"
                     for w in work[:12]]
            reason = (
                "钉钉来了归本项目的新消息（dtwatch 按 config.json 的 route 表派给这个会话，"
                f"不是发给哨兵会话的）。按 {os.path.join(HERE, 'TRIAGE.md')} 处理，"
                f"处理完用 `python3 {os.path.join(HERE, 'dtwatch.py')} mark <id...> "
                "--status done|ignored|snoozed --note \"...\"` 回写：\n"
                + "\n".join(lines)
                + "\n\nid：" + " ".join(w["id"] for w in work[:12])
            )
            logline(f"[routed] 注入 sid={sid_tag(sid)} {len(work)} 条")
            print(json.dumps({"decision": "block", "reason": reason},
                             ensure_ascii=False))
        return 0
    quoted = got.get("quoted_text")
    ctx = ""
    if quoted:
        # 他在手机上是「点某条播报的回复按钮」来说话的 —— 不带上被引用的原文，
        # 同一个窗口里挂着好几件事时必然对错人
        ctx = ("\n（他在手机上引用的是你之前这条播报，说明这句话是在回它，"
               f"别套到别的事上去）：\n{quoted[:400]}")
    reason = ("我从钉钉手机端发来一条新指令，按它继续做，做完照常收尾："
              f"\n{got['text']}{ctx}")
    print(json.dumps({"decision": "block", "reason": reason},
                     ensure_ascii=False))
    logline(f"[hook-stop] inject: {got['text'][:120]}")
    return 0


def beat_fleet(sid: str, payload: dict):
    """收尾时报一句心跳：我是谁、在哪个 tmux、刚干完什么、现在闲着。

    多个会话之间「谁在忙、谁闲着、谁的 pane 已经没了」没有别的地方能知道 ——
    只有各自收尾的这一刻自己报。`fleet.py wake` 要靠这张表才知道往哪个 pane
    打字，以及那个会话现在能不能被打断。

    心跳丢一次无所谓，所以整段包在 try 里：**绝不能因为报心跳失败挡住收尾**。
    """
    if not sid:
        return
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import fleet
        note = last_assistant_text(payload.get("transcript_path", ""), 100)
        fleet.beat(sid, state="idle", note=(note or "").replace("\n", " "))
    except Exception as e:                      # noqa: BLE001
        logline(f"[fleet] beat 失败（不影响收尾）：{type(e).__name__}: {e}")


def routed_work(sid: str) -> list[dict]:
    """问 dtwatch：有没有按 route 表派给本会话的新钉钉消息。

    这是「让对应项目的 Claude 直接收到对应的消息」那条路 ——
    只谈某一个项目的那位同事，他的消息就该落到那个项目的会话手里接着干，
    而不是全堆到哨兵会话里由它转述一遍。
    注意：`for-session` 会记账，所以只在**确定要注入**的那一刻才调它。
    """
    if not sid:
        return []
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "dtwatch.py"),
                            "for-session", sid],
                           capture_output=True, text=True, timeout=90)
    except Exception as e:                       # noqa: BLE001
        logline(f"[routed] 调 dtwatch 失败: {e}")
        return []
    out = []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def cmd_hook_notify(cfg, args):
    """Notification hook：Claude 要授权 / 空等我输入时，捅一下手机。

    只在遥控窗口开着时推（或 config 里 cc.notify_always=true）——
    人就坐在电脑前的时候，终端里已经有提示了，手机再响一遍纯属吵。
    """
    payload = load_json_str(sys.stdin.read())
    msg = (payload.get("message") or "").strip()
    if not msg:
        return 0
    alive, _ = remote_active(cfg)
    if not alive and not cfg["cc"].get("notify_always"):
        return 0
    state = load_json(STATE, {})
    last = state.get("last_notify")
    last_at = state.get("last_notify_at")
    if last == msg and last_at and (now() - parse_ts(last_at)).total_seconds() < 300:
        return 0
    # 这条也要带标签并登记进环 —— 否则他点它「回复」时认不出是哪个会话在等，
    # 只能退回「谁最后说话谁接」，多 pane 下就又不确定了
    sid = sid_of(payload)
    body = f"【CC·{who_tag(sid)}】{msg}\n——电脑那边在等我，点这条「回复」就能直接指挥"
    ok, _ = send(cfg, body)
    if ok:
        note_broadcast(sid, body, cfg)
    state = load_json(STATE, {})
    state["last_notify"], state["last_notify_at"] = msg, ts(now())
    save_json(STATE, state)
    return 0


def load_json_str(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="dtcc", description="钉钉遥控 Claude Code")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("say", help="往钉钉推一条进度")
    p.add_argument("text")
    p.set_defaults(fn=cmd_say)

    p = sub.add_parser("push-loop",
                       help="盯自聊天，新指令即刻回执并叫醒目标会话"
                            "（正常由 remote on 拉起，不用手敲）")
    p.add_argument("--interval", type=int, default=8, help="轮询间隔秒（默认 8）")
    p.add_argument("--once", action="store_true", help="只扫一轮就退出（测试用）")
    p.add_argument("--no-wake", action="store_true",
                   help="只回执不叫醒（看路由判断对不对）")
    p.add_argument("--managed", action="store_true",
                   help="（已废弃，保留只为兼容老的 launchd plist / 命令行）")
    p.set_defaults(fn=cmd_push_loop)

    p = sub.add_parser("push-stop", help="停掉常驻的自聊天轮询（回执会跟着停）")
    p.set_defaults(fn=lambda cfg, args: (
        print(json.dumps({"ok": True, "killed": push_stop()},
                         ensure_ascii=False)) or 0))

    p = sub.add_parser("ask", help="问一个问题并阻塞等答复")
    p.add_argument("question")
    p.add_argument("--options", help="候选项，用 | 分隔")
    p.add_argument("--timeout", type=int, default=540, help="最多等多少秒")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("wait", help="阻塞等一条指令")
    p.add_argument("--timeout", type=int, default=540)
    p.add_argument("--any", action="store_true",
                   help="（已无作用：裸句子现在一律当指令）")
    p.add_argument("--include-queued", action="store_true",
                   help="连之前攒下没读的一起算")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("inbox", help="取走新指令，不阻塞")
    p.add_argument("--any", action="store_true",
                   help="（已无作用：裸句子现在一律当指令）")
    p.add_argument("--peek", action="store_true", help="看一眼但不标记已读")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("remote", help="遥控窗口开关")
    p.add_argument("action", nargs="?", default="status",
                   choices=["on", "off", "status"])
    p.add_argument("minutes", nargs="?", type=int)
    p.add_argument("--interval", type=int, default=8,
                   help="on 时拉起的 push-loop 的轮询间隔秒（默认 8）")
    p.set_defaults(fn=cmd_remote)

    p = sub.add_parser("tail", help="看最近的来回")
    p.add_argument("--hours", type=int, default=6)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("hook-stop", help="给 Stop hook 用")
    p.set_defaults(fn=cmd_hook_stop)

    p = sub.add_parser("hook-notify", help="给 Notification hook 用")
    p.set_defaults(fn=cmd_hook_notify)

    args = ap.parse_args()
    cfg = cfg_load()
    if not (cfg.get("self") or {}).get("open_dingtalk_id"):
        print(json.dumps({"ok": False, "error": "config.json 缺 self.open_dingtalk_id"},
                         ensure_ascii=False))
        return 1
    os.makedirs(DATA, exist_ok=True)
    return args.fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
