# -*- coding: utf-8 -*-
"""终端仪表盘的组版判据。

用户 2026-08-28 原话：「是不是可以重新启动了呢？有个酷炫的动画吗？在终端里面」。
他挑的是「启动序列 + 常驻面板」那一版。

这批用例守的是三条**已经栽过**的形状，不是覆盖率：

1. **读不出 ≠ 0**：`ctx_usage` 解析不到时 `bar` 必须画成跟 0% 不一样的东西，
   采集函数抛异常时那一段必须显示「读不出」。屏幕上一个 0 会让人以为压过了。
2. **同一个 pane 只算一次**：autocompact 上就栽在这——多条记录指同一个窗口，
   结果同一个 pane 被选中三次。
3. **截断了必须说**：只显示前 N 个而不报被省掉多少，等于谎报「就这些」。

外加一条**对账**：`parse_ctx` 的 kb 必须跟 `fleet._ctx_kb` 一致。两个函数解析
同一个字串，不钉住就会各自漂。

全部用造的数据，不碰 data/、不抓屏、不进 tmux。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dash                                                      # noqa: E402
import fleet                                                     # noqa: E402


def pane(coord="OS:workOS.1", pid="%1", ctx="100k (10%)"):
    return {"coord": coord, "pane": pid, "ctx": ctx}


def snap(**over):
    base = {"at": "16:00:00", "checks": [{"ok": True, "label": "依赖 tmux", "note": ""}],
            "services": [{"name": "console", "state": "OK", "when": "常驻", "note": ""}],
            "collector": [{"k": "上次采集", "v": "1 分前", "warn": False}],
            "stale": 0, "panes": [pane()]}
    base.update(over)
    return base


def text_of(lines):
    return "\n".join(t for t, _ in lines)


class ParseCtx(unittest.TestCase):
    def test_两半都解析(self):
        self.assertEqual(dash.parse_ctx("529k (53%)"), (529, 53))

    def test_解析不到给None不给0(self):
        # 这是整个文件最重要的一条：0 是个合法的占用值，None 是「不知道」。
        for bad in (None, "", "—", "ctx unknown", "?"):
            self.assertEqual(dash.parse_ctx(bad), (None, None), bad)

    def test_只有一半时另一半是None(self):
        self.assertEqual(dash.parse_ctx("300k"), (300, None))
        self.assertEqual(dash.parse_ctx("(42%)"), (None, 42))

    def test_kb跟fleet那份对账(self):
        """同一批输入，两个解析器必须给同一个 kb。"""
        # 后三个是**能让两者分歧的**输入：fleet 用 re.match，前面有别的字就该
        # 解析不到。表里只放 "529k (53%)" 这种规整字串，match/search 换着用都绿，
        # 这条对账就成了摆设（变异测试里 search 版活下来过一次）。
        for s in ("529k (53%)", "0k (0%)", "300k", None, "", "垃圾", "1234k (99%)",
                  "ctx 300k (30%)", " 12k", "x529k"):
            self.assertEqual(dash.parse_ctx(s)[0], fleet._ctx_kb(s), repr(s))


class Bar(unittest.TestCase):
    def test_读不出跟0画得不一样(self):
        self.assertNotEqual(dash.bar(None), dash.bar(0))

    def test_宽度恒定(self):
        for pct in (None, 0, 1, 50, 99, 100):
            self.assertEqual(dash.display_width(dash.bar(pct, 12)), 12, pct)

    def test_越界不崩也不超宽(self):
        self.assertEqual(dash.display_width(dash.bar(150, 12)), 12)
        self.assertEqual(dash.display_width(dash.bar(-10, 12)), 12)

    def test_满和空是两端(self):
        self.assertNotIn("▁", dash.bar(100, 6))
        self.assertNotIn("█", dash.bar(0, 6))


class CtxRows(unittest.TestCase):
    def test_同一个pane只出一行(self):
        rows, hidden = dash.ctx_rows(
            [pane("OS:a.1", "%7"), pane("OS:b.1", "%7"), pane("OS:c.1", "%8")], top=6)
        self.assertEqual([r["pane"] for r in rows], ["%7", "%8"])
        self.assertEqual(hidden, 0)

    def test_按占用降序(self):
        rows, _ = dash.ctx_rows([pane("a", "%1", "10k (1%)"),
                                 pane("b", "%2", "400k (40%)"),
                                 pane("c", "%3", "200k (20%)")], top=6)
        self.assertEqual([r["coord"] for r in rows], ["b", "c", "a"])

    def test_读不出的排最后不排最前(self):
        """None 既不能当 0（排最后没问题）也不能当无穷大（占掉榜首）。
        它得排在所有读得出的后面，因为屏幕位置有限，先给能看的。"""
        rows, _ = dash.ctx_rows([pane("bad", "%1", None),
                                 pane("small", "%2", "1k (0%)")], top=6)
        self.assertEqual([r["coord"] for r in rows], ["small", "bad"])

    def test_截断了要报被省掉多少(self):
        rows, hidden = dash.ctx_rows([pane(f"p{i}", f"%{i}") for i in range(10)], top=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(hidden, 7)

    def test_去重之后才算被省掉几个(self):
        """三条记录两个 pane、上限 1 → 省掉的是 1 个 pane，不是 2 条记录。"""
        _, hidden = dash.ctx_rows(
            [pane("a", "%1"), pane("b", "%1"), pane("c", "%2")], top=1)
        self.assertEqual(hidden, 1)


class Width(unittest.TestCase):
    def test_中文算两格(self):
        self.assertEqual(dash.display_width("压力测试"), 8)
        self.assertEqual(dash.display_width("abc"), 3)

    def test_pad补到显示宽度(self):
        self.assertEqual(dash.display_width(dash.pad("压测", 10)), 10)

    def test_超长坐标不把后面的列推歪(self):
        """写这条的原因：第一版只 pad 不 clip，一个中文窗口名的坐标比列宽长，
        进度条整列被推出去了。"""
        long = snap(panes=[pane("proj-a:安卓模拟器压力测试.1", "%1", "400k (40%)")])
        short = snap(panes=[pane("a.1", "%1", "400k (40%)")])
        def bar_col(sn):
            row = [t for t in text_of(dash.compose(sn, 100)).splitlines() if "█" in t][0]
            # 量**显示宽度**，不是 str.index —— 中文一个字符占两格，
            # 对齐了字符下标照样不等。第一版这条断言就栽在这儿。
            return dash.display_width(row[:row.index("█")])
        self.assertEqual(bar_col(long), bar_col(short))


class Compose(unittest.TestCase):
    def test_采集失败显示读不出而不是0(self):
        out = text_of(dash.compose(snap(collector=None, stale=None, panes=None), 100))
        self.assertEqual(out.count("读不出"), 3)
        self.assertNotIn("0 条", out)

    def test_哨兵为0说的是0条不是读不出(self):
        """`None` 和 `0` 走的是两条完全不同的话。"""
        out = text_of(dash.compose(snap(stale=0), 100))
        self.assertIn("0 条", out)
        self.assertNotIn("读不出", out)

    def test_自检失败项逐条展开(self):
        out = text_of(dash.compose(snap(checks=[
            {"ok": True, "label": "依赖 tmux", "note": ""},
            {"ok": False, "label": "config.json", "note": "缺"}]), 100))
        self.assertIn("1/2 通过", out)
        self.assertIn("✗ config.json", out)

    def test_超过压缩阈值才标该压了(self):
        kb = fleet.AUTOCOMPACT_THRESHOLD_KB
        hot = snap(panes=[pane("a", "%1", f"{kb}k (50%)")])
        cold = snap(panes=[pane("a", "%1", f"{kb - 1}k (49%)")])
        self.assertIn("该压了", text_of(dash.compose(hot, 100)))
        self.assertNotIn("该压了", text_of(dash.compose(cold, 100)))

    def test_阈值取自fleet不是写死的字面量(self):
        self.assertEqual(dash.COMPACT_KB, fleet.AUTOCOMPACT_THRESHOLD_KB)

    def test_窄终端不崩(self):
        for w in (1, 10, 40, 200):
            lines = dash.compose(snap(), w)
            self.assertTrue(lines)

    def test_不上色时不带转义序列(self):
        """--plain 和管道出去必须是干净文本。"""
        for text, tone in dash.compose(snap(), 100):
            self.assertNotIn("\x1b", dash.paint(text, tone, color=False))

    def test_没有Claude会话时明说(self):
        self.assertIn("没有 Claude pane", text_of(dash.compose(snap(panes=[]), 100)))


def boom():
    raise RuntimeError("boom")


class Snapshot(unittest.TestCase):
    """`snapshot` 是唯一做 IO 的函数，所以这里把它的**每一个**采集面都换成假的。

    ⚠️ 一个都不能漏：漏一个就会去读真的 `data/`（第一版漏了四个，
    `tests/_audit_run.py` 的计数从 0 跳到 5 才发现）。生产台账是线上状态，
    测试连读都不该读——读了就等于用例结果依赖当时线上有几条消息。
    """

    def setUp(self):
        self.saved = {n: getattr(dash.console, n) for n in
                      ("collect_checks", "collect_services",
                       "collect_collector", "collect_panes")}
        self.saved_stale = dash.dtwatch
        for n in self.saved:
            setattr(dash.console, n, lambda *a, **k: [])
        dash.dtwatch = None                      # 走「没有 dtwatch」那条，不碰 inbox

    def tearDown(self):
        for n, fn in self.saved.items():
            setattr(dash.console, n, fn)
        dash.dtwatch = self.saved_stale

    def test_一项抛异常留None不留空(self):
        """失败必须跟「合法的空」分得开：抛异常 → None → 屏幕上「读不出」。
        如果这里兜成 `[]`，屏幕会理直气壮地说「0 条」。"""
        dash.console.collect_services = boom
        s = dash.snapshot(with_ctx=False)
        self.assertIsNone(s["services"])
        self.assertIn("读不出", text_of(dash.compose(s, 100)))

    def test_一项炸了不影响别的项(self):
        dash.console.collect_services = boom
        s = dash.snapshot(with_ctx=False)
        self.assertIsNone(s["services"])
        self.assertEqual(s["checks"], [])         # 空列表≠None：它真的返回了空
        self.assertEqual(s["collector"], [])

    def test_全炸了也能画出一屏(self):
        for n in ("collect_checks", "collect_services",
                  "collect_collector", "collect_panes"):
            setattr(dash.console, n, boom)
        out = text_of(dash.compose(dash.snapshot(with_ctx=False), 100))
        self.assertEqual(out.count("读不出"), 5)   # 五段全部如实说读不出


class Eased(unittest.TestCase):
    def test_向目标靠近(self):
        p = [pane("a", "%1", "500k (50%)")]
        cur = dash.step_eased({"%1": 0.0}, p)
        self.assertGreater(cur["%1"], 0)
        self.assertLess(cur["%1"], 50)

    def test_够近就吸附(self):
        p = [pane("a", "%1", "500k (50%)")]
        self.assertEqual(dash.step_eased({"%1": 49.9}, p)["%1"], 50.0)

    def test_多帧后收敛(self):
        p = [pane("a", "%1", "500k (50%)")]
        cur = {}
        for _ in range(60):
            cur = dash.step_eased(cur, p)
        self.assertEqual(cur["%1"], 50.0)

    def test_读不出的pane不参与动画(self):
        """留在字典里就会被当成 0% 画出来——那正是「读不出显示成 0」。"""
        cur = dash.step_eased({"%1": 30.0}, [pane("a", "%1", None)])
        self.assertNotIn("%1", cur)

    def test_空输入不崩(self):
        self.assertEqual(dash.step_eased({}, None), {})


if __name__ == "__main__":
    unittest.main()
