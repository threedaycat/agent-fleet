#!/usr/bin/env python3
"""dtwatch —— 钉钉消息哨兵。

只干两件事，且只做规则层，不做判断：
  1. 采集：把「别人私聊我」和「关注群里的消息」增量抓下来，归一化成一条条记录。
  2. 打标：用关键词/发送人/会话类型这些死规则给每条记录打个 level，攒成待办队列。

真正的「看懂在说什么、要不要拉更多上下文、要不要提醒」交给 Claude，
它读 `pending` 的输出，然后用 `mark` 回写结论。

子命令：
  init      建立会话注册表，并把所有游标对齐到「现在」（首次装机跑一次）
  poll      跑一轮增量采集，输出本轮摘要 JSON
  pending   打印还没处理的记录（Claude 的输入）
  show      打印某条记录 + 它在会话里的上下文
  mark      把记录标成 done / ignored / snoozed，可带备注
  remind    往自己的钉钉单聊发一条提醒（受免打扰时段和最小间隔约束）
  status    看一眼采集器自身状态
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CONFIG_PATH = os.path.join(BASE, "config.json")

REGISTRY = os.path.join(DATA, "registry.json")
STATE = os.path.join(DATA, "state.json")
INBOX = os.path.join(DATA, "inbox.ndjson")
TRIAGE = os.path.join(DATA, "triage.json")
INJECTED = os.path.join(DATA, "injected.json")   # 已投递给某个会话的条目，防重复注入
AT_EVENTS = os.path.join(DATA, "at_events.ndjson")
RUNLOG = os.path.join(DATA, "run.log")

TS_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- 基础设施

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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def logline(msg: str):
    os.makedirs(DATA, exist_ok=True)
    with open(RUNLOG, "a", encoding="utf-8") as f:
        f.write(f"{ts(now())} {msg}\n")


def dws(args: list[str], timeout: int = 60):
    """跑一条 dws 命令，返回 (data, err)。永远不抛异常。"""
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


# ---------------------------------------------------------------- 会话

def conversation_list(cfg):
    """按最近活跃度排序的全部会话。dws 返回就是这个顺序，采集靠它做前缀扫描。"""
    d, err = dws(["chat", "+conversation-list", "--limit", "100"])
    if err:
        return [], err
    return d.get("conversations", []), None


def conv_is_single(cid: str, registry: dict) -> bool:
    """会话是不是单聊。查一次缓存一次，注册表里没有才打接口。"""
    hit = registry.get(cid)
    if hit and "single" in hit:
        return hit["single"]
    d, err = dws(["chat", "+conversation-info", "--group", cid])
    single = False
    if not err:
        info = (d.get("result") or {}).get("conversationInfo") or {}
        single = bool(info.get("singleChat"))
    entry = registry.setdefault(cid, {})
    entry["single"] = single
    entry["probed"] = ts(now())
    return single


def fetch_since(cid: str, since: str, cfg):
    """拉 cid 在 since 之后的全部消息，自动翻页。返回按时间升序的列表。"""
    page_limit = cfg["poll"]["page_limit"]
    max_pages = cfg["poll"]["max_pages_per_conv"]
    cursor, out, seen = since, [], set()
    for _ in range(max_pages):
        d, err = dws(["chat", "message", "list", "--group", cid,
                      "--time", cursor, "--direction", "newer",
                      "--limit", str(page_limit)])
        if err:
            logline(f"[fetch] {cid} err={err}")
            break
        res = d.get("result") or {}
        msgs = res.get("messages") or []
        fresh = [m for m in msgs if m.get("openMessageId") not in seen]
        for m in fresh:
            seen.add(m["openMessageId"])
        out.extend(fresh)
        if not fresh or not res.get("hasMore"):
            break
        newest = max(m["createTime"] for m in msgs)
        if newest == cursor:
            break
        cursor = newest
    out.sort(key=lambda m: m["createTime"])
    return out


# ---------------------------------------------------------------- 打标

# 日报卡片、审批卡片这类系统消息：正文是 JSON + dingtalk:// 深链，
# 里面必然带「日报」「上线」这些词，不拦住会把待办队列冲垮。
NOISE_RE = re.compile(
    r'^\{"report_id"|^\{"[a-z_]+":|dingtalkclient/action/openapp|'
    r'^\[链接\]|^\[图片\]$|^\[表情\]$'
)


def is_noise(text: str) -> bool:
    head = text[:200]
    if NOISE_RE.search(head):
        return True
    # 深链占了正文一半以上，基本就是卡片
    links = sum(len(m) for m in re.findall(r'dingtalk://\S+|https?://\S+', text))
    return len(text) > 400 and links > len(text) * 0.4


# ---------------------------------------------------------------- 时效消息
#
# 一次真实失效：某人在项目群 @所有人 说「一会儿开个会同步一下，大家二十分钟后
# 方便吗」，12 分钟后发了日程卡片（会议再过 2 分钟就开），又过 4 分钟说「入会啦」。
# 系统一条都没提醒 —— 两个原因叠在一起：
#   ① 那句话没 @ 到他本人（`@所有人` 不匹配 aliases），那个群又在
#      low_priority_conversations 里 → 打成 low，队列里等于隐形。
#   ② 那张日程卡片被 `is_noise` 当噪音吞了 → 也是 low。
# 会议这种带时间点的消息晚 16 分钟等于没通知，所以单独立一类最高优：
# 认出来就**立刻推手机**，不等轮询节奏、不等任何会话收尾，也不要求 @ 到他。
# 宁可误报（他删一下就行）也别漏（漏了就是错过会）。

# 日程卡片的铁证：正文里一定带这些深链/标识之一
CAL_MARKERS = ("type=calendar", "calendar_detail", "videoConfFromCalendar",
               "page/calendar", "钉钉视频会议", "DingTalk Meeting",
               "meetingFromCalendar")

# 约人做事的词。单独出现不算，要跟时间模式同时命中才算（免得「开会」出现在
# 「上次开会说的那个」这种复述里也报）
MEET_KW = ("开会", "开个会", "会议", "同步一下", "碰一下", "对一下", "过一下",
           "入会", "腾讯会议", "钉钉视频", "视频会议", "zoom", "约会", "面试",
           "评审", "站会", "拉个会", "会一下")

# 单独出现就够强的词：明确的截止/催办
DEADLINE_KW = ("deadline", "截止", "今天内", "下班前", "务必在", "最晚")

# 「现在就进来」这一类，不需要再带时间词 —— 它本身就是时间。
# 那场会的最后一条「@所有人 入会啦」只有"入会"没时间词，差点又漏掉。
IMMEDIATE_KW = ("入会", "进会议", "进来开会", "会议开始", "已经开始了",
                "都在等", "就等你", "等你一个", "开始了啊")

TIME_RE = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}"                                  # 19:45 / 19：45
    r"|\d{1,2}\s*点(?:半|\s*\d{1,2}\s*分?)?"                     # 8点 / 8点半 / 8点30
    r"|今天|今晚|今早|明天|明早|明晚|后天|大后天"
    r"|上午|下午|中午|傍晚|晚上"
    r"|\d{1,3}\s*(?:分钟|分|小时|个小时)\s*(?:后|之后|以后|内)"     # 20分钟后
    r"|[一二三四五六七八九十半几]{1,4}\s*(?:分钟|分|小时|个小时)\s*(?:后|之后|以后|内)"
    r"|一会儿|一会|马上|立刻|立即|现在|稍后|待会"
)

# 卡片里单独成行的 13 位毫秒时间戳就是会议起止时间。
# 只认「整行都是数字」和 targetDateTime= —— 正文里还混着
# `<13位时间戳><一串十六进制>-…` 这种把时间戳当前缀的消息 id，用宽松正则会捞错。
EPOCH_LINE_RE = re.compile(r"^(1\d{12})$", re.M)
EPOCH_PARAM_RE = re.compile(r"targetDateTime=(1\d{12})")


def card_times(text: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    """从日程卡片里抠出（开始, 结束）。抠不到返回 (None, None)。"""
    stamps = {int(s) for s in EPOCH_LINE_RE.findall(text)}
    stamps |= {int(s) for s in EPOCH_PARAM_RE.findall(text)}
    if not stamps:
        return None, None
    order = sorted(stamps)
    try:
        lo = dt.datetime.fromtimestamp(order[0] / 1000)
        hi = dt.datetime.fromtimestamp(order[-1] / 1000) if len(order) > 1 else None
    except (OverflowError, OSError, ValueError):
        return None, None
    return lo, hi


def card_title(text: str) -> str:
    """日程卡片的标题。卡片正文是按行拼的，标题是第一个像人话的行。"""
    for line in text.split("\n")[:6]:
        line = line.strip()
        if (len(line) >= 4 and not line.isdigit()
                and not line.startswith(("dingtalk://", "http", "{"))
                and "/" not in line):
            return line[:60]
    return ""


def is_time_sensitive(rec: dict) -> tuple[bool, str]:
    """这条是不是「带时间点、晚了就没意义」的消息。返回 (是否, 判定依据)。

    刻意做粗、偏向误报 —— 漏一次会议的代价远大于多推一条。
    """
    text = rec.get("text") or ""
    if any(m in text for m in CAL_MARKERS):
        return True, "日程卡片"
    # 机器载荷（报表 JSON、推送卡片）只认日程卡片那条铁证，不走关键词 ——
    # 否则 `{"report_id":…}` 里碰巧有「评审」和一个时间就报，纯噪音。
    # 真人约会永远是短句，走得通下面的路。
    if is_noise(text):
        return False, ""
    if any(k in text for k in IMMEDIATE_KW):
        return True, "喊你进会"
    has_time = bool(TIME_RE.search(text))
    if any(k in text for k in DEADLINE_KW) and has_time:
        return True, "催办+时间"
    if any(k in text for k in MEET_KW) and has_time:
        return True, "约会+时间"
    return False, ""


def classify(rec: dict, cfg) -> tuple[str, list[str]]:
    """死规则打标。返回 (level, flags)。level ∈ high/normal/low。"""
    flags = []
    text = rec["text"] or ""
    conv = rec["conv"]
    sender = rec["sender"] or ""

    is_bot = any(b in sender for b in cfg["bot_senders"])
    if is_bot:
        flags.append("bot")

    # ★ 时效判定必须在 is_noise **之前**。日程卡片满身深链，is_noise 一定判它是
    # 噪音并压成 low —— 07-30 那场会就是这么丢的。带时间点的东西不是噪音。
    urgent, why = is_time_sensitive(rec)
    if urgent:
        return "high", flags + ["urgent", f"urgent:{why}"]

    if is_noise(text):
        # 卡片只有一种情况值得看：它在私聊里直接发给我
        return ("normal" if rec["single"] else "low"), flags + ["card"]

    if rec["single"]:
        flags.append("dm")

    if any(a and a in text for a in cfg["self"]["aliases"]):
        flags.append("at_me")

    kws = [k for k in cfg["priority_keywords"] if k in text]
    if kws:
        flags.append("kw:" + ",".join(kws[:4]))

    if any(s and s in sender for s in cfg["priority_senders"]):
        flags.append("key_sender")

    low_conv = conv in cfg["low_priority_conversations"]

    # 私聊永远是高优先级 —— 有人专门来找你，没有例外
    if rec["single"] and not is_bot:
        return "high", flags
    # 机器人私聊（IM 自带的 AI 助手这类）降一档，但仍然要看
    if rec["single"] and is_bot:
        return "normal", flags
    # 群里点名到我
    if "at_me" in flags:
        return "high" if not low_conv else "normal", flags
    # 关键人 + 关键词
    if "key_sender" in flags and kws:
        return "normal" if not low_conv else "low", flags
    if kws and not low_conv:
        return "normal", flags
    return "low", flags


# ---------------------------------------------------------------- 采集

def read_at_events(state):
    """把 @我 实时事件流里的新行读进来，用于补齐没在关注列表里的群。"""
    if not os.path.exists(AT_EVENTS):
        return [], state.get("at_offset", 0)
    out = []
    off = state.get("at_offset", 0)
    with open(AT_EVENTS, encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if off > size:          # 文件被轮转过
            off = 0
        f.seek(off)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        off = f.tell()
    return out, off


# ---------------------------------------------------------------- 会话路由
#
# tmux 里每个项目开一个 Claude。某位同事说的都是项目A 的事，那些消息应该直接落到
# 项目A 那个会话手里接着干，而不是全堆到哨兵这一个会话里由我转述。
# config.json 的 `route` 就是这张表：会话名 或 发送人名 → 目标 session id。

# ------------------------------------------------------------- 会话态路由
#
# 静态路由（下面 `route_of` 查的 config.json `route` 表）回答的是
# 「**这个人 / 这个群**归哪个会话」—— 按身份、手工配，现在 5 条。
#
# 它答不了另一个问题：「**我发出去的那条私聊，回信归谁**」。发的那一刻
# 没有留下任何登记，所以回信来了无从归属。实测（2026-08-25）全部历史
# 759 条单聊里 **602 条（79%）无人接**，涉及 32 个私聊通道；它们落回哨兵
# 视图 —— 也就是「给人看」，没有任何会话拿到。Claude 私聊完一个同事等回话，
# 回信 100% 走这条路，而且**全程不报错**。
#
# 这一层补的就是那件事。钥匙是 `cid`（钉钉的通道 id，14194 条记录每条都有）。
# 机制跟 `dtcc.note_broadcast` 的播报环是同一个形状 —— 发的时候记一笔，
# 回来的时候认回原主。区别只在钥匙：播报环用「文本指纹 + 对方引用了」，
# 而同事私聊回你时不会引用。

def match_awaiting(claims, rec: dict, at) -> tuple[str, str]:
    """这条消息落在哪条「在等回信」的登记上。**纯判据：不读盘、不看时钟。**

    `claims` 形状 `[{"cid":…, "sid":…, "label":…, "until":…}]`，按登记顺序给、
    **最新的在最后** —— 跟播报环同一个约定。`at` 是「现在」，从参数进来，
    所以测过期不用真的等。

    返回 `(session_id, 标签)`，认不出返回 `("", "")`。

    三条判据，各自钉住一种会**静默漏事**的情况：

    1. **只认 `cid`，不认名字。** 名字会重名、会改，群名会被人改；`cid` 是
       钉钉给的通道 id。按名字认，就会把张三的回信投给在等李三的那个会话。
    2. **`until` 缺了算不命中**，不是算永不过期。失败方向是故意选的：
       漏路由 → 落回哨兵视图，人还看得见；而一条写坏的登记若永不过期，
       就是**永久劫持**这个通道，且不报错。宁可漏，不可劫持。
    3. **从后往前找，后登记的赢。** 两个会话同时等同一个人时的规则，
       跟 `match_broadcast`（「他引用的总是最近那条」）保持一致。

    **判死不在这里做。** 登记的会话可能已经没了，而那个结论只有
    `fleet.build_sessions()` 拿 `tmux list-panes` 算得出来（写在 `known`
    字段里）。这里再问一遍 tmux 就是第二份真相，而且等于给一个刚被证明
    可测的纯判据重新加上 IO —— 理由跟 `dtcc.routable_sessions` 逐字相同。
    **剔掉死会话的登记是调用方的事。**
    """
    cid = rec.get("cid") or ""
    if not cid:
        return "", ""
    stamp = ts(at)
    for c in reversed(list(claims or ())):
        if (c.get("cid") or "") != cid:
            continue
        sid = c.get("sid") or ""
        if not sid:
            continue                        # 写坏的登记不该把这个通道弄死
        until = c.get("until") or ""
        # 边界跟 select_for_session 的 snooze 同一套：`until > 现在` 才算还有效，
        # 所以「正好到点」算过期。今天上午在「满 15 分钟重投」上栽过同一个边界。
        if not until or until <= stamp:
            continue
        return sid, c.get("label") or ""
    return "", ""


def route_of(rec: dict, cfg, claims=None, at=None) -> tuple[str, str]:
    """这条消息归哪个 Claude 会话。返回 (session_id, 标签)，没配就是 ("","")。

    顺序：先看「在等回信」的登记，再按群/单聊名字匹配，最后按发送人名字匹配 ——
    这样既能整个需求群路由过去，也能只把某个人的私聊路由过去，而 Claude 自己
    发出去等回话的那条，回信能认回它自己。

    **登记排在静态表之前。** 因为登记是「此刻正在进行的对话」，比手工配的
    身份表更具体；反过来会让配过静态路由的人的回信永远走不到发起方。

    `claims` 默认 `None` = 没有登记，行为跟加这一层之前**逐字相同**
    （`test_routing.py` 那 14 个用例一行没改，这是刻意的）。

    给了 `claims` 就必须给 `at`：过期判断绝不能让 route_of 自己看时钟 ——
    它是纯函数，能测就因为这个。忘了给宁可**当场抛错**，也不要悄悄按
    「永不过期」走，那就是判据 2 说的永久劫持。
    """
    if claims:
        if at is None:
            raise ValueError(
                "route_of: 给了 claims 就必须给 at —— 过期判断不能靠它自己看时钟")
        sid, label = match_awaiting(claims, rec, at)
        if sid:
            # 登记没写 label 时退回对方的名字，跟静态表「用命中的那个 key 当标签」
            # 一致。⚠️ 欠账：这样一来显示层分不出「静态路由」和「等回信」，
            # 而 route_of 返回的是二元组，加第三个值会动到现有 14 个用例。
            return sid, label or (rec.get("sender") or rec.get("conv") or "")
    routes = cfg.get("route") or {}
    for key in ((rec.get("conv") or ""), (rec.get("sender") or "")):
        if key and key in routes:
            r = routes[key]
            if isinstance(r, str):
                return r, key
            return r.get("session", ""), r.get("label") or key
    return "", ""


# ---------------------------------------------------------- 「在等回信」的登记
#
# 判据在上面的 match_awaiting()，这一段是它的**存写侧和接线**。
#
# 为什么登记是一条独立命令，而不是把发消息也包进来：发私聊走的是 `dws`，
# 在这个仓库外面。在这里再造一个发送器，就是第二个发送路径 ——
# 而「同时管一件事的两套机制」正是这个系统最贵的那类毛病。
# 所以仓库只提供**原语**，「发完立刻登记」这个原子性由调用方保证
# （`dingtalk-await-reply` skill 本来就是「发完 → 挂守候」一个动作，
# 它把 await 加进那一步就够）。
#
# ⚠️ 因此这一层**仍然可能被忘**。这不是设计得好，是权衡：
# 忘了登记 → 回信落回哨兵视图（给人看，看得见）；
# 造第二个发送器 → 两份真相，且不报错。选了前者。
AWAITING = os.path.join(DATA, "awaiting.json")
AWAIT_HOURS = 12

# 守候回信：多久看一次 data/inbox.ndjson。这是**读本地文件**，不打钉钉接口，
# 所以间隔可以比采集器密 —— 真正的延迟上限由采集器的 300s 决定，不由这里。
WATCH_INTERVAL_SECONDS = 20
# 采集器多久没动静就判「我在盯一个没人写的文件」。跟 `feed --stale-minutes` 同一个量纲。
COLLECTOR_STALE_MINUTES = 20          # 默认守候时长。同事隔夜回是常态，但不该活过一个工作日。


def prune_claims(claims, at) -> list:
    """扔掉过期和写坏的登记。**纯函数。**

    写坏的定义跟 match_awaiting 一致：没 cid、没 sid、没 until 的都不算数。
    在这里就扔掉，是为了让 awaiting.json 不会无限长 —— 它每次写都会被 prune。
    """
    stamp = ts(at)
    out = []
    for c in (claims or ()):
        if not (c.get("cid") and c.get("sid") and c.get("until")):
            continue
        if c["until"] <= stamp:
            continue
        out.append(c)
    return out


def add_claim(claims, cid: str, sid: str, label: str, at, hours=AWAIT_HOURS) -> list:
    """登记一条「我在等这个通道的回信」。**纯函数：返回新列表，不改入参。**

    追加到末尾 —— match_awaiting 从后往前找，所以「后登记的赢」这条规则
    是靠顺序实现的，不是靠去重。同一个 cid 重复登记是**合法的续期**，
    不是错误。
    """
    return prune_claims(claims, at) + [{
        "cid": cid, "sid": sid, "label": label or "",
        "at": ts(at), "until": ts(at + dt.timedelta(hours=float(hours))),
    }]


def open_claims(claims, routable_sids, at) -> list:
    """还有效、且登记方还活着的登记。**纯函数。**

    `routable_sids` 是「现在还能收件的会话 id」集合，由调用方给 ——
    判死只有 `fleet.build_sessions()` 一处算得出（`known` 字段），
    这里再问一遍 tmux 就是第二份真相。传 `None` 表示**不做存活过滤**
    （拿不到 fleet 时的退路：宁可投给一个可能已经没了的会话，
    也不要因为查不到就把所有登记一起丢掉 —— 后者是静默全失效）。
    """
    live = prune_claims(claims, at)
    if routable_sids is None:
        return live
    return [c for c in live if c["sid"] in routable_sids]


def drop_claim(claims, cid: str, sid: str, at) -> list:
    """撤掉「某个会话在某个通道上」的那一条登记。**纯函数。**

    `cid` 和 `sid` 必须都对上才撤 —— 同一个通道上别的会话也可能在等，
    顺手清掉就是替别人做决定，而且是静默的（他不会知道自己的登记没了）。
    """
    return [c for c in prune_claims(claims, at)
            if not ((c.get("cid") or "") == cid and (c.get("sid") or "") == sid)]


def new_replies(records, cid: str, base_ts: str, only_from: str = "") -> list:
    """这个通道里 `base_ts` **之后**来的消息，按时间升序。**纯函数。**

    不用排除「自己发的」—— 采集器在入库前就把自己的滤掉了（`cmd_poll` 里
    `sender_id == cfg["self"]["open_dingtalk_id"]` 那一支只更新 `self_last`，
    不写 inbox）。所以旧脚本那套「挂起时最新一条一定是我刚发的、取它的
    senderId 当自己」的前提校验，在这里整个不需要 —— 那也是它最容易失效的
    地方（隔太久挂就认不出自己，直接报错退出）。

    `only_from` 同时按 `sender_id` 和 `sender`（姓名）匹配：群里人多时盯一个人，
    而调用方手上通常只有姓名，没有 openDingTalkId。

    时间是 `%Y-%m-%d %H:%M:%S`，字符串比较跟时间比较同序，所以直接比字符串 ——
    跟 `read_inbox` 里 `last > r["time"]` 那处同一套。
    """
    if not cid or not base_ts:
        return []
    out = []
    for r in records or ():
        if (r.get("cid") or "") != cid:
            continue
        t = r.get("time") or ""
        if not t or t <= base_ts:
            continue
        if only_from and only_from not in ((r.get("sender_id") or ""),
                                           (r.get("sender") or "")):
            continue
        out.append(r)
    out.sort(key=lambda r: ((r.get("time") or ""), (r.get("id") or "")))
    return out


def collector_gap_minutes(state, at):
    """采集器上次跑完到现在多少分钟。**纯函数。** 没跑过或时间戳坏了返回 `None`。"""
    lp = (state or {}).get("last_poll")
    if not lp:
        return None
    try:
        return (at - parse_ts(lp)).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def watch_verdict(records, cid, base_ts, only_from, state, at, deadline,
                  stale_minutes: float = COLLECTOR_STALE_MINUTES):
    """守候这一轮该干什么。**纯函数。** 返回 `(动作, 载荷)`。

    动作四种：

    - `reply`  有新消息了，载荷是记录列表
    - `blind`  **采集器停了**，我们盯的这份文件没人在写，载荷是原因
    - `expire` 到点了，载荷是空
    - `""`     继续等

    `blind` 必须是一个独立结果，不能并进「还没回」—— 盯一个没人写的文件，
    看起来跟「他一直没回」一模一样。这个仓库栽过 8 次的就是这个形状：
    用一个可能根本没在更新的面去推断另一头的状态。所以查不到采集器在跑时
    要出声退出，而不是安静地等满 12 小时再说「他没回」。

    顺序是「先看有没有回信」：文件里已经有回信了，采集器此刻是否健康就不影响
    这个结论 —— 消息已经落盘了。
    """
    hits = new_replies(records, cid, base_ts, only_from)
    if hits:
        return "reply", hits
    gap = collector_gap_minutes(state, at)
    if gap is None:
        return "blind", "采集器从没跑过（data/state.json 里没有 last_poll）"
    if gap > stale_minutes:
        return "blind", (f"采集器上次跑完是 {int(gap)} 分钟前，"
                         f"超过 {stale_minutes} 分钟没动静")
    if at >= deadline:
        return "expire", ""
    return "", None


# ---------------------------------------------------------------- 待拍板草稿
#
# 问题：会话拟好了一条要发给别人的回复，然后**只活在它自己的上下文里** ——
# 推不出去（没有可推的东西）、数不出来、会话一没就没了。
# 2026-08-28 用户原话：「这个不太行，因为没有跟我说要回复的消息是啥」
# —— 哨兵推送里只有别人问了什么，没有我们拟的回复。
#
# 存储用**只追加的 ndjson**，不用 save_json 那套读-改-写：
# `save_json` 是原子替换但**不加锁**，多个会话同时拟稿就会互相覆盖，
# 而覆盖掉的是「还没给他看过的草稿」——静默丢失，正是要治的病。
# 追加 + 读时按 id 取最后一条，跟 `read_inbox` 同一套。

OUTBOX = os.path.join(DATA, "outbox.ndjson")

# ---------------------------------------------------------------- 自己发的回声
#
# 问题（2026-08-28 坐实）：系统推到自聊天的每一条提醒，8 秒后被 dtcc 的
# push-loop 读回来，**当成他打的指令**派给一个 Claude 会话并叫醒它。
# 实测 10:31 / 10:32 / 11:17 三条 `[push] target=workos/5380` 全是系统自己的推送。
#
# 原来的防线是 `dtcc.OUT_MARKERS = ("【CC", "【dtwatch", "【dtcc")` —— **黑名单**。
# 而 dtwatch 推的是 `【哨兵】`/`【时效·…】`/`【待你拍板】`/`【台账任务…】`/
# `【picker】`，一个都不在里面。补前缀能修眼前这几个，但下一个新通知照样静默
# 变成指令 —— 这正是今天刚记过一遍的教训（白名单不是黑名单）。
#
# 所以改成**记账**：发出去的时候记一条正文指纹，读回来的时候按指纹认出自己。
# 跟 `dtcc.head_of` 是同一套（send 只拿得到 openTaskId，拿不到 openMessageId，
# 所以只能靠文本认）。加新通知不用改任何名单。
#
# 失败方向：指纹没对上 → 当成指令（就是今天的行为，不退步）；
# 而误杀要求他一字不差打出某条推送的前 80 个字符，实际不会发生。
SENT_SELF = os.path.join(DATA, "sent_self.ndjson")
SENT_SELF_WINDOW_MINUTES = 90     # 超过这么久的回声不再认，免得老指纹误杀新消息
SENT_SELF_KEEP = 200              # 读盘只看最后这么多行

# 推送里每条草稿最多显示多少字。手机上看的，太长翻不完；
# 全文在 `outbox show <id>`，推送里只给够他判断「这稿对不对路」的量。
PUSH_DRAFT_CHARS = 160
PUSH_ASK_CHARS = 60
# 一次推送里最多详列几条，剩下的只报数 —— 一条几百字的钉钉消息没人读。
PUSH_MAX_ITEMS = 3


def send_head(text: str) -> str:
    """正文指纹：压平空白取前 80 字。**纯函数。**

    跟 `dtcc.head_of` 同一套口径。取头不取全文，因为钉钉那边偶尔会把长消息
    截断或改写尾部，而头部稳定。
    """
    return re.sub(r"\s+", " ", (text or "").strip())[:80]


def is_self_echo(text: str, ring, at, minutes: float = SENT_SELF_WINDOW_MINUTES) -> bool:
    """这条是不是我们自己刚发出去的那条。**纯函数。**

    要求**指纹完全相等**且在窗口内 —— 前缀/子串匹配会误杀他引用我们播报时
    自己写的话。
    """
    head = send_head(text)
    if not head:
        return False
    for e in reversed(list(ring or ())):
        if (e.get("head") or "") != head:
            continue
        stamp = e.get("at") or ""
        if not stamp:
            continue
        try:
            if (at - parse_ts(stamp)).total_seconds() <= minutes * 60:
                return True
        except ValueError:
            continue
    return False


def note_self_send(text: str, at) -> None:
    """记一条「这是我们自己发的」。IO 外壳，只追加。"""
    head = send_head(text)
    if not head:
        return
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(SENT_SELF, "a", encoding="utf-8") as f:
            f.write(json.dumps({"head": head, "at": ts(at)},
                               ensure_ascii=False) + "\n")
    except OSError:                                       # noqa: BLE001
        pass          # 记不上账最多退回今天的行为，不该让发送本身失败


def recent_self_sends() -> list:
    """最近发出去的指纹。IO 外壳，只读文件末尾若干行。"""
    if not os.path.exists(SENT_SELF):
        return []
    try:
        with open(SENT_SELF, encoding="utf-8") as f:
            lines = f.readlines()[-SENT_SELF_KEEP:]
    except OSError:                                       # noqa: BLE001
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def collapse(text: str, n: int) -> str:
    """压成一行并截断。**纯函数。**"""
    one = " ".join((text or "").split())
    return one if len(one) <= n else one[:n] + "…"


def fold_outbox(lines) -> list:
    """一堆 ndjson 行 → 每个 id 的最新状态，按创建时间升序。**纯函数。**

    同一个 id 后写的覆盖先写的（drop / 标记已推送都是追加一条新的）。
    """
    seen = {}
    for r in lines or ():
        if isinstance(r, dict) and r.get("id"):
            prev = seen.get(r["id"]) or {}
            merged = dict(prev)
            merged.update(r)
            seen[r["id"]] = merged
    return sorted(seen.values(), key=lambda r: (r.get("at") or "", r.get("id") or ""))


def next_draft_id(entries, at) -> str:
    """下一个草稿 id。**纯函数。** 形如 `ob-260828-3`。

    不用随机数：同样的输入要能算出同样的结果，测试才不用打桩。
    序号在**当天之内**递增，跨天从 1 开始 —— 他在手机上要打 "发3"，
    编号必须短到能一眼看清、能手打。
    """
    day = at.strftime("%y%m%d")
    pre = "ob-%s-" % day
    used = []
    for e in entries or ():
        i = e.get("id") or ""
        if i.startswith(pre):
            try:
                used.append(int(i[len(pre):]))
            except ValueError:
                pass
    return pre + str((max(used) if used else 0) + 1)


def make_draft(entries, at, to_label, cid, draft, about_text="",
               about_id="", by="", single=True) -> dict:
    """造一条待拍板草稿。**纯函数。**

    `cid` 落盘但**不进推送文本** —— 它是发送句柄，二期的守护进程要用，
    给人看没有意义（人认的是姓名）。
    """
    return {
        "id": next_draft_id(entries, at),
        "at": ts(at),
        "to_label": to_label or "",
        "cid": cid or "",
        "single": bool(single),
        "about_id": about_id or "",
        "about_text": about_text or "",
        "draft": draft or "",
        "by": by or "",
        "status": "pending",
        "notified_at": "",
    }


def pending_drafts(entries) -> list:
    """还等着他拍板的。**纯函数。**"""
    return [e for e in fold_outbox(entries) if e.get("status") == "pending"]


def unnotified(entries) -> list:
    """待拍板、且还没推给他看过的。**纯函数。**

    「推送被免打扰/最小间隔挡掉」和「已经给他看过了」必须分得开 ——
    挡掉了就要留着下次再推，否则草稿静默躺着，跟没拟一样。
    """
    return [e for e in pending_drafts(entries) if not e.get("notified_at")]


def format_push(fresh, total_pending: int) -> str:
    """推到手机上的那段话。**纯函数。**

    三件事必须在里面，缺一件这条推送就白推：
      1. **拟的回复是什么** —— 这是 2026-08-28 那次的直接教训，
         只说「有人点你的名」而不给拟稿，他没法判断；
      2. **一共有几条待拍板** —— 「我不知道他到底有几条要发」；
      3. **怎么答** —— 在手机上能打出来的短指令。
    """
    if not fresh:
        return ""
    head = "【待你拍板】新增 %d 条" % len(fresh)
    if total_pending > len(fresh):
        head += "，共 %d 条未处理" % total_pending
    lines = [head]
    for e in fresh[:PUSH_MAX_ITEMS]:
        where = "私聊" if e.get("single") else "群"
        lines.append("")
        lines.append("[%s] 回 %s（%s）" % (e.get("id", ""),
                                          e.get("to_label") or "?", where))
        if e.get("about_text"):
            lines.append("  他问：" + collapse(e["about_text"], PUSH_ASK_CHARS))
        lines.append("  拟回：" + collapse(e.get("draft", ""), PUSH_DRAFT_CHARS))
    if len(fresh) > PUSH_MAX_ITEMS:
        lines.append("")
        lines.append("（还有 %d 条，`outbox list` 看全部）" % (len(fresh) - PUSH_MAX_ITEMS))
    lines.append("")
    # 一期还不能从手机发出去，说清楚，别让他在手机上白打字等回音。
    lines.append("一期还不能从手机发，要发去电脑上处理；不要了回复不管用，"
                 "用 `outbox drop <编号>`。")
    return "\n".join(lines)


def mark_notified(ids, at) -> list:
    """给「已推送」造补丁行。**纯函数。**"""
    stamp = ts(at)
    return [{"id": i, "notified_at": stamp} for i in ids]


def drop_patch(entry_id: str, at, why: str = "") -> dict:
    """给「丢弃」造补丁行。**纯函数。**"""
    return {"id": entry_id, "status": "dropped",
            "dropped_at": ts(at), "drop_reason": why or ""}


def resolve_cid(records, name: str, kind: str = "single"):
    """从收件历史里认出「这个名字对应的通道」。**纯函数。**

    `kind`：`single` 只认私聊（默认，跟以前一样）、`group` 只认群、`any` 都认。
    群和私聊各有各的通道，混着认就可能把「等某人私聊回信」的登记落到
    一个同名的群上。

    返回 `(cid, 有几个不同的候选)`。认不出返回 `("", 0)`。

    为什么从历史里认而不是去问 dws：这个仓库已经存了 14198 条记录，每条都带
    `cid`；再去问一次接口是多一条依赖、多一个会失败的地方。代价是**从没跟你
    私聊过的人认不出来** —— 那种情况本来也没有「回信」可等。

    候选多于一个时把数字带回去，让调用方决定要不要拦 —— 重名的人各有各的
    通道，猜错就是把回信投给等另一个人的会话。
    """
    if not name:
        return "", 0
    seen = {}
    for r in records or ():
        is_single = bool(r.get("single"))
        if kind == "single" and not is_single:
            continue
        if kind == "group" and is_single:
            continue
        if name not in ((r.get("sender") or ""), (r.get("conv") or "")):
            continue
        cid = r.get("cid") or ""
        if cid:
            seen[cid] = r.get("time") or ""
    if not seen:
        return "", 0
    newest = max(seen.items(), key=lambda kv: kv[1])[0]
    return newest, len(seen)


def load_claims() -> list:
    return load_json(AWAITING, []) or []


def save_claims(claims) -> None:
    save_json(AWAITING, claims)


def live_claims(at):
    """当下真正该参与路由的登记。IO 外壳：读盘 + 问 fleet 谁还活着。"""
    claims = load_claims()
    if not claims:
        return []
    sids = None
    try:
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        import fleet
        sids = {sid for sid, rec in (fleet.sessions() or {}).items()
                if fleet.routable(rec)}
    except Exception:                                    # noqa: BLE001
        sids = None            # 问不到就不过滤，见 open_claims 的文档
    return open_claims(claims, sids, at)


def cmd_await(cfg, args):
    """登记 / 查看 / 清除「在等回信」。"""
    at = now()
    claims = load_claims()

    if args.list:
        live = live_claims(at)
        if not live:
            print("没有在等的回信。")
            return 0
        for c in live:
            print(f"{c['sid'][:4]}  等到 {c['until']}  {c.get('label') or c['cid'][:12]}")
        return 0

    if args.clear:
        keep = [c for c in prune_claims(claims, at)
                if not (args.to and c.get("label") == args.to)
                and not (args.cid and c.get("cid") == args.cid)]
        if not args.to and not args.cid:
            keep = []
        save_claims(keep)
        print(f"清掉 {len(prune_claims(claims, at)) - len(keep)} 条，还剩 {len(keep)} 条。")
        return 0

    sid = args.session or fleet_sid()
    if not sid:
        print("不知道该登记给哪个会话：给 --session，或让 CLAUDE_CODE_SESSION_ID 有值。",
              file=sys.stderr)
        return 2

    cid, n = args.cid, 1
    if not cid:
        cid, n = resolve_cid(read_inbox(), args.to)
    if not cid:
        print(f"收件历史里找不到跟「{args.to}」的私聊通道 —— "
              f"他从没私聊过你的话，本来也没有回信可等。\n"
              f"确定要等就用 --cid 直接给通道 id。", file=sys.stderr)
        return 2
    if n > 1:
        print(f"⚠️ 「{args.to}」匹配到 {n} 个不同通道，用了最近的那个 {cid[:12]}…；"
              f"不对就用 --cid 指定。", file=sys.stderr)

    claims = add_claim(claims, cid, sid, args.to or "", at, args.hours)
    save_claims(claims)
    print(f"已登记：{args.to or cid[:12]} 的回信归 {sid[:4]}，"
          f"到 {claims[-1]['until']} 为止。")
    return 0


def read_inbox_cid(cid: str) -> list:
    """只读某一个通道的记录。IO 外壳。

    先按子串粗筛再 `json.loads` —— inbox 现在 17MB / 15k 行，全量 `read_inbox()`
    每轮 0.11s，粗筛 0.03s。守候要跑 12 小时、每 20 秒一轮，差的是两千多次全文解析。

    不用「记住文件偏移只读新增」那个更快的写法：inbox 是只追加的，但 `reclassify`
    重打标之后会把同一个 id 再追加一遍，按 id 去重（保留最后一条）才是对的，
    这跟 `read_inbox` 同一套。偏移法拿不到「后面那条覆盖前面那条」这个语义。

    也**不**走 `read_inbox()`：它会套 `self_last` 的降级逻辑（我之后又说过话就压级别），
    那是给「待处理队列」用的判断，跟「他回没回我」无关。
    """
    if not cid or not os.path.exists(INBOX):
        return []
    seen = {}
    with open(INBOX, encoding="utf-8") as f:
        for line in f:
            if cid not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("cid") or "") == cid and r.get("id"):
                seen[r["id"]] = r
    return list(seen.values())


def fmt_reply(recs, label: str) -> str:
    """把命中的回信排成给会话看的文本。"""
    lines = [f"「{label}」有新消息（{len(recs)} 条）："]
    for r in recs:
        text = " ".join((r.get("text") or "").split())
        lines.append(f"  {r.get('time','')}  {r.get('sender','')}：{text}")
    return "\n".join(lines)


def cmd_await_reply(cfg, args):
    """登记 + 守候某个通道的回信。回信来了就退出，让 harness 唤起本会话。

    **不自己轮询钉钉。** 盯的是 `data/inbox.ndjson` —— 采集器（launchd `.poll`，
    300 秒一轮）已经在往里写了。旧脚本每 60 秒自己打一次
    `dws chat message list`，那是**第二个轮询器打同一个账号的同一个 token**；
    run.sh:40-47 记着这个形状上次的后果：多份进程同时轮询、同时写 state.json，
    `last_poll_took_s` 涨到 600+ 秒，表现成「采集器可能停了」反复报警。

    代价是延迟从「最多 1 分钟」变成「最多 5 分钟」（采集器的间隔）。等一个人回话，
    这个换得过。**不要在这里顺手触发一次 poll 去压延迟** —— 那就是把第二个
    写 state.json 的进程又请回来了。
    """
    at = now()

    # ---- 认通道
    cid, n = (args.cid or ""), 1
    label = args.to or args.group or ""
    if not cid:
        if not label:
            print("要么给 --cid，要么给 --to（私聊对象姓名）/ --group（群名）。",
                  file=sys.stderr)
            return 2
        cid, n = resolve_cid(read_inbox(apply_self_reply=False), label,
                             "single" if args.to else "group")
        if not cid:
            kind = "私聊过" if args.to else "在这个群里发过消息"
            print(f"收件历史里找不到「{label}」的通道 —— 他从没{kind}的话，"
                  f"本来也没有回信可等。\n确定要等就用 --cid 直接给通道 id。",
                  file=sys.stderr)
            return 2
        if n > 1:
            print(f"⚠️ 「{label}」匹配到 {n} 个不同通道，用了最近的那个 "
                  f"{cid[:12]}…；不对就用 --cid 指定。", file=sys.stderr)
    label = label or cid[:12]

    # ---- 起算点默认「现在」
    # 不取「inbox 里这个通道最新一条的时间」：采集器最多滞后 5 分钟，那条可能是
    # 我发消息**之前**的旧消息，拿它当基线会立刻把旧消息当成回信、假命中一次。
    # 我们刚发完消息，此刻之后来的才是回信。
    base_ts = args.baseline or ts(at)

    # ---- 登记：回信真的来了、而本进程已经不在了的时候，路由层还认得这条归谁
    sid = args.session or fleet_sid()
    if not sid:
        print("⚠️ 不知道本会话 id（CLAUDE_CODE_SESSION_ID 空），**这次没有登记**："
              "本进程还活着时守候有效，但进程一死回信就回落到默认路由。",
              file=sys.stderr)
    elif not args.no_register:
        save_claims(add_claim(load_claims(), cid, sid, label, at, args.hours))
        print(f"已登记：「{label}」的回信归 {sid[:4]}，{args.hours} 小时内有效。")

    deadline = at + dt.timedelta(hours=float(args.hours))
    print(f"守候「{label}」{cid[:12]}… 起算 {base_ts}，"
          f"到 {ts(deadline)} 为止，每 {args.interval}s 看一次 inbox。", flush=True)

    def finish(msg: str, code: int) -> int:
        # 登记连同守候一起收尾：留着一条已经没人守的登记，会把后来的消息静默路由
        # 给一个不再等它的会话。宁可回落到默认路由（看得见），不要留个哑登记。
        if sid and not args.no_register:
            save_claims(drop_claim(load_claims(), cid, sid, now()))
        if args.out:
            try:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError as e:                              # noqa: BLE001
                print(f"（写 --out 失败：{e}）", file=sys.stderr)
        print(msg, flush=True)
        return code

    while True:
        action, payload = watch_verdict(
            read_inbox_cid(cid), cid, base_ts, args.from_who,
            load_json(STATE, {}), now(), deadline, args.stale_minutes)
        if action == "reply":
            body = fmt_reply(payload, label)
            # inbox 不存引用块，所以补几条上文当锚 —— 同一个窗口里常有好几件事在跑。
            prev = sorted((r for r in read_inbox_cid(cid)
                           if (r.get("time") or "") <= base_ts),
                          key=lambda r: r.get("time") or "")[-3:]
            if prev:
                body += "\n\n  ── 这个通道之前的几条（上文锚，inbox 不存引用块）──"
                for r in prev:
                    text = " ".join((r.get("text") or "").split())[:120]
                    body += f"\n  {r.get('time','')}  {r.get('sender','')}：{text}"
            return finish(body, 0)
        if action == "blind":
            return finish(f"[!] 守候「{label}」没法作数：{payload}。\n"
                          f"    盯的是 data/inbox.ndjson，没人往里写的时候，"
                          f"「他没回」和「我看不见」长得一模一样。\n"
                          f"    先 ./run.sh status 看采集器。", 1)
        if action == "expire":
            return finish(f"守候 {args.hours} 小时到期，「{label}」一直没有新消息。", 0)
        time.sleep(float(args.interval))


def read_outbox() -> list:
    """读全部草稿（已折叠）。IO 外壳。"""
    if not os.path.exists(OUTBOX):
        return []
    rows = []
    with open(OUTBOX, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return fold_outbox(rows)


def append_outbox(rows) -> None:
    """追加几行。IO 外壳。**只追加**，所以并发写不会互相覆盖。"""
    if not rows:
        return
    os.makedirs(DATA, exist_ok=True)
    with open(OUTBOX, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def push_outbox(cfg, at) -> dict:
    """把还没给他看过的草稿推到手机。IO 外壳。

    走 `send_reminder` 那条**唯一**的提醒通道 —— 免打扰时段和最小间隔对它同样
    生效，不因为是自动化就绕过约束（这是本仓库一贯口径，见 send_reminder）。

    被挡掉时**不**标 notified：挡掉了就得留着下次再推，否则草稿静默躺着，
    跟没拟一样。这正是要治的病。
    """
    entries = read_outbox()
    fresh = unnotified(entries)
    if not fresh:
        return {"ok": True, "skipped": "nothing_new",
                "pending": len(pending_drafts(entries))}
    body = format_push(fresh, len(pending_drafts(entries)))
    res = send_reminder(cfg, body)
    if res.get("ok"):
        append_outbox(mark_notified([e["id"] for e in fresh], at))
        logline("[outbox] 推了 %d 条待拍板" % len(fresh))
    else:
        logline("[outbox] 推送没成功 %s（%d 条仍未通知）" % (res, len(fresh)))
    res["pushed"] = len(fresh)
    res["body"] = body
    return res


def cmd_outbox(cfg, args):
    """拟好的回复落盘 + 推到手机等他拍板。**一期不发送任何东西。**"""
    at = now()
    entries = read_outbox()

    if args.action == "list":
        pend = pending_drafts(entries)
        if not pend:
            print("没有待拍板的草稿。")
            return 0
        print("待拍板 %d 条：" % len(pend))
        for e in pend:
            flag = "" if e.get("notified_at") else "  ⚠️ 还没推给他看"
            print("  [%s] %s → %s%s" % (e["id"], e["at"][5:16],
                                        e.get("to_label") or "?", flag))
            if e.get("about_text"):
                print("      他问：%s" % collapse(e["about_text"], 70))
            print("      拟回：%s" % collapse(e.get("draft", ""), 100))
        return 0

    if args.action == "show":
        if not args.id:
            print("要给 --id", file=sys.stderr)
            return 2
        hit = [e for e in entries if e["id"] == args.id]
        if not hit:
            print("没有这条草稿：%s" % args.id, file=sys.stderr)
            return 2
        print(json.dumps(hit[0], ensure_ascii=False, indent=1))
        return 0

    if args.action == "drop":
        if not args.id:
            print("要给 --id", file=sys.stderr)
            return 2
        hit = [e for e in entries if e["id"] == args.id]
        if not hit:
            print("没有这条草稿：%s" % args.id, file=sys.stderr)
            return 2
        if hit[0].get("status") != "pending":
            print("这条已经是 %s 了，不用再丢" % hit[0].get("status"))
            return 0
        append_outbox([drop_patch(args.id, at, args.why)])
        print("丢掉 %s（还剩 %d 条待拍板）"
              % (args.id, len(pending_drafts(read_outbox()))))
        return 0

    if args.action == "notify":
        res = push_outbox(cfg, at)
        print(json.dumps({k: v for k, v in res.items() if k != "body"},
                         ensure_ascii=False))
        return 0 if res.get("ok") else 1

    # add
    if not args.draft:
        print("要给 --draft（拟好的回复全文）", file=sys.stderr)
        return 2
    cid, n = args.cid, 1
    if not cid:
        if not args.to:
            print("要么给 --cid，要么给 --to（对方姓名/群名）", file=sys.stderr)
            return 2
        cid, n = resolve_cid(read_inbox(apply_self_reply=False), args.to,
                             "group" if args.group else "single")
        if not cid:
            print("收件历史里找不到「%s」的通道；用 --cid 直接给。" % args.to,
                  file=sys.stderr)
            return 2
        if n > 1:
            print("⚠️ 「%s」匹配到 %d 个通道，用了最近的 %s…"
                  % (args.to, n, cid[:12]), file=sys.stderr)

    about_text = args.about_text
    if args.about_id and not about_text:
        hit = [r for r in read_inbox(apply_self_reply=False)
               if r.get("id") == args.about_id]
        if hit:
            about_text = hit[0].get("text") or ""

    draft = make_draft(entries, at, args.to or "", cid, args.draft,
                       about_text, args.about_id, args.session or fleet_sid(),
                       single=not args.group)
    append_outbox([draft])
    print("落盘 %s → %s" % (draft["id"], draft["to_label"] or cid[:12]))

    if args.no_notify:
        print("（--no-notify：没推。他看不到，记得自己告诉他。）")
        return 0
    res = push_outbox(cfg, at)
    if res.get("ok"):
        print("已推到他手机（这次推了 %d 条）" % res.get("pushed", 0))
    else:
        # 静默失败是这里最不能有的东西：他不知道有草稿，会话以为通知过了。
        print("⚠️ **没推出去**：%s —— 他现在还不知道有这条。"
              "免打扰/最小间隔挡的话，下次 `outbox notify` 会补推；"
              "急就当面告诉他。" % json.dumps(res, ensure_ascii=False),
              file=sys.stderr)
    return 0


def fleet_sid() -> str:
    """本会话 id。跟 fleet.py / dtcc.py 认的是同一个环境变量。"""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID") or "").strip()


# 「必达」条目投出去之后，多久没人 mark 就重投一次。
REDELIVER_AFTER_MINUTES = 15


def must_arrive(rec: dict) -> bool:
    """这条属于「必达」类吗 —— 只有点名到他本人的算。

    为什么不是「所有 high」：ledger 里「投递过又没人 mark」的 13 条，其中 12 条是
    私聊里的对话碎片（"干掉"、"我以为他带我们"、"[语音通话] 通话时长 2:53"），
    级别是 high 只因为「私聊永远 high」这条规则。把那些也拿去无限重投就是纯刷屏。
    真正必达的是 at_me —— 有人点了他的名字要他做事。
    """
    return "at_me" in (rec.get("flags") or [])


# 投递优先级的档位。以前是 cmd_for_session 里的一个局部字典，
# 抽出来是因为判据搬到了 select_for_session，两边得共用同一份。
LEVEL_ORDER = {"high": 0, "normal": 1, "low": 2}


def select_for_session(records, cfg, session, level, triage, ledger, at, claims=None):
    """挑出「归这个会话、此刻该投给它」的条目。**纯判据：不读文件、不看时钟。**

    抽出来的理由跟 tmux_probe 抽屏幕抓取一样，是这条判据错了会**静默漏事**：
    幂等破了就是同一条任务投两次（收尾一次塞一遍，刷屏）；反过来收得太死就是
    黑洞 —— 老实现 `if r["id"] in ledger: continue` 只要投过一次就永远不再投，
    实测有一条投出去没人接住，挂了 25 小时才被发现，而且**全程不报错**。

    原来这段判据长在 `cmd_for_session` 里，跟 `read_inbox()` / `load_json` /
    `now()` / `print` 缠在一起，要测就得写 `data/` 下的生产文件。现在四样外部
    状态全从参数进来：`records`（收件箱）、`triage`（处置台账）、
    `ledger`（投递台账）、`at`（现在）。测试给一个固定时刻，就能不等真实
    15 分钟地验重投窗口。

    返回 `[(rec, label, prev_stamp)]`；`prev_stamp` 非空表示这是一次**重投**，
    调用方据此写日志。**不改 rec** —— 打 `route_label` 留给调用方，
    这样这个函数拿同一批记录跑两次结果一样。
    """
    want = LEVEL_ORDER[level]
    out = []
    for r in records:
        sid, label = route_of(r, cfg, claims, at)
        if sid != session:
            continue
        # 不把 low 塞给项目会话 —— 日报卡片、CI 推送、文件消息刷屏没意义
        if LEVEL_ORDER.get(r.get("level", "low"), 2) > want:
            continue
        t = triage.get(r["id"], {})
        if t.get("status") in ("done", "ignored"):
            continue
        if t.get("status") == "snoozed" and t.get("until", "") > ts(at):
            continue
        prev = ledger.get(r["id"])
        if prev:
            # 以前这里是 `if r["id"] in ledger: continue` —— 只要投递过一次就**永远**
            # 不再投，不管有没有被 mark。于是「投出去了、但那个会话没接住」的条目变成
            # 永久黑洞：有一条投过一次，triage 里一条记录都没有，
            # 之后再也没被投出来，挂了 25 小时。
            # 现在只有「已 mark」才算真落地；没 mark 的必达条目过窗口就重投。
            if t:
                continue          # 有 triage 记录（todo 之类）= 他在跟了，别再投
            if not must_arrive(r):
                continue          # 非必达类保持老行为：投一次就算了，不刷屏
            try:
                waited = (at - parse_ts(prev)).total_seconds()
            except ValueError:
                waited = REDELIVER_AFTER_MINUTES * 60      # 时间戳坏了当成该重投
            if waited < REDELIVER_AFTER_MINUTES * 60:
                continue          # 刚投过，别每次收尾都塞一遍
        out.append((r, label, prev or ""))
    return out


def stamp_delivered(ledger, records, at):
    """把这一批记进投递台账。下一轮 `select_for_session` 就靠它判重。

    单独一个函数是为了让「投一次 → 记账 → 再问一次」这个循环能在内存里跑完，
    不用碰 `data/injected.json`。
    """
    stamp = ts(at)
    for r in records:
        ledger[r["id"]] = stamp
    return ledger


def cmd_for_session(cfg, args):
    """吐出「归本会话、该投给它」的条目，并记账防重复投递。

    dtcc 的 Stop hook 调这个：会话一收尾就问一句「有我的活吗」，
    有就把它注入回去接着干。--peek 只看不记账。

    判据在 `select_for_session` 里，这里只剩 IO：读两个台账、写日志、
    记账、打印。
    """
    triage = load_json(TRIAGE, {})
    ledger = load_json(INJECTED, {})
    at = now()
    out = []
    for r, label, prev in select_for_session(
            read_inbox(), cfg, args.session, args.level, triage, ledger, at,
            live_claims(at)):
        if prev:
            logline(f"[for-session] 重投 {r['id'][:14]} "
                    f"（上次 {prev}，至今无 triage 记录）")
        r["route_label"] = label
        out.append(r)
    if out and not args.peek:
        stamp_delivered(ledger, out, at)
        save_json(INJECTED, ledger)
    for r in out:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def self_reacted(m: dict, cfg) -> str:
    """他自己给这条消息贴过表情吗？贴了返回表情名。

    `message list` 的每条消息都带 emotionReplyList：
      [{"emoji": "OK", "replyUsers": ["<我自己>", "<别的谁>"]}]
    他常用「贴个 OK」代替回一句话（TRIAGE.md 里就是这么写的规矩），
    不解析这个字段的话，他已经处理掉的事在队列里永远是「没人接」。
    """
    names = set(cfg["self"]["aliases"]) | {cfg["self"]["name"]}
    for r in m.get("emotionReplyList") or []:
        if names & set(r.get("replyUsers") or []):
            return r.get("emoji") or "?"
    return ""


def sweep_acks(cfg, per_conv_open: dict) -> list[dict]:
    """回扫「还开着的条目」，看他是不是已经贴表情结掉了。

    表情是消息发出之后才贴的，采集当时看不到，所以必须回头再看一眼。
    只扫还有待处理条目的会话，通常就 0~2 个，不费接口。
    """
    if not per_conv_open:
        return []
    triage = load_json(TRIAGE, {})
    closed = []
    for cid, items in per_conv_open.items():
        earliest = min(i["time"] for i in items)
        try:
            since = ts(parse_ts(earliest) - dt.timedelta(seconds=1))
        except ValueError:
            continue
        d, err = dws(["chat", "message", "list", "--group", cid,
                      "--time", since, "--direction", "newer", "--limit", "50"])
        if err:
            logline(f"[acks] {cid} err={err}")
            continue
        acked = {}
        for m in (d.get("result") or {}).get("messages") or []:
            e = self_reacted(m, cfg)
            if e:
                acked[m.get("openMessageId")] = e
        for i in items:
            e = acked.get(i["id"])
            if not e:
                continue
            triage[i["id"]] = {"status": "done", "ts": ts(now()),
                               "note": f"他自己贴了「{e}」表情，当已处理"}
            closed.append({"id": i["id"], "conv": i["conv"],
                           "emoji": e, "text": i["text"][:60]})
    if closed:
        save_json(TRIAGE, triage)
        logline(f"[acks] 表情结掉 {len(closed)} 条: "
                + "; ".join(f"{c['conv']}/{c['emoji']}" for c in closed))
    return closed


def cmd_poll(cfg, args):
    os.makedirs(DATA, exist_ok=True)
    registry = load_json(REGISTRY, {})
    state = load_json(STATE, {})
    cursors = state.setdefault("cursors", {})
    started = now()

    convs, err = conversation_list(cfg)
    if err:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        logline(f"[poll] conversation-list failed: {err}")
        return 1

    by_name = {c["conversationName"]: c["openConversationId"] for c in convs}
    order = [c["openConversationId"] for c in convs]
    names = {c["openConversationId"]: c["conversationName"] for c in convs}
    for cid, nm in names.items():
        registry.setdefault(cid, {})["name"] = nm

    muted = set(cfg["mute_conversations"])

    # 候选 = 最近活跃前缀 ∪ 常驻关注群 ∪ @我事件带来的会话
    at_events, at_off = read_at_events(state)
    at_cids = {e.get("conversation_id") for e in at_events if e.get("conversation_id")}
    at_msgids = {e.get("message_id") for e in at_events if e.get("message_id")}

    prefix_n = min(cfg["poll"]["scan_top_n"], len(order))
    candidates = list(order[:prefix_n])
    for nm in cfg["groups_always_watch"]:
        cid = by_name.get(nm)
        if cid and cid not in candidates:
            candidates.append(cid)
    for cid in at_cids:
        if cid not in candidates:
            candidates.append(cid)
    candidates = [c for c in candidates if names.get(c, "") not in muted]

    default_cursor = state.get("last_poll") or ts(
        started - dt.timedelta(hours=cfg["poll"]["new_conv_lookback_hours"]))

    self_last = state.setdefault("self_last", {})
    new_records, per_conv, scanned = [], {}, 0
    idx = 0
    while idx < len(candidates):
        cid = candidates[idx]
        idx += 1
        scanned += 1
        conv_name = names.get(cid, registry.get(cid, {}).get("name", cid))
        since = cursors.get(cid, default_cursor)
        msgs = fetch_since(cid, since, cfg)
        if not msgs:
            continue
        single = conv_is_single(cid, registry)
        kept = 0
        for m in msgs:
            mid = m.get("openMessageId")
            sender_id = m.get("senderOpenDingTalkId", "")
            if sender_id == cfg["self"]["open_dingtalk_id"]:
                # 自己发的不进队列，但要记下最后发言时间：
                # 在这之前的消息我至少是看见了，优先级该往下压
                prev = self_last.get(cid, "")
                if m.get("createTime", "") > prev:
                    self_last[cid] = m["createTime"]
                continue
            rec = {
                "id": mid,
                "cid": cid,
                "conv": conv_name,
                "single": single,
                "sender": m.get("sender", ""),
                "sender_id": sender_id,
                "time": m.get("createTime", ""),
                "text": (m.get("content") or "").strip(),
                "collected_at": ts(started),
            }
            level, flags = classify(rec, cfg)
            if mid in at_msgids and "at_me" not in flags:
                flags.append("at_me")
                level = "high"
            # 采集时他就已经贴过表情的（比如隔了一轮才扫到）直接压成 low
            emoji = self_reacted(m, cfg)
            if emoji:
                flags.append(f"acked:{emoji}")
                level = "low"
            rec["level"], rec["flags"] = level, flags
            new_records.append(rec)
            kept += 1
            # ★ 时效消息就在这儿推 —— 分类完当场推，不等这一轮 poll 把
            # 几十个会话扫完，也不等任何 Claude 会话收尾。这是采集链上最早的点。
            if "urgent" in flags:
                try:
                    push_urgent(cfg, rec, next(
                        (f.split(":", 1)[1] for f in flags
                         if f.startswith("urgent:")), "时效"))
                except Exception as e:              # noqa: BLE001
                    logline(f"[urgent] 推送异常（不挡采集）"
                            f"{type(e).__name__}: {e}")
        # +1 秒：`--time T --direction newer` 是**含 T** 的，直接把游标设成 max(createTime)
        # 会让边界那条消息每轮都被重新捞一遍、重新追加一次。
        # （2026-07-29 发现时 inbox 里 793 行只有 242 条唯一，一条最多被采了 59 次。）
        newest_ct = max(m["createTime"] for m in msgs)
        try:
            cursors[cid] = ts(parse_ts(newest_ct) + dt.timedelta(seconds=1))
        except ValueError:
            cursors[cid] = newest_ct
        if kept:
            per_conv[conv_name] = per_conv.get(conv_name, 0) + kept

        # 自适应扩展：最近活跃前缀的最后一个还在出新消息，说明可能截短了，往下再扫一段
        at_prefix_end = idx == prefix_n and prefix_n < min(cfg["poll"]["max_scan"], len(order))
        if at_prefix_end and msgs:
            extra = order[prefix_n:prefix_n + cfg["poll"]["extend_step"]]
            prefix_n = min(prefix_n + cfg["poll"]["extend_step"], cfg["poll"]["max_scan"])
            for c in extra:
                if c not in candidates and names.get(c, "") not in muted:
                    candidates.insert(idx, c)

    if new_records:
        new_records.sort(key=lambda r: r["time"])
        with open(INBOX, "a", encoding="utf-8") as f:
            for r in new_records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    state["cursors"] = cursors
    state["self_last"] = self_last
    state["at_offset"] = at_off
    state["last_poll"] = ts(started)
    state["last_poll_took_s"] = round((now() - started).total_seconds(), 1)
    state["last_poll_scanned"] = scanned
    save_json(STATE, state)
    save_json(REGISTRY, registry)

    # 回扫表情：他常用贴 OK 代替回话，那也算处理完了
    triage_now = load_json(TRIAGE, {})
    per_conv_open = {}
    for r in read_inbox(apply_self_reply=False):
        t = triage_now.get(r["id"], {})
        if t.get("status") in ("done", "ignored"):
            continue
        if t.get("status") == "snoozed" and t.get("until", "") > ts(now()):
            continue
        if r["level"] == "low":
            continue
        per_conv_open.setdefault(r["cid"], []).append(r)
    acked = sweep_acks(cfg, per_conv_open)

    # 点名到他、超时没人 triage 的，自己往自聊天推一条 —— 这是 at_me 唯一的
    # 「必达」通道。以前 remind 只能手敲，等于没有。
    remind = auto_remind(cfg)

    counts = {"high": 0, "normal": 0, "low": 0}
    for r in new_records:
        counts[r["level"]] += 1
    summary = {
        "ok": True,
        "at": ts(started),
        "scanned_conversations": scanned,
        "new_messages": len(new_records),
        "by_level": counts,
        "by_conversation": per_conv,
        "acked_by_emoji": len(acked),
        "auto_remind": remind,
        "took_s": state["last_poll_took_s"],
    }
    logline(f"[poll] {json.dumps(summary, ensure_ascii=False)}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- 队列

def read_inbox(apply_self_reply=True):
    if not os.path.exists(INBOX):
        return []
    # 按 id 去重，同一个 id 保留**最后**一条（reclassify 重打标之后写在后面）。
    # inbox 是只追加的，而游标那个 off-by-one 曾经让边界消息每轮都被重新追加一次；
    # 在读这一层去重，所有下游命令一次全好，也不用重写历史文件。
    seen = {}
    with open(INBOX, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("id"):
                seen[r["id"]] = r
    out = sorted(seen.values(), key=lambda r: r.get("time", ""))
    if not apply_self_reply:
        return out

    # 我在这条之后又在同一个会话里说过话 —— 说明我至少看见了。
    # 不直接清掉：看见不等于处理了（比如别人问了两件事，我只答了一件），
    # 所以只降一档并打个标，让上层自己判断。
    #
    # **但点名到他本人的（at_me）不降级。** 2026-07-30 体检实测出来的洞：
    # 某同事在项目群 @ 他本人定一版基线，他之后又在同群说过别的话
    # → high 被降成 normal → `pending --level high` 里直接看不见 → 挂了 25 小时。
    # 「在同一个群里说过话」根本不等于「看见了那个 @」—— 群里几十条刷过去，
    # 被 @ 那条恰恰是最容易被冲掉的。所以 at_me 保持原级别，另打一个
    # at_me_unresolved 标记说明「降级在这里被压住了」，方便单独筛。
    self_last = load_json(STATE, {}).get("self_last", {})
    for r in out:
        last = self_last.get(r["cid"])
        if last and last > r["time"]:
            if "replied_after" not in r["flags"]:
                r["flags"] = r["flags"] + ["replied_after"]
            if "at_me" in r["flags"]:
                if "at_me_unresolved" not in r["flags"]:
                    r["flags"] = r["flags"] + ["at_me_unresolved"]
            elif r["level"] == "high":
                r["level"] = "normal"
    return out


def cmd_pending(cfg, args):
    triage = load_json(TRIAGE, {})
    recs = read_inbox()
    # 循环外取一次：live_claims 要读盘并问 fleet，放循环里就是每条记录问一遍
    _pending_claims = live_claims(now())
    order = {"high": 0, "normal": 1, "low": 2}
    want = order[args.level]
    out = []
    routed_away = {}
    for r in recs:
        t = triage.get(r["id"])
        if t:
            if t.get("status") in ("done", "ignored"):
                continue
            if t.get("status") == "snoozed" and t.get("until", "") > ts(now()):
                continue
        if order[r["level"]] > want:
            continue
        # 路由过滤：默认只看「没派给别的会话」的，否则哨兵会替项目会话转述一遍。
        # 登记（在等回信）跟静态表在这里是同一等地位——被会话认领了就不该
        # 再出现在给人看的视图里，否则同一条回信人和会话各处理一遍。
        sid, label = route_of(r, cfg, _pending_claims, now())
        if not args.all_routes:
            if args.session:
                if sid != args.session:
                    continue
            elif sid:
                routed_away.setdefault(label or sid[:4], 0)
                routed_away[label or sid[:4]] += 1
                continue
        if sid:
            r = dict(r, flags=r["flags"] + [f"route:{label or sid[:4]}"])
        out.append(r)
    out.sort(key=lambda r: (order[r["level"]], r["time"]))
    out = out[-args.limit:] if args.limit else out

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if not out:
        print("（没有待处理的消息）")
        if routed_away:
            print("  另有已派给别的会话的：" +
                  "、".join(f"{k} {v} 条" for k, v in routed_away.items()))
        return 0

    for blk in group_runs(out, args.gap):
        first, last = blk[0], blk[-1]
        kind = "私聊" if first["single"] else "群"
        span = first["time"] if len(blk) == 1 else f"{first['time']} → {last['time'][11:]}"
        lvl = min(blk, key=lambda r: order[r["level"]])["level"]
        allflags = sorted({f for r in blk for f in r["flags"]})
        print(f"[{lvl.upper()}] {span}  {kind}｜{first['conv']}｜{first['sender']}"
              f"{'' if len(blk) == 1 else f'  （{len(blk)} 条连发）'}")
        print(f"  flags={','.join(allflags) or '-'}")
        for r in blk:
            body = r["text"]
            if len(body) > args.chars:
                body = body[:args.chars] + f" …（共 {len(r['text'])} 字）"
            lines = body.splitlines() or [""]
            print(f"  | {lines[0]}")
            for ln in lines[1:]:
                print(f"  | {ln}")
        print(f"  ids: {' '.join(r['id'] for r in blk)}")
        print()
    print(f"—— 共 {len(out)} 条待处理")
    return 0


def group_runs(recs, gap_minutes):
    """把同一会话、同一发送人、间隔在 gap 分钟内的连发消息并成一块。"""
    blocks = []
    for r in recs:
        if blocks:
            prev = blocks[-1][-1]
            same = (prev["cid"] == r["cid"] and prev["sender"] == r["sender"]
                    and prev["level"] == r["level"])
            close = abs((parse_ts(r["time"]) - parse_ts(prev["time"])).total_seconds()) \
                <= gap_minutes * 60
            if same and close:
                blocks[-1].append(r)
                continue
        blocks.append([r])
    return blocks


def cmd_show(cfg, args):
    recs = {r["id"]: r for r in read_inbox()}
    r = recs.get(args.id)
    if not r:
        print(f"找不到 id={args.id}", file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if args.context:
        anchor = parse_ts(r["time"]) - dt.timedelta(minutes=args.context)
        d, err = dws(["chat", "message", "list", "--group", r["cid"],
                      "--time", ts(anchor), "--direction", "newer", "--limit", "40"])
        if err:
            print(f"\n拉上下文失败: {err}", file=sys.stderr)
            return 1
        print(f"\n—— {r['conv']} 上下文（{r['time']} 前 {args.context} 分钟起）")
        msgs = sorted((d.get("result") or {}).get("messages") or [],
                      key=lambda m: m["createTime"])
        for m in msgs:
            mark = " <<<" if m.get("openMessageId") == r["id"] else ""
            print(f"{m['createTime']} {m.get('sender','')}: "
                  f"{(m.get('content') or '')[:400]}{mark}")
    return 0


def cmd_mark(cfg, args):
    triage = load_json(TRIAGE, {})
    stamp = ts(now())
    for mid in args.ids:
        entry = {"status": args.status, "ts": stamp}
        if args.note:
            entry["note"] = args.note
        if args.status == "snoozed":
            entry["until"] = ts(now() + dt.timedelta(minutes=args.minutes))
        triage[mid] = entry
    save_json(TRIAGE, triage)
    print(json.dumps({"ok": True, "marked": len(args.ids), "status": args.status},
                     ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 提醒

def in_quiet_hours(cfg) -> bool:
    q = cfg["reminder"]
    cur = now().strftime("%H:%M")
    s, e = q["quiet_start"], q["quiet_end"]
    return (s <= cur or cur < e) if s > e else (s <= cur < e)


def send_reminder(cfg, text: str, force: bool = False,
                  touch_interval: bool = True) -> dict:
    """真正发提醒的那段。手敲的 `remind` 和 poll 的自动提醒共用同一条路，
    所以免打扰时段和最小间隔对两边都生效，不会因为自动化绕过约束。

    `touch_interval=False`：发了但**不占用**最小间隔的配额。时效消息（会议）
    用这个 —— 它是插播，不该把接下来 45 分钟的 @我 提醒挤掉。
    """
    state = load_json(STATE, {})
    if in_quiet_hours(cfg) and not force:
        return {"ok": False, "skipped": "quiet_hours"}
    last = state.get("last_reminder_at")
    gap = cfg["reminder"]["min_interval_minutes"]
    if last and not force:
        try:
            if (now() - parse_ts(last)).total_seconds() < gap * 60:
                return {"ok": False, "skipped": "min_interval", "last": last}
        except ValueError:
            pass
    _, err = dws(["chat", "message", "send",
                  "--open-dingtalk-id", cfg["self"]["open_dingtalk_id"],
                  "--text", text])
    if err:
        return {"ok": False, "error": err}
    # 记一笔「这条是我们自己发的」——否则 8 秒后 push-loop 会把它读回来当成
    # 他打的指令。记账必须在这里（唯一的发送出口），不是在每个调用方。
    note_self_send(text, now())
    if touch_interval:
        state = load_json(STATE, {})      # 重读：dws 那几十秒里别的命令可能写过
        state["last_reminder_at"] = ts(now())
        save_json(STATE, state)
    logline(f"[remind] {text[:120]}")
    return {"ok": True, "sent": text[:120]}


def push_urgent(cfg, rec: dict, why: str) -> dict:
    """时效消息立刻推手机。不看免打扰、不看最小间隔、不等任何会话。

    为什么敢无视免打扰：这一类本来就是「晚了就没意义」的。半夜误报一条他删掉
    就行；真漏了一场 9:00 的会，代价大得多。要关就把
    `reminder.urgent_ignore_quiet` 设成 false。
    """
    state = load_json(STATE, {})
    pushed = state.get("urgent_pushed") or {}
    if rec["id"] in pushed:
        return {"skipped": "already"}

    text = (rec.get("text") or "").replace("\n", " ").strip()
    where = "私聊" if rec.get("single") else (rec.get("conv") or "")
    head = f"【时效·{why}】"
    start, end = card_times(rec.get("text") or "")
    if start:
        span = start.strftime("%H:%M")
        if end and end != start:
            span += end.strftime("-%H:%M")
        mins = int((start - now()).total_seconds() // 60)
        when = f"{mins} 分钟后" if 0 <= mins <= 180 else (
            "已经开始了" if mins < 0 else start.strftime("%m-%d %H:%M"))
        body = f"{head}{span} {card_title(rec.get('text') or '')}（{when}）"
    else:
        body = f"{head}{text[:70]}"
    body += f"\n{rec.get('sender', '?')} · {where}"

    ignore_quiet = cfg["reminder"].get("urgent_ignore_quiet", True)
    res = send_reminder(cfg, body, force=ignore_quiet, touch_interval=False)
    if res.get("ok"):
        state = load_json(STATE, {})
        pushed = state.get("urgent_pushed") or {}
        pushed[rec["id"]] = ts(now())
        # 只留最近 200 条，别让 state 无限长
        if len(pushed) > 200:
            for k in sorted(pushed, key=lambda k: pushed[k])[:len(pushed) - 200]:
                pushed.pop(k, None)
        state["urgent_pushed"] = pushed
        save_json(STATE, state)
        logline(f"[urgent] 立刻推送（{why}）{rec.get('sender')}"
                f"／{where}：{text[:60]}")
    else:
        logline(f"[urgent] 推送没成功 {res} :: {text[:60]}")
    res["body"] = body
    return res


def cmd_remind(cfg, args):
    res = send_reminder(cfg, args.text, force=args.force)
    print(json.dumps(res, ensure_ascii=False))
    return 1 if res.get("error") else 0


# 点名到他的消息，多久没人 triage 就往自聊天推一条提醒。
AT_ME_REMIND_AFTER_MINUTES = 15


def at_me_handled(rec: dict, in_triage: bool) -> bool:
    """这条点名到他的消息，算不算已经有人管了。**纯函数。**

    三种算「已处理」：

    1. **triage 里有记录** —— 人或会话明确标过。
    2. **他自己贴了表情**（采集时打的 `acked:<表情>`）。这是本仓库一贯口径：
       `sweep_acks` 见到表情直接写 triage=done，但它只扫 level>low 的会话，
       而贴过表情的条目采集时就被压成 low 了 —— 所以这类只有 flag、没有 triage
       记录，必须在这儿单独认。不认的话第一条自动提醒就会去催他已经 OK 过的事。
    3. **私聊里、他之后在同一通道说过话**（`replied_after` + `single`）。

    ## 第 3 条为什么只限私聊

    `read_inbox` 会给「我之后又在同一通道说过话」的条目打 `replied_after`，
    但**故意不给 at_me 降级** —— 那是 2026-07-30 的教训：某同事在项目群
    @ 他本人定基线，他之后又在同群说过别的话，级别被降 → `pending --level high`
    里看不见 → 挂了 25 小时。**群里几十条刷过去，「说过话」根本不等于
    「看见了那个 @」。**

    但那条规矩是**群**推出来的，套到私聊上就错了：一对一窗口里，
    你在他那条之后说了话，基本就是在回他。

    2026-08-28 实测这个错的代价：哨兵认为「还没处理」的有 331 条，
    其中 **224 条他之后在同一通道说过话**。当天更直接的一次——
    13:14 对方问「仓库有了吗」，13:29:57 他回了（`self_last` 记着），
    13:30:19 哨兵**仍然**推「有人点你的名还没处理：13:14 仓库有了吗」，
    回完 22 秒。他的原话：「我已经回复了吧，你消息太慢了」。

    代价不对称，所以敢在私聊上放开：
      - 放开后误判成「已处理」→ 他没回全的话，对方会再问一次（当天就发生了，
        对方 13:21 又说「好的」、13:37 又说了一句）。**代价可见且会自愈。**
      - 不放开 → 每天几百条假警报，通道整体不可信。**代价是那个通道废掉。**
    """
    if in_triage:
        return True
    flags = rec.get("flags") or []
    if any(f.startswith("acked:") for f in flags):
        return True
    if rec.get("single") and "replied_after" in flags:
        return True
    return False


def stale_at_me(cfg, minutes: int = AT_ME_REMIND_AFTER_MINUTES) -> list[dict]:
    """点名到他、超过 minutes 分钟、又没有任何「已处理」痕迹的。

    「算不算已处理」这条判据在 `at_me_handled` 里（纯函数，三种都要认，
    连同为什么私聊和群不一样，都写在那儿）。这里只剩 IO：读台账、比时间。
    """
    triage = load_json(TRIAGE, {})
    cutoff = now() - dt.timedelta(minutes=minutes)
    out = []
    for r in read_inbox():
        if "at_me" not in r["flags"]:
            continue
        if at_me_handled(r, r["id"] in triage):
            continue
        try:
            if parse_ts(r["time"]) > cutoff:
                continue
        except ValueError:
            continue
        out.append(r)
    return out


def auto_remind(cfg) -> dict:
    """poll 每拍收尾时调：点名到他、超时又没人 triage 的，往自聊天推一条。

    为什么要接这一下：`cmd_remind` 本身写得挺全（免打扰时段、最小间隔都有），
    但**全仓零调用方** —— 只能手敲。于是「@他本人的消息挂了 25 小时」这种事，
    系统里没有任何通道能让它自己冒出来。这里把它接到常驻的 poll 循环上。

    一条只提醒一次（记在 state.reminded_at_me）。不一直催是因为另外两条通道会
    继续让它活着：`pending --level high` 不再把 at_me 降级冲掉，`for-session`
    每 15 分钟会把它重投给目标会话。自提醒只负责「戳一下让他知道有这回事」。

    发的是他自己的单聊，内容是「谁在哪个群点了你的名」，不替他回任何人。
    """
    if not cfg["reminder"].get("auto_at_me", True):
        return {"skipped": "disabled"}
    state = load_json(STATE, {})
    already = state.get("reminded_at_me") or {}
    stale = [r for r in stale_at_me(cfg) if r["id"] not in already]
    if not stale:
        return {"skipped": "nothing_stale"}

    lines = ["【哨兵】有人点你的名还没处理："]
    for r in stale[:5]:
        where = "私聊" if r.get("single") else r.get("conv", "")
        text = (r.get("text") or "").replace("\n", " ").strip()
        lines.append(f"· {r['time'][5:16]} {r.get('sender', '?')}"
                     f"（{where}）{text[:60]}")
    if len(stale) > 5:
        lines.append(f"（还有 {len(stale) - 5} 条）")
    res = send_reminder(cfg, "\n".join(lines))

    if res.get("ok"):
        # 只有真发出去了才记账，否则被免打扰/最小间隔挡掉的这批会被永久吞掉
        state = load_json(STATE, {})
        already = state.get("reminded_at_me") or {}
        stamp = ts(now())
        for r in stale:
            already[r["id"]] = stamp
        state["reminded_at_me"] = already
        save_json(STATE, state)
    res["stale_at_me"] = len(stale)
    return res


# ---------------------------------------------------------------- 其它

def cmd_init(cfg, args):
    os.makedirs(DATA, exist_ok=True)
    convs, err = conversation_list(cfg)
    if err:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1
    registry = load_json(REGISTRY, {})
    state = load_json(STATE, {})
    cursors = state.setdefault("cursors", {})
    stamp = ts(now() - dt.timedelta(hours=args.lookback))
    for c in convs:
        cid, nm = c["openConversationId"], c["conversationName"]
        registry.setdefault(cid, {})["name"] = nm
        cursors.setdefault(cid, stamp)
    state["cursors"] = cursors
    state["last_poll"] = stamp
    state.setdefault("at_offset", 0)
    save_json(REGISTRY, registry)
    save_json(STATE, state)
    for p in (INBOX, AT_EVENTS):
        open(p, "a", encoding="utf-8").close()
    print(json.dumps({"ok": True, "conversations": len(convs),
                      "cursor_start": stamp}, ensure_ascii=False, indent=1))
    return 0


ANNOUNCED = os.path.join(DATA, "announced.json")


def cmd_feed(cfg, args):
    """给 Monitor 用：每条「还没播报过」的消息打一行，播完就记账。

    刻意做成一行一条 —— Monitor 把每行 stdout 变成一条通知推给 Claude。
    """
    seen = set(load_json(ANNOUNCED, []))
    triage = load_json(TRIAGE, {})
    order = {"high": 0, "normal": 1, "low": 2}
    want = order[args.level]
    fresh = []
    for r in read_inbox():
        if r["id"] in seen or order[r["level"]] > want:
            continue
        if triage.get(r["id"], {}).get("status") in ("done", "ignored"):
            seen.add(r["id"])
            continue
        fresh.append(r)
    for r in fresh:
        kind = "私聊" if r["single"] else "群"
        text = " ".join((r["text"] or "").split())[:160]
        print(f"[{r['level']}] {r['time'][11:]} {kind}｜{r['conv']}｜"
              f"{r['sender']}: {text}  <{r['id']}>", flush=True)
        seen.add(r["id"])
    if fresh:
        save_json(ANNOUNCED, sorted(seen))

    # 静默不等于没事：采集器挂了也要出声，否则「没通知」看起来跟「没消息」一样
    state = load_json(STATE, {})
    lp = state.get("last_poll")
    stale = lp is None or (now() - parse_ts(lp)).total_seconds() > args.stale_minutes * 60
    if stale:
        warned = state.get("stale_warned_at")
        if not warned or (now() - parse_ts(warned)).total_seconds() > 1800:
            print(f"[!] dtwatch 采集器可能停了：上次采集 {lp or '从未'}，"
                  f"已超过 {args.stale_minutes} 分钟。用 ./run.sh status 查一下。", flush=True)
            state["stale_warned_at"] = ts(now())
            save_json(STATE, state)
    elif state.get("stale_warned_at"):
        state.pop("stale_warned_at", None)
        save_json(STATE, state)
    return 0


def cmd_reclassify(cfg, args):
    """改完 config.json 的规则后，把已经收进来的历史消息重新打一遍标。"""
    recs = read_inbox(apply_self_reply=False)
    changed = 0
    for r in recs:
        old = r.get("level")
        r["level"], r["flags"] = classify(r, cfg)
        if r["level"] != old:
            changed += 1
    with open(INBOX, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    counts = {"high": 0, "normal": 0, "low": 0}
    for r in recs:
        counts[r["level"]] += 1
    print(json.dumps({"ok": True, "total": len(recs), "level_changed": changed,
                      "by_level": counts}, ensure_ascii=False, indent=1))
    return 0


def cmd_status(cfg, args):
    state = load_json(STATE, {})
    triage = load_json(TRIAGE, {})
    recs = read_inbox()
    def open_item(r):
        t = triage.get(r["id"], {})
        if t.get("status") in ("done", "ignored"):
            return False
        if t.get("status") == "snoozed" and t.get("until", "") > ts(now()):
            return False
        return True

    pend = [r for r in recs if open_item(r)]
    snoozed = sum(1 for r in recs
                  if triage.get(r["id"], {}).get("status") == "snoozed"
                  and triage[r["id"]].get("until", "") > ts(now()))
    print(json.dumps({
        "last_poll": state.get("last_poll"),
        "last_poll_took_s": state.get("last_poll_took_s"),
        "last_poll_scanned": state.get("last_poll_scanned"),
        "last_reminder_at": state.get("last_reminder_at"),
        "in_quiet_hours": in_quiet_hours(cfg),
        "conversations_tracked": len(state.get("cursors", {})),
        "inbox_total": len(recs),
        "pending_total": len(pend),
        "pending_high": sum(1 for r in pend if r["level"] == "high"),
        "pending_normal": sum(1 for r in pend if r["level"] == "normal"),
        "snoozed": snoozed,
        "at_stream_lines": sum(1 for _ in open(AT_EVENTS, encoding="utf-8"))
        if os.path.exists(AT_EVENTS) else 0,
    }, ensure_ascii=False, indent=1))
    return 0


def main():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print(f"读不到配置 {CONFIG_PATH}", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(prog="dtwatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="建注册表并把游标对齐到现在")
    p.add_argument("--lookback", type=float, default=0.05, help="从几小时前开始算（默认 3 分钟）")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("poll", help="跑一轮增量采集")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("pending", help="打印待处理消息")
    p.add_argument("--level", choices=["high", "normal", "low"], default="normal")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--chars", type=int, default=500)
    p.add_argument("--gap", type=int, default=15, help="连发合并的时间间隔（分钟）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--session", default="",
                   help="只看派给这个 session id 的（config.json 的 route 表）")
    p.add_argument("--all-routes", action="store_true",
                   help="连派给别的会话的一起看")
    p.set_defaults(fn=cmd_pending)

    p = sub.add_parser("for-session",
                       help="吐出归某个会话、还没投递过的条目（给 dtcc 的 Stop hook 用）")
    p.add_argument("session")
    p.add_argument("--level", choices=["high", "normal", "low"], default="normal")
    p.add_argument("--peek", action="store_true", help="只看，不记账")
    p.set_defaults(fn=cmd_for_session)

    p = sub.add_parser("show", help="看某条记录 + 会话上下文")
    p.add_argument("id")
    p.add_argument("--context", type=int, default=30, help="往前回溯几分钟，0=不拉上下文")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("mark", help="标记处理结果")
    p.add_argument("ids", nargs="+")
    p.add_argument("--status", choices=["done", "ignored", "snoozed", "todo"],
                   default="done")
    p.add_argument("--note", default="")
    p.add_argument("--minutes", type=int, default=120, help="snoozed 时推迟多久")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("remind", help="给自己的钉钉单聊发提醒")
    p.add_argument("text")
    p.add_argument("--force", action="store_true", help="无视免打扰和最小间隔")
    p.set_defaults(fn=cmd_remind)

    p = sub.add_parser("feed", help="打印未播报过的消息，一行一条（给 Monitor 用）")
    p.add_argument("--level", choices=["high", "normal", "low"], default="normal")
    p.add_argument("--stale-minutes", type=int, default=20,
                   help="采集器多久没跑就报警")
    p.set_defaults(fn=cmd_feed)

    p = sub.add_parser("reclassify", help="按当前规则重打历史消息的标")
    p.set_defaults(fn=cmd_reclassify)

    p = sub.add_parser("status", help="采集器自身状态")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("await", help="登记「我在等某人的回信」，回信来了归本会话")
    p.add_argument("--to", default="", help="对方姓名，从收件历史里认通道")
    p.add_argument("--cid", default="", help="直接给通道 id（--to 认不出时用）")
    p.add_argument("--session", default="",
                   help="登记给哪个会话，默认取 CLAUDE_CODE_SESSION_ID")
    p.add_argument("--hours", type=float, default=AWAIT_HOURS, help="守候多久")
    p.add_argument("--list", action="store_true", help="看现在在等谁")
    p.add_argument("--clear", action="store_true", help="清除（配 --to/--cid 清一条，不配清全部）")
    p.set_defaults(fn=cmd_await)

    p = sub.add_parser("await-reply",
                       help="登记 + 守候回信：盯 inbox，不自己轮询钉钉")
    p.add_argument("--to", default="", help="私聊对象姓名")
    p.add_argument("--group", default="", help="群名")
    p.add_argument("--cid", default="", help="直接给通道 id（--to/--group 认不出时用）")
    p.add_argument("--from", dest="from_who", default="",
                   help="只认这个人发的（姓名或 sender_id）。群里人多时盯一个人")
    p.add_argument("--session", default="",
                   help="登记给哪个会话，默认取 CLAUDE_CODE_SESSION_ID")
    p.add_argument("--hours", type=float, default=AWAIT_HOURS, help="守候多久")
    p.add_argument("--baseline", default="",
                   help="起算时间 'YYYY-MM-DD HH:MM:SS'，默认「现在」")
    p.add_argument("--interval", type=float, default=WATCH_INTERVAL_SECONDS,
                   help="多久看一次 inbox（读本地文件，不打接口）")
    p.add_argument("--stale-minutes", type=float, default=COLLECTOR_STALE_MINUTES,
                   help="采集器多久没动静就判「我在盯一个没人写的文件」并出声退出")
    p.add_argument("--no-register", action="store_true",
                   help="只守候不登记（本进程死了回信就回落默认路由）")
    p.add_argument("--out", default="", help="结果也写到这个文件")
    p.set_defaults(fn=cmd_await_reply)

    p = sub.add_parser("outbox",
                       help="拟好的回复落盘 + 推手机等他拍板（一期不发送）")
    p.add_argument("action", nargs="?", default="add",
                   choices=["add", "list", "show", "drop", "notify"])
    p.add_argument("--draft", default="", help="拟好的回复全文")
    p.add_argument("--to", default="", help="对方姓名（群用 --to 群名 加 --group）")
    p.add_argument("--group", action="store_true", help="--to 给的是群名")
    p.add_argument("--cid", default="", help="直接给通道 id")
    p.add_argument("--about-id", dest="about_id", default="",
                   help="在回哪条 inbox 消息（会自动取它的正文当上下文）")
    p.add_argument("--about-text", dest="about_text", default="",
                   help="他问了什么，不给就从 --about-id 取")
    p.add_argument("--session", default="", help="谁拟的，默认取本会话")
    p.add_argument("--id", default="", help="show / drop 用")
    p.add_argument("--why", default="", help="drop 的理由")
    p.add_argument("--no-notify", action="store_true",
                   help="只落盘不推（他就看不到，一般别用）")
    p.set_defaults(fn=cmd_outbox)

    args = ap.parse_args()
    return args.fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
