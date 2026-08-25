"""屏幕抓取层 —— 唯一跟 tmux 打交道的地方，并且**假设它会说谎**。

从 fleet.py 抽出来的。抽的理由是可量化的：这个仓库 25 个 commit 里 9 个是
fix，其中 8 个（32%）全落在同一件事上 —— 用 `capture-pane` 的屏幕文本去猜
另一个进程的状态。屏幕不是接口，是一块给人看的字符画，它至少有五种说谎方式：

  1. 旧帧      抓到的是上一次渲染的画面                      95225e2
  2. 回显      自己刚打进去的关键词被当成对方的输出          c080b6e
  3. 滚动区    读到的页脚是历史里的，不是当前的              b466a64
  4. 竞态      长文本还没送完回车就到了，任务躺在输入框里    5f05ec1
  5. 打错 pane 读/写的压根不是目标进程 —— **返回码骗你**    见下

第 5 种跟前四种不是一类：前四种是「读到的文本不对」，第 5 种是
`tmux send-keys` 打到别人窗口**也返回 0**。事故原文（`~/.claude/CLAUDE.md`，
2026-08-01 凌晨）：编队四个会话各自送 `/compact`，四个全部报告「已送达」，
四个页脚一个没降（336k/342k/284k/342k），然后各自空转。根因是用
`tmux display-message -p '#{pane_id}'` 取「自己」的 pane —— 那个命令返回的是
**当前聚焦的** pane：某会话实际在 `%44`，拿到 `%27`，`%27` 是个普通 zsh，
于是 `/compact 保留：…` 被当 shell 命令执行，`zsh: parse error near ')'`，
退出码 127。打到哪全看用户当时聚焦在哪。

所以这个模块的两条结构约束：

  A. **pane 身份只能显式传进来。** 这里故意**不提供**任何「取当前 pane」的
     函数 —— 让第 5 种在类型上就写不出来。要自己的 pane，读 `$TMUX_PANE`
     （tmux 在每个 pane 自己的 shell 里设好的），那是调用方的事，不是这里的。
  B. **解析和 IO 分开。** `parse_*` 只吃字符串、不碰 tmux，所以可以喂真实
     抓到过的屏幕文本做测试；IO 在 `ScreenSource` 后面，有真假两个实现。

行为跟抽出来之前逐字一致 —— 这是纯重构，不修任何判据。
"""

import re
import subprocess
import time
from typing import List, Optional, Protocol, Sequence, Tuple

# 送文本的时序参数。都是实测值，不是拍的。
MAX_ENTER_RETRIES = 3      # 回车补发上限——宁可放弃也别死循环往人家输入框塞字
ENTER_RETRY_WAIT = 0.6     # 每次补发后等多久再回读
BASE_SEND_DELAY = 0.4      # 短文本的基础延时
PER_CHAR_DELAY = 0.001     # 每个字符多等多久（长文本按长度线性加）
MAX_SEND_DELAY = 3.0       # 延时上限，别为了超长文本无限等
STABLE_GAP = 0.4           # 两次抓屏的间隔

# 提交成功、对方正忙、消息已排队的提示。**不是**没提交的文字。
QUEUED_HINT = "Press up to edit queued messages"

# 停在交互选择框上的特征，抄 desk_push.sh 的 desk_busy()
CHOICE_MARKERS = r"Enter to select|↑/↓ to navigate|Esc to cancel"


def is_pane_id(pane: str) -> bool:
    """只认 pane-id（`%NN`）。

    不接受 `session:window` 名 —— 会话改名后按名字发会静默打空
    （2026-07-30 `journal` 被改成 `OS` 那次）。pane-id 改名、挪窗口、
    换 index 都不变。空字符串更要拒：`send-keys` 不带有效 `-t` 会打到
    「当前活动 pane」，2026-07-30 自测时就这么把命令打进了别人的 shell。
    """
    return bool(pane) and pane.startswith("%")


# ---------------------------------------------------------------- 纯解析
# 下面四个只吃字符串。它们是这个模块里唯一有判据的部分，也是唯一值得测的部分。

def tail(text: str, n: int) -> str:
    """取最后 n 行。

    为什么在 Python 这边切，不用 `capture-pane -S`：**`-S` 的行号是相对
    可见区顶部数的，不是相对底部**。一开始想用 `-S -{n} -E -` 精确取尾部，
    pane 矮的时候看着像只拿了尾部（凑巧），pane 高的时候（实测一个 46 行的）
    照样把整个可见区带回来。整块拿回来自己切，就不受 pane 高度影响。
    """
    return "\n".join(text.splitlines()[-n:])


def parse_pending_input(screen: str) -> str:
    """输入框里有没有已经打进去、但还没敲回车提交的文字。

    输入框是个方框，提示符 "❯ "；正常时提示符后面啥都没有。上边框是带标题的
    "──── 项目名 ──"，下边框是纯 "─" 一行。有文字排着没提交时，提示符和下
    边框之间会有内容 —— 扫到下边框为止，别只看提示符那一行，免得长文本换行漏看。

    `❯` 取**最后一次**出现，避免历史输出里凑巧带这个符号。
    """
    lines = screen.splitlines()
    prompt_i = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("❯"):
            prompt_i = i
    if prompt_i is None:
        return ""
    buf = []  # type: List[str]
    first = lines[prompt_i].strip()[1:].strip()
    if first and first != QUEUED_HINT:
        buf.append(first)
    for ln in lines[prompt_i + 1:]:
        s = ln.strip()
        if not s or s == QUEUED_HINT:
            continue
        if re.fullmatch(r"─+", s):        # 下边框：到此为止
            break
        buf.append(s)
    return " ".join(buf).strip()


def parse_awaiting_choice(tail_text: str) -> bool:
    """对面是不是正停在一个交互选择框上，不是在等文字输入。

    跟 `parse_pending_input` 防的不是一回事：那个防「输入框有字没提交」，
    这个防「根本不在输入框，正等你按键」。硬发文字进去会被当成对选择框的
    按键响应，可能替人确认了一件本该他自己点头的事（发消息确认、权限确认、
    危险命令确认）。这不是体验问题，是会踩红线的坑。

    **只吃尾部几行**，别喂整块可见区 —— 可见区里只要有人回显过
    "Enter to select"（比如正在 grep 它，或者这几个字本来就是任务原文的一部分
    被打印出来），就会被当成真的停在选择框上，误拒正常派活。
    """
    if not tail_text:
        return False
    return bool(re.search(CHOICE_MARKERS, tail_text))


def parse_ctx_usage(tail_text: str) -> Optional[str]:
    """从页脚解析上下文占用，形如 "529k (53%)"。

    页脚长这样：`[Opus 5 (1M context)] 项目名  ▓▓▓░░ 53% (529k)  ⚠⚠ /compact now`。

    解析不到返回 None —— **不猜**。页脚格式以后要是变了，这里应该老实说
    「不知道」，不该给一个可能是错的数字出去；拿这个字段判断该不该催压缩，
    字段错了调度就是错的。

    尾部里凑巧出现不止一个百分比时取**最后一个** —— 那个才离当前最近。
    """
    if not tail_text:
        return None
    matches = re.findall(r"(\d+)%\s*\((\d+k)\)", tail_text)
    if not matches:
        return None
    pct, kk = matches[-1]
    return "%s (%s%%)" % (kk, pct)


def send_delay(text: str) -> float:
    """文字和回车之间该停多久，按文本长度动态加。

    `-l` 把文字塞进输入框是异步的，紧跟着发 Enter 会跟文字挤在一起，任务
    停在输入框里没提交，文本越长越容易中。2026-08-01 实测：`capture-pane`
    层面文字几乎立刻就「看着」落定了，但应用内部真正进入「能接受 Enter＝
    提交」的状态明显更慢，固定 0.4s 对长文本不够。
    """
    return min(MAX_SEND_DELAY, BASE_SEND_DELAY + len(text) * PER_CHAR_DELAY)


# ---------------------------------------------------------------- IO 接缝

class ScreenSource(Protocol):
    """跟 tmux 之间的全部接触面。真实现调 tmux，假实现喂固定文本。

    注意这里**没有** `current_pane()`。见模块文档的约束 A。
    """

    def capture(self, pane: str, history: int = 0) -> Tuple[bool, str]: ...
    def send_literal(self, pane: str, text: str) -> bool: ...
    def send_enter(self, pane: str) -> bool: ...
    def alive(self, pane: str) -> bool: ...
    def live_panes(self) -> set: ...


def _sh(args: Sequence[str], timeout: float = 5) -> Tuple[int, str]:
    try:
        p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


class TmuxScreen(object):
    """真实现。"""

    def capture(self, pane, history=0):
        if not is_pane_id(pane):
            return False, ""
        args = ["tmux", "capture-pane", "-t", pane, "-p"]
        if history:
            args += ["-S", "-%d" % history]
        code, out = _sh(args)
        return (code == 0), (out if code == 0 else "")

    def send_literal(self, pane, text):
        if not is_pane_id(pane):
            return False
        code, _ = _sh(["tmux", "send-keys", "-t", pane, "-l", text])
        return code == 0

    def send_enter(self, pane):
        if not is_pane_id(pane):
            return False
        code, _ = _sh(["tmux", "send-keys", "-t", pane, "Enter"])
        return code == 0

    def alive(self, pane):
        if not pane:
            return False
        code, out = _sh(["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"])
        return code == 0 and out.startswith("%")

    def live_panes(self):
        code, out = _sh(["tmux", "list-panes", "-a", "-F", "#{pane_id}"])
        return set(out.split()) if code == 0 else set()


class FakeScreen(object):
    """假实现。**测试里唯一该用的东西**，不碰 tmux、不碰时钟。

    `frames` 按调用顺序返回，用完停在最后一帧 —— 这样能建模「旧帧」：
    给 `["旧内容", "新内容"]`，第一次抓到的就是过时的那一帧。

    `alive_panes` 为 None 表示「什么 pane 都活着」。给一个集合就能建模
    「pane 已经关了」。

    双后端要做全套：只给身份存储做了内存实现、别处还在读真文件，测试之间
    就会共享状态、互相污染 —— 那个教训是被一个「期望 1 条却拿到 3 条」的
    断言逼出来的，所以这里连时钟都不给真的。
    """

    def __init__(self, frames=None, alive_panes=None, send_ok=True, enter_ok=True):
        self.frames = list(frames or [""])
        self.alive_panes = alive_panes
        self.send_ok = send_ok
        self.enter_ok = enter_ok
        self.captures = 0
        self.sent = []          # [("literal", pane, text) | ("enter", pane, None)]

    def capture(self, pane, history=0):
        if not is_pane_id(pane):
            return False, ""
        i = min(self.captures, len(self.frames) - 1)
        self.captures += 1
        return True, self.frames[i]

    def send_literal(self, pane, text):
        if not is_pane_id(pane):
            return False
        self.sent.append(("literal", pane, text))
        return self.send_ok

    def send_enter(self, pane):
        if not is_pane_id(pane):
            return False
        self.sent.append(("enter", pane, None))
        return self.enter_ok

    def alive(self, pane):
        if not pane:
            return False
        if self.alive_panes is None:
            return is_pane_id(pane)
        return pane in self.alive_panes

    def live_panes(self):
        return set(self.alive_panes or [])


# ---------------------------------------------------------------- 探针

class PaneProbe(object):
    """一个 pane 的状态探针。

    **pane 身份是构造参数。** 没有「取当前 pane」这种方法 —— 见模块文档约束 A。
    `sleep` 也注进来，测试里传一个不睡的，就不用等真实秒数。
    """

    def __init__(self, pane, screen_source=None, sleep=None):
        self.pane = pane
        self.screen = screen_source if screen_source is not None else TmuxScreen()
        self.sleep = sleep if sleep is not None else time.sleep

    # --- 读 ---

    def pending_input(self):
        """**单次**抓屏的结果。会读到旧帧，别直接拿它做决定。"""
        ok, out = self.screen.capture(self.pane, history=20)
        return parse_pending_input(out) if ok else ""

    def pending_input_stable(self):
        """连抓两次、一致才采信；不一致返回 None，表示「读不准」。

        单次抓屏靠不住：`send-keys -l` 发完立刻抓，抓到的还是发之前那帧；
        清空之后抓到「已空」，下一次又变回清空前的旧帧。这条对发送后的回读
        护栏是致命的 —— 读到「旧帧还有字」会误判没提交、多补一次回车，可能
        把下一条消息错误提交；读到「旧帧是空的」会误判已提交，而且带着
        「已回读确认」的假象，比原来更难发现。

        不是万能药：两次都撞上同一帧旧值的概率不为零。**这是这个函数的
        可靠性上限，不能写成「已解决」。** 调用方必须自己决定读不准时怎么办，
        不能把 None 当成确定的空或确定的非空。
        """
        a = self.pending_input()
        self.sleep(STABLE_GAP)
        b = self.pending_input()
        return a if a == b else None

    def tail(self, n=6):
        """尾部 n 行。不管可见区多高、滚动到哪，永远是「此刻最新」这几行。"""
        ok, out = self.screen.capture(self.pane)
        return tail(out, n) if ok else ""

    def awaiting_choice(self):
        return parse_awaiting_choice(self.tail())

    def ctx_usage(self):
        return parse_ctx_usage(self.tail())

    def alive(self):
        return self.screen.alive(self.pane)

    # --- 写 ---

    def send(self, text):
        """发一段文字并确认真的提交了。**唯一允许发 send-keys 的公开入口。**

        为什么不直接暴露 `send_literal` + `send_enter`：因为
        `send-keys` 返回 0 只说明 tmux 把按键送进了 pane，既不保证对方收下了
        回车（实测两次卡了 54 和 79 分钟，文本进了输入框但没提交，是人工再
        敲一次回车才动），也不保证打对了 pane（见模块文档第 5 种）。
        「发出去了」不等于「提交了」。所以发送和回读必须是一个动作。

        回车重试是**兜底不是主力** —— 实测过单靠重试救不回来（应用没就绪时
        再敲几次 Enter 也没用），真正起作用的是 `send_delay` 那个动态延时。
        重试留着是为了「提交没提交」这件事不再靠猜、靠沉默的返回值。
        """
        if not is_pane_id(self.pane):
            return False
        if not self.alive():
            return False
        if not self.screen.send_literal(self.pane, text):
            return False
        self.sleep(send_delay(text))
        for _ in range(MAX_ENTER_RETRIES):
            if not self.screen.send_enter(self.pane):
                return False
            self.sleep(ENTER_RETRY_WAIT)
            if self.pending_input_stable() == "":
                return True
            # None（读不准）和非空（确实没提交）都继续重试
        return self.pending_input_stable() == ""
