#!/usr/bin/env python3
"""fleet_up —— 把「这台机器上跑着哪些会话、每个 pane 里坐着谁」变成一份可版本化的声明，
并能照着它在一台空机器上重建出来。

这一层补的是 SYSTEM.md §7 的缺口：那一节能把**机器层**（三个常驻进程）装起来，
但**智能层**（tmux 拓扑 + 每个 pane 里的 Claude 是谁）此前一个字都没落盘——
新机器照着 repo 装完，得到的是零个 window。

五个子命令：

  setup     交互式流水线配置。一步一问、可中断、可重入，已完成的步骤自动跳过。
            扫码授权、填 id 这类**注定不能一键**的事都在这里，脚本停下来等你办完。
  up        照 yaml 把 tmux 拉起来。已存在的 session 默认跳过，不覆盖。
  check     yaml 与现实对账，列出差异。
  doctor    只读自检：依赖、授权、launchd 托管、角色文件缺没缺。
  capture   把**指定的** session 抓成 yaml，给已有拓扑做逆向存档用。

**范围红线：这份配置只描述 workOS 本体**——主会话、desk、值班、巡检、遥控这些
「不管你手上是什么项目都要有」的基础设施。具体项目的会话（某个 app、某条业务线）
不属于这里，那是干活时临时开的，随项目生灭。所以 `capture` 强制要求点名 session，
不给「一把梭全抓」的默认行为：全抓出来的是你今天的工作现场，不是这套系统本身。

配置文件沿用仓库既有约定（同 TRIAGE.md / _local-TRIAGE.md）：
  _local-fleet.yaml   本机真实拓扑，含真实项目名/路径，**gitignore 挡掉**
  fleet.example.yaml  仓库里的通用模板，不含任何真实名字
角色提示词同理：`_local-roles/<name>.md` 优先，回落 `roles/<name>.md`。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import yaml
except ImportError:
    sys.exit("需要 pyyaml：pip3 install pyyaml")

BASE = os.path.dirname(os.path.abspath(__file__))
LOCAL_CFG = os.path.join(BASE, "_local-fleet.yaml")
EXAMPLE_CFG = os.path.join(BASE, "fleet.example.yaml")
STATUS_JSON = os.path.expanduser("~/.claude/tmux-claude-status.json")

# capture 时用来判断「这个 pane 里坐着的是不是 Claude」。
# pane_current_command 对 Claude 会话显示的是版本号（node 改了进程标题），
# 形如 2.1.220 —— 但这只是**兜底**，主判据是 tmux-claude-status.json 里的 pane id，
# 那份是 hook 亲手写的，比进程标题可靠。
CLAUDE_CMD_RE = re.compile(r"^\d+\.\d+\.\d+$")

# up 时创建 pane 后等 shell 起来的时间。太短会把命令打进还没就绪的 shell。
SHELL_SETTLE = 0.35


# ---------------------------------------------------------------- tmux 基础

def tmux(*args: str, check: bool = True) -> str:
    """跑一条 tmux 命令，返回 stdout。check=False 时失败返回空串而不抛。"""
    p = subprocess.run(("tmux",) + args, capture_output=True, text=True)
    if p.returncode != 0:
        if check:
            raise RuntimeError(f"tmux {' '.join(args)} 失败：{p.stderr.strip()}")
        return ""
    return p.stdout


def tmux_ok() -> bool:
    return shutil.which("tmux") is not None


def live_sessions() -> list[str]:
    out = tmux("list-sessions", "-F", "#{session_name}", check=False)
    return [x for x in out.splitlines() if x]


def session_exists(name: str) -> bool:
    return name in live_sessions()


def send_line(pane: str, text: str) -> None:
    """把一行命令送进 pane 并回车。

    分两步、中间停一下：`send-keys -l` 送长文本时，如果紧接着送 Enter，
    回车可能先于文本到达，命令就卡在输入框里不执行（CLAUDE.md 里记过这个坑）。
    """
    tmux("send-keys", "-t", pane, "-l", text)
    time.sleep(0.4)
    tmux("send-keys", "-t", pane, "Enter")


# ---------------------------------------------------------------- 角色文件

def role_path(name: str) -> str | None:
    """`_local-roles/` 优先于 `roles/`，同 _local-TRIAGE.md 覆盖 TRIAGE.md 的规矩。"""
    for d in ("_local-roles", "roles"):
        p = os.path.join(BASE, d, f"{name}.md")
        if os.path.isfile(p):
            return p
    return None


def role_cmd(name: str, claude_bin: str) -> str:
    """启动一个带角色的 Claude。

    用 `claude "<首条消息>"` 而不是先起 claude 再 send-keys 喂 prompt ——
    后者要靠猜「claude 启动好了没」，前者由 claude 自己保证顺序。
    role 文件路径用 $(cat) 展开，避免把整段 prompt 塞进命令行导致引号地狱。
    """
    p = role_path(name)
    if not p:
        raise FileNotFoundError(
            f"角色 {name!r} 找不到定义：_local-roles/{name}.md 和 roles/{name}.md 都不存在"
        )
    rel = os.path.relpath(p, BASE)
    return f'{claude_bin} "$(cat {sh_quote(os.path.join(BASE, rel))})"'


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 配置读写

def load_cfg(path: str | None = None) -> dict:
    p = path or (LOCAL_CFG if os.path.isfile(LOCAL_CFG) else EXAMPLE_CFG)
    if not os.path.isfile(p):
        sys.exit(f"没有配置文件：{LOCAL_CFG}\n先跑 `fleet_up.py capture` 从当前 tmux 生成一份。")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("_path", p)
    return cfg


def expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def shrink(p: str) -> str:
    """写进 yaml 时把 $HOME 收成 ~，换台机器用户名不同也能用。"""
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home) else p


# ---------------------------------------------------------------- capture

def claude_panes() -> dict[str, dict]:
    """从 hook 写的状态文件里读「哪些 pane 是 Claude」。这是主判据。"""
    import json
    try:
        with open(STATUS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cmd_capture(args) -> int:
    if not tmux_ok():
        sys.exit("找不到 tmux")
    if not args.session and not args.all:
        sys.exit(
            "capture 必须点名要抓哪个 session：`--session OS`（可重复）。\n"
            "不给默认全抓，是因为全抓出来的是你今天的工作现场——具体项目的会话随项目生灭，\n"
            "不属于 workOS 本体。真要连项目会话一起存档才加 --all。\n"
            f"当前活着的：{' '.join(live_sessions()) or '（无）'}"
        )
    fmt = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{window_layout}"
    rows = [r.split("\t") for r in tmux("list-panes", "-a", "-F", fmt).splitlines() if r]

    known = claude_panes()
    sessions: dict[str, dict] = {}
    for sname, widx, wname, pidx, pid, pcmd, pcwd, wlayout in rows:
        if args.session and sname not in args.session:
            continue
        s = sessions.setdefault(sname, {"name": sname, "windows": {}})
        w = s["windows"].setdefault(
            widx, {"name": clean_window_name(wname), "layout": wlayout, "panes": []}
        )
        is_claude = pid in known or bool(CLAUDE_CMD_RE.match(pcmd))
        pane: dict = {"cwd": shrink(pcwd)}
        if is_claude:
            # 抓不出「这个 Claude 是谁」——角色只活在会话历史里，那正是本工具要补的。
            # 先标 claude: true，角色由人补进 role: 字段。
            pane["claude"] = True
        elif pcmd not in ("zsh", "bash", "sh", "fish"):
            pane["cmd"] = pcmd
        w["panes"].append(pane)

    out = {
        "version": 1,
        "sessions": [
            {
                "name": s["name"],
                "windows": [s["windows"][k] for k in sorted(s["windows"], key=int)],
            }
            for s in sessions.values()
        ],
    }

    text = yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=100)
    if args.out == "-":
        print(text)
        return 0
    dest = args.out or LOCAL_CFG
    if os.path.exists(dest) and not args.force:
        sys.exit(f"{dest} 已存在。要覆盖加 --force，或用 --out - 打到标准输出先看看。")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
    n_w = sum(len(s["windows"]) for s in out["sessions"])
    n_p = sum(len(w["panes"]) for s in out["sessions"] for w in s["windows"])
    n_c = sum(1 for s in out["sessions"] for w in s["windows"]
              for p in w["panes"] if p.get("claude"))
    print(f"已写入 {dest}")
    print(f"{len(out['sessions'])} 个 session / {n_w} 个 window / {n_p} 个 pane，其中 Claude {n_c} 个")
    print("下一步：给每个 claude: true 的 pane 补一个 role:，并在 _local-roles/ 下写它的角色文件。")
    return 0


def clean_window_name(n: str) -> str:
    """window 名前面那个状态图标是 hook 实时改的，不属于拓扑，抓的时候去掉。"""
    return re.sub(r"^[⠂⠐✳✱⣿◍●○·\s]+", "", n).strip() or n


# ---------------------------------------------------------------- up

def target_size() -> tuple[int, int]:
    """detached 建出来的 session 该用多大。

    不给 `-x/-y` 的话 tmux 按 80x24 建，而 `main-vertical` 的主 pane 默认就要 80 列——
    于是剩下的 pane 被压成 **1 列宽**，命令送进去照常执行，但显示成一列一个字，
    看上去像「没送进去」。等你 attach 上来 tmux 也不会自动重排，那份畸形布局会留着。
    （2026-08-06 实测：78x24 / 1x12 / 1x11。）

    有 client 连着就照它的尺寸，没有就给一个够宽的默认值。
    """
    out = tmux("list-clients", "-F", "#{client_width}x#{client_height}", check=False)
    for line in out.splitlines():
        try:
            w, h = line.split("x")
            if int(w) >= 80 and int(h) >= 24:
                return int(w), int(h)
        except ValueError:
            continue
    return 280, 80


def pane_ids(target: str) -> list[str]:
    """按 pane_index 顺序拿到 pane-id（%NN）。

    定位一律用 pane-id，不用 `session:window.index`——window 名可以带点号、
    可以被 hook 实时改（状态图标就是这么加的），名字路径随时会指错地方。
    SYSTEM.md §4 也是这么定的。
    """
    out = tmux("list-panes", "-t", target, "-F", "#{pane_index}\t#{pane_id}", check=False)
    rows = []
    for line in out.splitlines():
        if "\t" in line:
            idx, pid = line.split("\t", 1)
            rows.append((int(idx), pid))
    return [pid for _, pid in sorted(rows)]


def build_window(sess: str, w: dict, first: bool, claude_bin: str, dry: bool) -> None:
    wname = w.get("name") or ""
    panes = w.get("panes") or [{}]
    cwd0 = expand(panes[0].get("cwd") or w.get("cwd") or "~")

    def run(*a: str) -> None:
        if dry:
            print("  tmux " + " ".join(a))
        else:
            tmux(*a)

    if first:
        cols, rows = target_size()
        run("new-session", "-d", "-s", sess, "-n", wname, "-c", cwd0,
            "-x", str(cols), "-y", str(rows))
    else:
        run("new-window", "-t", f"{sess}:", "-n", wname, "-c", cwd0)
    target = f"{sess}:{wname}"

    for p in panes[1:]:
        run("split-window", "-t", target, "-c", expand(p.get("cwd") or cwd0))

    layout = w.get("layout") or "tiled"
    run("select-layout", "-t", target, layout)

    ids = [] if dry else pane_ids(target)
    for i, p in enumerate(panes):
        cmd = None
        if p.get("role"):
            cmd = role_cmd(p["role"], claude_bin)
        elif p.get("claude"):
            cmd = claude_bin
        elif p.get("cmd"):
            cmd = p["cmd"]
        if not cmd:
            continue
        if dry:
            print(f"  send-keys -t <{target} 第 {i + 1} 个 pane> :: {cmd}")
            continue
        if i >= len(ids):
            print(f"  警告：{target} 只建出 {len(ids)} 个 pane，第 {i + 1} 个的命令没送",
                  file=sys.stderr)
            continue
        time.sleep(SHELL_SETTLE)
        send_line(ids[i], cmd)


def cmd_up(args) -> int:
    if not tmux_ok():
        sys.exit("找不到 tmux")
    cfg = load_cfg(args.config)
    claude_bin = (cfg.get("defaults") or {}).get("claude_cmd", "claude")

    # 先把所有角色文件查一遍再动手。缺一个就整体不动，
    # 免得建到一半发现角色缺失，留下半拉子 session 要人手工收拾。
    missing = []
    for s in cfg.get("sessions") or []:
        if args.session and s["name"] not in args.session:
            continue
        for w in s.get("windows") or []:
            for p in w.get("panes") or []:
                if p.get("role") and not role_path(p["role"]):
                    missing.append(p["role"])
    if missing:
        for r in sorted(set(missing)):
            print(f"缺角色文件：_local-roles/{r}.md（或 roles/{r}.md）", file=sys.stderr)
        return 2

    built = skipped = 0
    for s in cfg.get("sessions") or []:
        name = s["name"]
        if args.session and name not in args.session:
            continue
        if session_exists(name):
            print(f"跳过 {name}：已存在（本工具不覆盖活着的 session）")
            skipped += 1
            continue
        print(f"{'[dry-run] ' if args.dry_run else ''}建 {name} …")
        for i, w in enumerate(s.get("windows") or []):
            build_window(name, w, first=(i == 0), claude_bin=claude_bin, dry=args.dry_run)
        built += 1

    print(f"\n完成：新建 {built} 个 session，跳过 {skipped} 个。")
    if built and not args.dry_run:
        print("Claude pane 需要几十秒启动并读完角色提示词，`fleet_up.py check` 可以对账。")
    return 0


# ---------------------------------------------------------------- check

def live_windows(sess: str) -> set[str]:
    out = tmux("list-windows", "-t", sess, "-F", "#{window_name}", check=False)
    return {clean_window_name(x) for x in out.splitlines() if x}


def session_diff(cfg: dict, name: str) -> tuple[str, str]:
    """一个 session 的声明与现实对账，返回 (状态, 说明)。

    只检查「声明的 window 在不在」，**不管多出来的**。同一个 tmux session 里
    除了 workOS 本体的窗口，通常还开着一堆随项目生灭的窗口（临时排查、某个仓库的
    开发窗口）——那些本来就不该进配置，按 window 数量比对会永远报差异，
    等于这个检查天天喊狼来了，久了就没人看了。
    """
    want = set()
    for s in cfg.get("sessions") or []:
        if s["name"] == name:
            want |= {w.get("name") for w in (s.get("windows") or []) if w.get("name")}
    live = live_windows(name)
    missing = sorted(want - live)
    extra = len(live - want)
    if missing:
        return "差异", f"缺 window：{'、'.join(missing)}" + (f"（另有 {extra} 个额外窗口，正常）" if extra else "")
    return "OK", f"{len(want)} 个声明窗口都在" + (f"，另有 {extra} 个额外窗口" if extra else "")


def cmd_check(args) -> int:
    cfg = load_cfg(args.config)
    live = set(live_sessions())
    want = {s["name"] for s in cfg.get("sessions") or []}
    print(f"配置：{cfg['_path']}")
    for n in sorted(want | live):
        if n in want and n in live:
            mark, note = session_diff(cfg, n)
            print(f"  {mark:<4} {n}  {note}")
        elif n in want:
            print(f"  缺   {n}（配置里有，机器上没有 → fleet_up.py up --session {n}）")
        else:
            print(f"  野生 {n}（机器上有、配置里没有——项目会话通常就该是这样）")
    return 0


# ---------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'x' if good else ' '}] {label}{'  ' + detail if detail else ''}")

    print("依赖")
    line(tmux_ok(), "tmux")
    line(shutil.which("claude") is not None, "claude")
    line(shutil.which("dws") is not None, "dws", "钉钉官方 CLI，缺就 npm i -g dingtalk-workspace-cli")

    print("\n配置")
    line(os.path.isfile(LOCAL_CFG), "_local-fleet.yaml", "缺就跑 capture")
    line(os.path.isfile(os.path.join(BASE, "config.json")), "config.json",
         "缺就 cp config.example.json config.json 再填")

    print("\n角色文件")
    if os.path.isfile(LOCAL_CFG):
        cfg = load_cfg()
        roles, noroles = set(), 0
        for s in cfg.get("sessions") or []:
            for w in s.get("windows") or []:
                for p in w.get("panes") or []:
                    if p.get("role"):
                        roles.add(p["role"])
                    elif p.get("claude"):
                        noroles += 1
        for r in sorted(roles):
            line(role_path(r) is not None, r)
        if noroles:
            print(f"  ({noroles} 个 Claude pane 还没指定 role，会起成空白会话)")
        if not roles and not noroles:
            print("  (配置里没有 Claude pane)")
    else:
        print("  (没有配置，跳过)")

    print("\n服务（launchd）")
    svcs = load_svc()
    if not svcs:
        print(f"  (没有 services 配置，模板在 {os.path.basename(EXAMPLE_SVC)})")
    for s in svcs:
        name = s.get("name", "?")
        st, note = svc_status(s)
        # push-loop 只在 remote on（出门遥控）时才该跑，桌前不跑是正常的，不判失败
        if s.get("label", "").endswith(".push"):
            print(f"  (-) {name}  {'在跑' if st in ('OK', '托管') else '没跑（桌前正常）'}")
            continue
        line(st in ("OK", "托管"), name, note or (f"状态：{st}" if st != "OK" else ""))

    # 机器上跑着、但任何配置都没提到的 launchd 任务：换台机器不会自动有。
    # 这是「部署缺口」，不是「坏了」，所以只提示不判失败。
    declared = {s.get("label") for s in svcs}
    loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    stray: list[str] = []
    for ln in loaded.splitlines():
        parts = ln.split("\t")
        label = parts[-1].strip() if parts else ""
        if label.startswith("com.workos.") and label not in declared:
            stray.append(label)
    for label in stray:
        print(f"  (?) {label}  在跑，但没有任何配置声明它 —— 换机器不会自动有")

    print("\n" + ("自检通过。" if ok else "有缺项，见上面未打勾的。"))
    return 0 if ok else 1


# ---------------------------------------------------------------- services（定时/常驻）

LOCAL_SVC = os.path.join(BASE, "_local-services.yaml")
EXAMPLE_SVC = os.path.join(BASE, "services.example.yaml")
LA_DIR = os.path.expanduser("~/Library/LaunchAgents")


def load_svc(path: str | None = None) -> list[dict]:
    p = path or (LOCAL_SVC if os.path.isfile(LOCAL_SVC) else EXAMPLE_SVC)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return ((yaml.safe_load(f) or {}).get("services")) or []


def launchd_loaded(label: str) -> bool:
    """直接查这个 label。

    别用 `launchctl list | grep -q`：grep 命中就退出会给 launchctl 一个 SIGPIPE，
    配上 pipefail 会假报「未加载」——run.sh 里为这个坑写过一大段注释。
    """
    return subprocess.run(["launchctl", "list", label],
                          capture_output=True).returncode == 0


def expand_weekdays(spec) -> list[int]:
    """`1-5` / `[1,3,5]` / `5` 都收。1=周一，0/7=周日（launchd 的约定）。"""
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, list):
        return [int(x) for x in spec]
    s = str(spec)
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",") if x.strip()]


def abspath(p: str) -> str:
    """展开 ~/$VAR，并把相对路径按**仓库目录**解析成绝对路径。

    plist 里放相对路径是无声的坑：launchd 的工作目录是 `/`，`./data/x.out` 会被
    解析成 `/data/x.out`，写不进去、进程照常起、日志一个字没有，你以为任务没跑。
    （2026-08-06 实测：装了个 `/bin/echo` 的测试服务，日志文件始终是空的。）
    所以这一层写出去的路径一律是绝对的。
    """
    e = expand(str(p))
    return e if os.path.isabs(e) else os.path.normpath(os.path.join(BASE, e))


def svc_plist(s: dict) -> dict:
    """把一条 service 声明翻成 launchd plist 的 dict。"""
    label = s["label"]
    # cmd[0] 是程序路径，同样不能是相对的；后面的参数原样保留（可能就是字面量）
    cmd = [str(x) for x in s["cmd"]]
    cmd = [abspath(cmd[0])] + [expand(str(x)) for x in cmd[1:]]
    d: dict = {"Label": label, "ProgramArguments": cmd}

    if s.get("cwd"):
        d["WorkingDirectory"] = abspath(s["cwd"])
    if s.get("log"):
        d["StandardOutPath"] = abspath(s["log"])
        d["StandardErrorPath"] = abspath(s.get("log_err") or s["log"])
    if s.get("keepalive"):
        d["KeepAlive"] = True
    d["RunAtLoad"] = bool(s.get("run_at_load", s.get("keepalive", False)))
    if s.get("interval"):
        d["StartInterval"] = int(s["interval"])

    cal = s.get("calendar")
    if cal:
        out = []
        for c in cal if isinstance(cal, list) else [cal]:
            base = {}
            if "hour" in c:
                base["Hour"] = int(c["hour"])
            if "minute" in c:
                base["Minute"] = int(c["minute"])
            if "day" in c:
                base["Day"] = int(c["day"])
            if "weekday" in c:
                for wd in expand_weekdays(c["weekday"]):
                    out.append(dict(base, Weekday=wd))
            else:
                out.append(base)
        d["StartCalendarInterval"] = out

    # launchd 给的 PATH 是最小集（/usr/bin:/bin:/usr/sbin:/sbin），homebrew 装的东西
    # 一律找不到。不显式补 PATH 的服务，跑起来会以「command not found」静默失败。
    env = dict(s.get("env") or {})
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:"
                           f"{os.path.expanduser('~/.local/bin')}:/usr/bin:/bin:/usr/sbin:/sbin")
    env.setdefault("HOME", os.path.expanduser("~"))
    d["EnvironmentVariables"] = {k: expand(str(v)) for k, v in env.items()}
    return d


def svc_status(s: dict) -> tuple[str, str]:
    """返回 (状态标记, 说明)。"""
    label = s.get("label")
    if s.get("owner"):
        loaded = launchd_loaded(label) if label else False
        return ("托管" if loaded else "未装"), f"由 {s['owner']} 安装，本层不接管"
    if not label:
        return "配置错", "缺 label"
    if not launchd_loaded(label):
        return "未装", ""
    # 装了，再看 plist 内容跟声明对不对得上——漂移比没装更难查
    p = os.path.join(LA_DIR, f"{label}.plist")
    if not os.path.isfile(p):
        return "不一致", "launchd 里加载着，但 ~/Library/LaunchAgents 下没有对应 plist"
    try:
        import plistlib
        with open(p, "rb") as f:
            live = plistlib.load(f)
        want = svc_plist(s)
        for k in ("ProgramArguments", "StartCalendarInterval", "StartInterval",
                  "KeepAlive", "WorkingDirectory", "StandardOutPath"):
            if live.get(k) != want.get(k):
                return "漂移", f"{k} 与声明不一致"
    except Exception as e:
        return "读不了", str(e)
    return "OK", ""


def svc_install(s: dict, dry: bool = False) -> bool:
    import plistlib
    label = s["label"]
    path = os.path.join(LA_DIR, f"{label}.plist")
    d = svc_plist(s)
    if dry:
        print(f"    会写 {path}")
        print(f"      {d['ProgramArguments']}")
        return True
    os.makedirs(LA_DIR, exist_ok=True)
    for key in ("StandardOutPath", "StandardErrorPath"):
        if d.get(key):
            os.makedirs(os.path.dirname(d[key]), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(d, f)
    uid = os.getuid()
    # 先 bootout 再 bootstrap：重复 bootstrap 同一个 label 会报 5:Input/output error，
    # 而且旧的定义还留在内存里，改了 plist 也不生效。
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print(f"    bootstrap 失败：{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return False
    return True


def svc_uninstall(s: dict) -> bool:
    label = s["label"]
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    path = os.path.join(LA_DIR, f"{label}.plist")
    if os.path.isfile(path):
        os.remove(path)
    return not launchd_loaded(label)


def cmd_services(args) -> int:
    svcs = load_svc(args.config)
    if not svcs:
        print(f"没有 services 配置。模板在 {EXAMPLE_SVC}，"
              f"复制成 _local-services.yaml 再改。")
        return 1

    pick = set(args.name or [])
    sel = [s for s in svcs if not pick or s.get("name") in pick]
    if pick and not sel:
        sys.exit(f"没有叫这些名字的服务：{' '.join(sorted(pick))}\n"
                 f"有的是：{' '.join(s.get('name', '?') for s in svcs)}")

    if args.action in ("list", "status"):
        w = max(len(s.get("name", "?")) for s in sel)
        for s in sel:
            st, note = svc_status(s)
            when = (f"每 {s['interval']}s" if s.get("interval")
                    else "常驻" if s.get("keepalive")
                    else fmt_calendar(s.get("calendar")) if s.get("calendar")
                    else "手动")
            print(f"  {st:<5} {s.get('name', '?'):<{w}}  {when:<18} {note}")
        return 0

    if args.action == "install":
        n = 0
        for s in sel:
            name = s.get("name", "?")
            if s.get("owner"):
                print(f"  跳过 {name}：{s['owner']} 装的，本层不碰"
                      f"（重复装会变成两份进程同时跑）")
                continue
            print(f"  装 {name} …")
            if svc_install(s, dry=args.dry_run):
                n += 1
        print(f"\n{'[dry-run] ' if args.dry_run else ''}处理 {n} 个。")
        return 0

    if args.action == "uninstall":
        for s in sel:
            if s.get("owner"):
                print(f"  跳过 {s.get('name')}：{s['owner']} 装的，用它自己的命令卸")
                continue
            if not pick and not args.yes:
                sys.exit("卸载全部要加 --yes，或者点名 `services uninstall <名字>`")
            print(f"  卸 {s.get('name')} … {'OK' if svc_uninstall(s) else '失败'}")
        return 0
    return 0


def fmt_calendar(cal) -> str:
    if not cal:
        return ""
    items = cal if isinstance(cal, list) else [cal]
    out = []
    for c in items:
        wd = c.get("weekday")
        t = f"{int(c.get('hour', 0)):02d}:{int(c.get('minute', 0)):02d}"
        out.append(f"周{wd} {t}" if wd is not None else t)
    return "、".join(out)


# ---------------------------------------------------------------- setup 流水线

def ask(prompt: str, default: str = "y") -> bool:
    """y/n 询问。非交互终端（管道里跑）一律取默认值，不阻塞。"""
    if not sys.stdin.isatty():
        return default == "y"
    hint = "Y/n" if default == "y" else "y/N"
    try:
        a = input(f"    {prompt} [{hint}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("已中断。setup 可重入，下次接着跑就行。")
    return default == "y" if not a else a.startswith("y")


def pause(prompt: str) -> None:
    if not sys.stdin.isatty():
        return
    try:
        input(f"    {prompt}（办完按回车，Ctrl-C 退出）")
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("已中断。setup 可重入，下次接着跑就行。")


class Step:
    """流水线里的一步：先自检，已完成就打勾跳过，没完成才动手。

    每步都必须能重复跑而不出事——setup 的价值在于任何时候中断都能接着来，
    不是「一口气走完否则从头再来」。
    """

    title = ""
    manual = False   # True = 注定要人工介入（扫码、填 id），脚本只负责停下来讲清楚

    def done(self) -> bool:
        raise NotImplementedError

    def do(self) -> bool:
        raise NotImplementedError

    def why(self) -> str:
        return ""


class StepDeps(Step):
    title = "依赖：tmux / claude / dws"

    def _missing(self) -> list[str]:
        want = {
            "tmux": "brew install tmux",
            "claude": "见 claude.com/claude-code 安装",
            "dws": "npm i -g dingtalk-workspace-cli",
        }
        return [f"{k}（{v}）" for k, v in want.items() if not shutil.which(k)]

    def done(self) -> bool:
        return not self._missing()

    def do(self) -> bool:
        print("    缺这些，装完再回来：")
        for m in self._missing():
            print(f"      - {m}")
        pause("装好了吗")
        return self.done()


class StepAuth(Step):
    title = "dws 授权（扫码，必须人工）"
    manual = True

    def done(self) -> bool:
        if not shutil.which("dws"):
            return False
        p = subprocess.run(["dws", "auth", "status"], capture_output=True, text=True, timeout=20)
        return p.returncode == 0 and "未登录" not in (p.stdout + p.stderr)

    def why(self) -> str:
        return "钉钉的授权是扫码换 token，没有任何办法自动化。管理员还得先在 open-dev 开「CLI 访问管理」。"

    def do(self) -> bool:
        if not ask("现在跑 `dws auth login` 扫码？"):
            return False
        subprocess.run(["dws", "auth", "login"])
        return self.done()


class StepConfig(Step):
    title = "config.json（本机 id 和路由表，必须人工）"
    manual = True
    path = os.path.join(BASE, "config.json")

    def done(self) -> bool:
        return os.path.isfile(self.path)

    def why(self) -> str:
        return "里面是你自己的 open_dingtalk_id / user_id / 关注哪些群 / 消息路由到哪个会话，换个人全不一样。"

    def do(self) -> bool:
        ex = os.path.join(BASE, "config.example.json")
        if not os.path.isfile(self.path) and os.path.isfile(ex):
            if ask("从 config.example.json 复制一份出来？"):
                shutil.copy(ex, self.path)
                print(f"    已生成 {self.path}")
        print(f"    用编辑器打开填一下：{self.path}")
        pause("填好了吗")
        return self.done()


class StepHooks(Step):
    title = "Claude hooks（会话心跳，写进 ~/.claude/settings.json）"
    path = os.path.expanduser("~/.claude/settings.json")

    def done(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                return "dtcc.py" in f.read()
        except OSError:
            return False

    def why(self) -> str:
        return "没有它，每个 Claude 会话收尾时不上报心跳，board 和唤醒就都是瞎子。"

    def do(self) -> bool:
        ex = os.path.join(BASE, "settings.example.json")
        print(f"    把 {ex} 里的 hooks 段合并进 {self.path}")
        print("    （是合并不是覆盖——那个文件里还有你自己的 env / permissions / statusLine）")
        pause("合好了吗")
        return self.done()


class StepLaunchd(Step):
    title = "常驻采集器（launchd 托管，开机自启）"

    def done(self) -> bool:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return "dtwatch.poll" in out and "dtwatch.at" in out

    def do(self) -> bool:
        if not ask("现在跑 `./run.sh install 300` 装上？"):
            return False
        subprocess.run(["./run.sh", "install", "300"], cwd=BASE)
        return self.done()


class StepFleetCfg(Step):
    title = "_local-fleet.yaml（本机要开哪些 workOS 会话）"

    def done(self) -> bool:
        return os.path.isfile(LOCAL_CFG)

    def why(self) -> str:
        return "仓库里的 fleet.example.yaml 只描述 workOS 本体；本机版由你按自己的路径改。"

    def do(self) -> bool:
        if not os.path.isfile(EXAMPLE_CFG):
            print(f"    没有 {EXAMPLE_CFG}，跳过")
            return False
        if not ask("从 fleet.example.yaml 复制一份 _local-fleet.yaml？"):
            return False
        shutil.copy(EXAMPLE_CFG, LOCAL_CFG)
        print(f"    已生成 {LOCAL_CFG}，按需要改里面的 cwd 和 window 组成。")
        return self.done()


class StepRoles(Step):
    title = "角色文件（每个 Claude pane 开机后是谁）"

    def _missing(self) -> list[str]:
        if not os.path.isfile(LOCAL_CFG):
            return []
        cfg = load_cfg()
        out = []
        for s in cfg.get("sessions") or []:
            for w in s.get("windows") or []:
                for p in w.get("panes") or []:
                    if p.get("role") and not role_path(p["role"]):
                        out.append(p["role"])
        return sorted(set(out))

    def done(self) -> bool:
        return os.path.isfile(LOCAL_CFG) and not self._missing()

    def do(self) -> bool:
        m = self._missing()
        if not m:
            return True
        print("    这些角色在配置里被引用，但找不到定义文件：")
        for r in m:
            print(f"      - _local-roles/{r}.md（或 roles/{r}.md）")
        pause("补好了吗")
        return self.done()


class StepUp(Step):
    title = "把会话拉起来"

    def done(self) -> bool:
        if not os.path.isfile(LOCAL_CFG):
            return False
        cfg = load_cfg()
        want = [s["name"] for s in cfg.get("sessions") or []]
        return bool(want) and all(session_exists(n) for n in want)

    def do(self) -> bool:
        cfg = load_cfg()
        want = [s["name"] for s in cfg.get("sessions") or []]
        todo = [n for n in want if not session_exists(n)]
        print(f"    要建：{' '.join(todo)}")
        if not ask("现在建？"):
            return False
        ns = argparse.Namespace(session=todo, config=None, dry_run=False)
        cmd_up(ns)
        return self.done()


SETUP_STEPS = [
    StepDeps, StepAuth, StepConfig, StepHooks,
    StepLaunchd, StepFleetCfg, StepRoles, StepUp,
]


def cmd_setup(args) -> int:
    steps = [c() for c in SETUP_STEPS]
    print("workOS 配置流水线")
    print("每步先自检，已完成的直接跳过；任何时候 Ctrl-C 都能退，下次接着跑。\n")

    # 先整体过一遍状态，让人一眼看见还差几步，而不是走一步看一步。
    pending = []
    for i, s in enumerate(steps, 1):
        try:
            d = s.done()
        except Exception as e:
            d = False
            print(f"  {i}. [?] {s.title}  自检出错：{e}")
            continue
        print(f"  {i}. [{'x' if d else ' '}] {s.title}{'  ← 需人工' if s.manual and not d else ''}")
        if not d:
            pending.append((i, s))
    if not pending:
        print("\n全部就绪，没什么要办的。")
        return 0

    print(f"\n还差 {len(pending)} 步。逐个来。\n")
    stuck = []
    for i, s in pending:
        print(f"── {i}. {s.title}")
        if s.why():
            print(f"    {s.why()}")
        try:
            ok_ = s.do()
        except SystemExit:
            raise
        except Exception as e:
            print(f"    出错：{e}")
            ok_ = False
        if ok_:
            print("    ✓ 完成\n")
        else:
            stuck.append(s.title)
            print("    — 没完成，先跳过\n")

    if stuck:
        print("还没办完的：")
        for t in stuck:
            print(f"  - {t}")
        print("\n办完再跑一次 `fleet_up.py setup`，已完成的会自动跳过。")
        return 1
    print("全部完成。`fleet_up.py check` 可以对账。")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("setup", help="交互式流水线配置（可中断、可重入）")
    t.set_defaults(func=cmd_setup)

    c = sub.add_parser("capture", help="把指定 session 抓成 yaml")
    c.add_argument("--session", "-s", action="append", help="要抓哪些 session，可重复；必填")
    c.add_argument("--all", action="store_true", help="连项目会话一起抓（默认不给这个行为）")
    c.add_argument("--out", "-o", help="输出路径，- 表示标准输出")
    c.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    c.set_defaults(func=cmd_capture)

    u = sub.add_parser("up", help="照 yaml 建 tmux（已存在的 session 会跳过）")
    u.add_argument("--session", "-s", action="append", help="只建这些 session，可重复")
    u.add_argument("--config", "-c", help="指定配置文件")
    u.add_argument("--dry-run", "-n", action="store_true", help="只打印要跑的 tmux 命令")
    u.set_defaults(func=cmd_up)

    k = sub.add_parser("check", help="yaml 与现实对账")
    k.add_argument("--config", "-c", help="指定配置文件")
    k.set_defaults(func=cmd_check)

    d = sub.add_parser("doctor", help="开机自检")
    d.set_defaults(func=cmd_doctor)

    v = sub.add_parser("services", help="定时/常驻任务（launchd）")
    v.add_argument("action", choices=["list", "status", "install", "uninstall"])
    v.add_argument("name", nargs="*", help="只处理这些服务，不给就是全部")
    v.add_argument("--config", "-c", help="指定配置文件")
    v.add_argument("--dry-run", "-n", action="store_true", help="只说要做什么，不动手")
    v.add_argument("--yes", action="store_true", help="卸载全部时需要")
    v.set_defaults(func=cmd_services)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
