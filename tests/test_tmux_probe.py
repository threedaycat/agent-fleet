"""tmux_probe 的测试。

每个用例钉住一个**已经付过学费**的 bug。commit 号是那次的出处 ——
以后再发现一种新的说谎方式，是在这里加一个用例，不是在业务代码里补一个 if。

跑：python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tmux_probe as tp


def screen(*lines):
    return "\n".join(lines)


BOX_TOP = "──── zymix ──────────────────────────"
BOX_BOT = "─────────────────────────────────────"
FOOTER = "[Opus 5 (1M context)] zymix  ▓▓▓░░ 53% (529k)  ⚠⚠ /compact now"


class PendingInput(unittest.TestCase):
    def test_空框返回空(self):
        s = screen("上一轮的输出", BOX_TOP, "❯ ", BOX_BOT)
        self.assertEqual(tp.parse_pending_input(s), "")

    def test_有未提交文字(self):
        s = screen(BOX_TOP, "❯ 帮我看一下这个", BOX_BOT)
        self.assertEqual(tp.parse_pending_input(s), "帮我看一下这个")

    def test_长文本换行要扫到下边框(self):
        """别只看提示符那一行，免得长文本换行漏看。"""
        s = screen(BOX_TOP, "❯ 第一行", "第二行", "第三行", BOX_BOT, FOOTER)
        self.assertEqual(tp.parse_pending_input(s), "第一行 第二行 第三行")

    def test_排队提示不算未提交(self):
        """5380 实测的假阴性：发给正忙的会话，消息其实排队成功了，
        但这行提示被当成「框里还有字」，导致 wake 报了假阴性。"""
        s = screen(BOX_TOP, "❯ " + tp.QUEUED_HINT, BOX_BOT)
        self.assertEqual(tp.parse_pending_input(s), "")

    def test_排队提示混在多行里也要跳过(self):
        s = screen(BOX_TOP, "❯ 真的没提交", tp.QUEUED_HINT, BOX_BOT)
        self.assertEqual(tp.parse_pending_input(s), "真的没提交")

    def test_取最后一个提示符(self):
        """历史输出里凑巧带 ❯ 不能算。"""
        s = screen("日志里有个 ❯ 符号", "更多输出", BOX_TOP, "❯ ", BOX_BOT)
        self.assertEqual(tp.parse_pending_input(s), "")

    def test_没有提示符返回空(self):
        self.assertEqual(tp.parse_pending_input(screen("啥也没有", "就是输出")), "")


class AwaitingChoice(unittest.TestCase):
    def test_停在选择框上(self):
        self.assertTrue(tp.parse_awaiting_choice(screen("要发这条吗？", "Enter to select")))

    def test_三种特征都认(self):
        for marker in ("Enter to select", "↑/↓ to navigate", "Esc to cancel"):
            self.assertTrue(tp.parse_awaiting_choice(marker), marker)

    def test_回显不算_c080b6e(self):
        """可见区里有人回显过 "Enter to select"（grep 它、或者它本来就是任务原文
        的一部分被打印出来），不能当成真的停在选择框上 —— 会误拒正常派活。
        防法是只喂尾部：这里模拟「关键词在很上面，尾部已经翻过去了」。"""
        full = screen("我在 grep Enter to select 这几个字", *["输出"] * 20, BOX_TOP, "❯ ", BOX_BOT)
        self.assertFalse(tp.parse_awaiting_choice(tp.tail(full, 6)))

    def test_空文本不算(self):
        self.assertFalse(tp.parse_awaiting_choice(""))


class CtxUsage(unittest.TestCase):
    def test_正常页脚(self):
        self.assertEqual(tp.parse_ctx_usage(FOOTER), "529k (53%)")

    def test_滚动区旧页脚取最后一个_b466a64(self):
        """原来带 -S -15 往上翻 scrollback，抓到过旧页脚：报 89k 实际 336k、
        报 658k 实际 126k。尾部里出现不止一个百分比时，最后一个才离当前最近。"""
        two = screen("[Opus 5] zymix ▓░ 9% (89k)", "中间的输出",
                     "[Opus 5] zymix ▓▓▓ 34% (336k)")
        self.assertEqual(tp.parse_ctx_usage(two), "336k (34%)")

    def test_解析不到就说不知道(self):
        """页脚格式变了应该老实返回 None，不该给一个可能是错的数字 ——
        拿这个字段判断该不该催压缩，字段错了调度就是错的。"""
        self.assertIsNone(tp.parse_ctx_usage(screen("完全不是页脚", "❯ ")))
        self.assertIsNone(tp.parse_ctx_usage(""))

    def test_百分比但没有k不算(self):
        self.assertIsNone(tp.parse_ctx_usage("进度 53% 完成"))


class Tail(unittest.TestCase):
    def test_不受可见区高度影响(self):
        """-S 的行号是相对可见区顶部数的，不是相对底部；pane 高的时候
        （实测一个 46 行的）会把整块带回来。所以在 Python 这边切。"""
        tall = screen(*["第%d行" % i for i in range(46)])
        self.assertEqual(tp.tail(tall, 3), "第43行\n第44行\n第45行")

    def test_行数不足就全给(self):
        self.assertEqual(tp.tail("只有一行", 6), "只有一行")


class StaleFrame(unittest.TestCase):
    """95225e2：capture-pane 会返回旧帧。"""

    def test_两帧不一致返回None(self):
        p = tp.PaneProbe("%1", screen_source=tp.FakeScreen(frames=[
            screen(BOX_TOP, "❯ 旧帧还有字", BOX_BOT),
            screen(BOX_TOP, "❯ ", BOX_BOT),
        ]), sleep=lambda _s: None)
        self.assertIsNone(p.pending_input_stable())

    def test_两帧一致才采信(self):
        p = tp.PaneProbe("%1", screen_source=tp.FakeScreen(frames=[
            screen(BOX_TOP, "❯ 还没提交", BOX_BOT),
        ]), sleep=lambda _s: None)
        self.assertEqual(p.pending_input_stable(), "还没提交")


class SendGuards(unittest.TestCase):
    """5f05ec1（回车竞态）+ 2026-07-30（打到别人 shell）+ 第 5 种（返回码骗你）。"""

    def _probe(self, pane="%1", **kw):
        fake = tp.FakeScreen(**kw)
        return tp.PaneProbe(pane, screen_source=fake, sleep=lambda _s: None), fake

    def test_pane为空一律拒发(self):
        """send-keys 不带有效 -t 会打到「当前活动 pane」。宁可不发不能乱发。"""
        p, fake = self._probe(pane="")
        self.assertFalse(p.send("任务"))
        self.assertEqual(fake.sent, [])

    def test_只接受pane_id(self):
        """会话改名后按名字发会静默打空。"""
        p, fake = self._probe(pane="OS:5.1")
        self.assertFalse(p.send("任务"))
        self.assertEqual(fake.sent, [])

    def test_pane不在了拒发(self):
        p, fake = self._probe(pane="%99", alive_panes=set())
        self.assertFalse(p.send("任务"))
        self.assertEqual(fake.sent, [])

    def test_回读为空才算成功(self):
        p, fake = self._probe(frames=[screen(BOX_TOP, "❯ ", BOX_BOT)])
        self.assertTrue(p.send("任务"))
        self.assertEqual([k for k, _, _ in fake.sent], ["literal", "enter"])

    def test_一直没提交就重试到上限然后失败(self):
        """回车重试是兜底不是主力——应用没就绪时再敲几次也没用，
        所以必须有上限，别死循环往人家输入框里塞字。"""
        p, fake = self._probe(frames=[screen(BOX_TOP, "❯ 卡住了", BOX_BOT)])
        self.assertFalse(p.send("任务"))
        enters = [k for k, _, _ in fake.sent if k == "enter"]
        self.assertEqual(len(enters), tp.MAX_ENTER_RETRIES)

    def test_文本越长延时越长(self):
        short, long_ = tp.send_delay("嗯"), tp.send_delay("字" * 2000)
        self.assertLess(short, long_)
        self.assertEqual(short, tp.BASE_SEND_DELAY + 1 * tp.PER_CHAR_DELAY)
        self.assertLessEqual(long_, tp.MAX_SEND_DELAY)

    def test_延时有上限(self):
        self.assertEqual(tp.send_delay("字" * 10 ** 6), tp.MAX_SEND_DELAY)


class NoCurrentPaneMethod(unittest.TestCase):
    """第 5 种失败模式：打到了别的 pane，而 send-keys **也返回 0**。

    根因是用 `tmux display-message -p '#{pane_id}'` 取「自己」的 pane ——
    那个命令返回的是当前**聚焦的** pane。2026-08-01 凌晨四个会话同时踩：
    全都报「已送达」，四个页脚一个没降，各自空转。

    修法不是重试或等待，是**别在运行时解析自己的身份**。所以这个模块
    故意不提供任何「取当前 pane」的入口 —— 让这个 bug 在类型上写不出来。
    这条用例就是那个约束的守卫。
    """

    FORBIDDEN = ("current_pane", "get_pane", "my_pane", "self_pane",
                 "whoami", "tmux_target")

    def test_模块不暴露取当前pane的入口(self):
        for name in self.FORBIDDEN:
            self.assertFalse(hasattr(tp, name),
                             "tmux_probe 不该有 %s ——「自己是谁」只能由调用方"
                             "从 $TMUX_PANE 读，不能在这里查" % name)

    def test_探针也不暴露(self):
        for name in self.FORBIDDEN:
            self.assertFalse(hasattr(tp.PaneProbe, name), name)

    def test_pane身份是构造参数(self):
        import inspect
        params = list(inspect.signature(tp.PaneProbe.__init__).parameters)
        self.assertEqual(params[1], "pane",
                         "pane 必须是第一个构造参数，不能是可选的、也不能延后解析")

    def test_每条tmux命令都显式带_t(self):
        """第 5 种的机制是「没指定目标就打到当前活动 pane，而且返回 0」。
        所以真实现里每一条对某个 pane 的 tmux 命令都必须显式带 -t，
        并且 -t 后面紧跟的就是传进来的 pane 参数——不是任何查出来的东西。"""
        import inspect
        for meth in (tp.TmuxScreen.capture, tp.TmuxScreen.send_literal,
                     tp.TmuxScreen.send_enter, tp.TmuxScreen.alive):
            src = inspect.getsource(meth)
            self.assertIn('"-t", pane', src,
                          "%s 必须显式 -t 到传进来的 pane" % meth.__name__)

    def test_真实现不从环境或查询里取自身身份(self):
        """别在运行时解析「我是谁」。$TMUX_PANE 也不该在这一层读——
        那是调用方的事，这里只接受被告知的 pane。"""
        import inspect
        src = inspect.getsource(tp.TmuxScreen) + inspect.getsource(tp.PaneProbe)
        for bad in ("TMUX_PANE", "os.environ", "session_name"):
            self.assertNotIn(bad, src, "tmux_probe 不该出现 %s" % bad)


if __name__ == "__main__":
    unittest.main()
