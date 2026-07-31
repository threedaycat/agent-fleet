#!/usr/bin/env python3
"""watch_done —— 盯「会话干完活但没喊人」这个洞。

事件日志（events.ndjson）只在会话**自觉写标记**时才会惊动值班/主会话。
可实际上最容易漏的恰好是它不自觉的时候：一轮活干完了，在自己 pane 里
提了个建议、问了个问题、或者摆出三个待办等人点头，却没写任何标记 ——
那些字就烂在那个 pane 里，谁都不知道有人在等。

这个部件不依赖会话自觉。它只看一件客观事实：tmux 状态文件里某个会话的
status 从 running/input 变成了 done。一变，就把它最后那段 pane 输出抓下来，
交给值班会话去判断「这里面有没有人在等」。

  用法：python3 watch_done.py            # 前台跑，每行输出是一个 done 事件
  停：  Ctrl-C 或 kill

输出契约：每个 done 事件先一行 `DONE| <sid> <会话/窗口> pane=%NN ...`，
随后缩进若干行是 pane 尾部摘要，最后一行给出全文快照的路径。

—— 一个补不了的洞，写在这儿别装作没有 ——
状态文件不是全量快照，它只保留「最近有活动的 pane」，会被整体重建
（实测有过一次从 23 条掉到 2 条）。所以一个会话如果在不在表里的时候进了
done，这个脚本就看不见它，而且**无法从消费端补救** —— 要补得在生产端
（写状态文件的那个 hook）保证 done 这个终态一定落一次。已知局限，不是 bug。
"""
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATUS = os.path.join(HOME, ".claude", "tmux-claude-status.json")

# data/ 整个不进版本库，所以状态和快照都放这儿：既不会被打包带走，
# 也不会在仓库脱敏/重写历史时被卷进去。
# 默认就是脚本旁边的 data/（脚本和其它部件同级放在仓库根）。
# WATCH_DONE_DATA 只为「脚本还没就位、但要先跑起来」这种过渡情况留的口子，
# 平时不用设 —— 也因此这里不写死任何绝对路径。
DATA = os.environ.get("WATCH_DONE_DATA") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
SEEN_FILE = os.path.join(DATA, "watch_done_seen.json")
CAPDIR = os.path.join(DATA, "panecaps")

# 不该监听的本机会话 id 前缀（8 位就够）：
#   第一个 = 值班会话自己 —— 收自己的收尾会自激振荡，越报越多
#   第二个 = 主会话       —— 它是上报的接收端，不是监控对象
# 换机器/换会话就改这两个值，脚本别的地方不用动。
SKIP_SID_PREFIXES = ("ae69f4e5", "53803776")

INTERVAL = 15          # 秒。10~20 秒足够：done 是个稳定终态，不会一闪而过
EXCERPT_LINES = 30     # pane 尾部摘要保留多少行（全文另存快照）
LINE_CAP = 220         # 单行截断，避免一条通知糊满屏幕
KEEP_CAPS = 200        # 快照最多留多少个
KEEP_DAYS = 7          # 且只留最近多少天 —— 两个上限同时生效，先到先剪


def load_status():
    """状态文件随时可能正被重写，读失败就当这一拍没数据，下一拍再来。"""
    try:
        with open(STATUS) as f:
            return json.load(f)
    except Exception:
        return {}


def load_seen():
    """已报过的 done 落盘，重启不重报 —— 内存态的版本每次重挂都会把
    当时所有 done 会话重报一遍，那是纯噪音。"""
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_seen(seen):
    """临时文件 + 原子替换：读的人永远看不到写了一半的 JSON。"""
    tmp = SEEN_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(seen, f, ensure_ascii=False)
        os.replace(tmp, SEEN_FILE)
    except Exception:
        pass


def prune_caps():
    """快照目录只会长不会短，加个双上限，别让它无限堆。"""
    try:
        names = [n for n in os.listdir(CAPDIR) if n.endswith(".txt")]
    except Exception:
        return
    entries = []
    for n in names:
        p = os.path.join(CAPDIR, n)
        try:
            entries.append((os.path.getmtime(p), p))
        except OSError:
            pass
    entries.sort(reverse=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    for i, (mtime, path) in enumerate(entries):
        if i >= KEEP_CAPS or mtime < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass


def capture(pane):
    """连滚屏一起抓：pane 可见区往往只剩最后几行，判断不了它在等什么。"""
    if not pane or not pane.startswith("%"):
        # 只认稳定 pane-id。传 session:window 名会在改名后静默打空，
        # 空 target 更糟 —— tmux 会打到当前活动 pane 去。
        return ""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", pane, "-p", "-S", "-120"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def emit(info, text, capfile):
    sid = (info.get("session_id") or "")[:8]
    name = "%s/%s" % (info.get("session", "?"), info.get("window_name", "?"))
    read = " [已读]" if info.get("read") else ""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    print("DONE| %s %s pane=%s%s cwd=%s" % (
        sid, name, info.get("pane"), read, info.get("cwd", "")))
    for l in lines[-EXCERPT_LINES:]:
        print("    " + (l if len(l) <= LINE_CAP else l[:LINE_CAP] + "…"))
    print("    ---- 全文: %s" % capfile)
    sys.stdout.flush()


def done_records(data):
    for pane, info in data.items():
        if info.get("status") != "done":
            continue
        sid = info.get("session_id") or pane
        if sid[:8] in SKIP_SID_PREFIXES:
            continue
        yield sid, info


def main():
    os.makedirs(CAPDIR, exist_ok=True)
    seen = load_seen()          # sid -> 上次报过的那次 done 的 updated_at
    first_run = not os.path.exists(SEEN_FILE)

    if first_run:
        # 头一次跑：把当下已经 done 的会话记下来但**不逐个抓 pane** ——
        # 那会一口气刷出一屏历史。只报一行「谁本来就在 done」，
        # 真要看哪个，人工去 capture 一次就行。
        data = load_status()
        stale = []
        for sid, info in done_records(data):
            seen[sid] = info.get("updated_at")
            stale.append("%s(%s)" % (sid[:8], info.get("window_name", "?")))
        save_seen(seen)
        print("DONE| 首次装载，已把当下 %d 个 done 会话记为已知、不逐条抓取：%s"
              % (len(stale), " ".join(stale) if stale else "无"))
        sys.stdout.flush()

    while True:
        data = load_status()
        changed = False
        for sid, info in done_records(data):
            stamp = info.get("updated_at")
            if seen.get(sid) == stamp:
                continue
            seen[sid] = stamp
            changed = True
            text = capture(info.get("pane", ""))
            if not text.strip():
                continue
            capfile = os.path.join(
                CAPDIR, "%s-%s.txt" % (sid[:8], int(stamp or 0)))
            try:
                with open(capfile, "w") as f:
                    f.write(text)
            except Exception:
                capfile = "(快照写盘失败)"
            emit(info, text, capfile)
        if changed:
            save_seen(seen)
            prune_caps()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
