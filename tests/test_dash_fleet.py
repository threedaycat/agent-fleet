"""dash 的舰队编组：一个 tmux session 一个框，框里是成员和状态。

盯三件事：
  1. 省略必须报数 —— 画 5 行说成全部，跟把投不出去的消息说成 0 条是一个错；
  2. 统计按**全组**算，不按画出来的几行算；
  3. `unknown`（上下文读不出）和 `never`（0k）必须分得开。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import dash                                             # noqa: E402


def P(coord, ctx="10k (1%)", role=None, status="input"):
    sess = coord.split(":", 1)[0]
    return {"coord": coord, "session": sess, "pane": "%" + coord,
            "ctx": ctx, "role": role, "status": status}


class TestMemberMark(unittest.TestCase):
    def test_跑着的优先显示在跑(self):
        self.assertEqual(dash.member_mark(P("a:w.1", status="running")), "▶")

    def test_零k是从没干过活(self):
        self.assertEqual(dash.member_mark(P("a:w.1", ctx="0k (0%)")), "○")

    def test_读不出不等于零k(self):
        """这条是整个文件的理由。读不出是「没有结论」，0k 是「结论是没干过活」。"""
        self.assertNotEqual(dash.member_mark(P("a:w.1", ctx=None)),
                            dash.member_mark(P("a:w.1", ctx="0k (0%)")))
        self.assertEqual(dash.member_mark(P("a:w.1", ctx=None)), "?")

    def test_解析不出来的字符串也算读不出(self):
        self.assertEqual(dash.member_mark(P("a:w.1", ctx="???")), "?")


class TestGroupPanes(unittest.TestCase):
    def test_按session分组(self):
        g, _, _ = dash.group_panes([P("a:w.1"), P("b:w.1"), P("a:w.2")])
        self.assertEqual({n for n, _, _ in g}, {"a", "b"})
        self.assertEqual(dict((n, s["total"]) for n, _, s in g), {"a": 2, "b": 1})

    def test_有声明角色的组排最前(self):
        """那是舰队正式编制，不是随手开的窗口。"""
        g, _, _ = dash.group_panes([P("big:w.1", ctx="900k (90%)"),
                                    P("big:w.2", ctx="900k (90%)"),
                                    P("os:w.1", ctx="1k (0%)", role="main")])
        self.assertEqual(g[0][0], "os")

    def test_组内角色排最前(self):
        g, _, _ = dash.group_panes([P("a:w.1", ctx="900k (90%)"),
                                    P("a:w.2", ctx="1k (0%)", role="desk")])
        self.assertEqual(g[0][1][0]["role"], "desk")

    def test_统计按全组算不按画出来的几行算(self):
        """**第一版就错在这。** 10 个 pane 只画 5 行，报「5 干过活」是编造。"""
        ps = [P(f"a:w.{i}", ctx="7k (1%)") for i in range(1, 11)]
        g, _, _ = dash.group_panes(ps, rows=5)
        name, shown, stat = g[0]
        self.assertEqual(len(shown), 5)
        self.assertEqual(stat["total"], 10)
        self.assertEqual(stat["live"], 10)

    def test_零k的不算干过活(self):
        g, _, _ = dash.group_panes([P("a:w.1", ctx="0k (0%)"), P("a:w.2", ctx="5k (1%)")])
        self.assertEqual(g[0][2]["live"], 1)

    def test_读不出的也不算干过活_但也不算零(self):
        g, _, _ = dash.group_panes([P("a:w.1", ctx=None)])
        self.assertEqual(g[0][2]["live"], 0)
        self.assertEqual(dash.member_mark(g[0][1][0]), "?")

    def test_超出的组和成员都要报数(self):
        ps = ([P(f"s{i}:w.1") for i in range(6)]
              + [P("s0:w.%d" % k) for k in range(2, 9)])
        g, lost_g, lost_m = dash.group_panes(ps, cap=2, rows=3)
        self.assertEqual(len(g), 2)
        self.assertEqual(lost_g, 4)
        # s0 有 8 个只画 3 个 → 少 5；另外 4 个组各 1 个 → 少 4
        self.assertEqual(lost_m, 9)

    def test_没超出时不报假的省略(self):
        g, lost_g, lost_m = dash.group_panes([P("a:w.1")], cap=4, rows=5)
        self.assertEqual((lost_g, lost_m), (0, 0))

    def test_空输入不炸(self):
        self.assertEqual(dash.group_panes([]), ([], 0, 0))
        self.assertEqual(dash.group_panes(None), ([], 0, 0))

    def test_没有session字段的归到问号组(self):
        g, _, _ = dash.group_panes([{"coord": "x", "ctx": "1k (1%)"}])
        self.assertEqual(g[0][0], "?")


class TestBoxLines(unittest.TestCase):
    def test_每行都是同一个显示宽度(self):
        """中文一个字占两列。按字符数补齐的框在中文行上会豁口。"""
        lines = dash.box_lines("组", "3 pane", ["中文成员名 abc", "x"], 40)
        self.assertEqual({dash.display_width(x) for x in lines}, {40})

    def test_标题太长时砍掉右上角统计而不是砍标题(self):
        lines = dash.box_lines("一个非常非常非常长的小组名字", "9 pane · 9 干过活", [], 24)
        self.assertNotIn("干过活", lines[0])
        self.assertEqual(dash.display_width(lines[0]), 24)

    def test_标题本身超宽就截标题(self):
        lines = dash.box_lines("超级长的名字" * 6, "", [], 20)
        self.assertEqual(dash.display_width(lines[0]), 20)

    def test_没有成员也画得出上下两条边(self):
        self.assertEqual(len(dash.box_lines("a", "", [], 20)), 2)

    def test_成员行超宽被截断而不是撑破框(self):
        lines = dash.box_lines("a", "", ["x" * 200], 30)
        self.assertEqual(dash.display_width(lines[1]), 30)


class TestMemberLine(unittest.TestCase):
    def test_坐标去掉session前缀(self):
        """session 名已经写在框的标题上了，成员行再重复一遍是浪费列宽。"""
        line = dash.member_line(P("proj-a:推荐.1"), 46)
        self.assertIn("推荐.1", line)
        self.assertNotIn("proj-a", line)

    def test_读不出的上下文不显示成0k(self):
        line = dash.member_line(P("a:w.1", ctx=None), 46)
        self.assertIn("—", line)
        self.assertNotIn("0k", line)

    def test_窄宽度下也不炸(self):
        for w in (10, 16, 24, 46, 80):
            dash.member_line(P("a:很长的窗口名字啊.1"), w)


class TestComposeStillRenders(unittest.TestCase):
    def test_panes读不出时整节报读不出而不是画空框(self):
        snap = {"at": "", "checks": None, "services": None, "collector": None,
                "stale": None, "roles": None, "panes": None}
        text = "\n".join(t for t, _ in dash.compose(snap, 58))
        self.assertIn("舰队", text)
        self.assertIn("读不出", text)

    def test_有pane时画出框(self):
        snap = {"at": "", "checks": None, "services": None, "collector": None,
                "stale": None, "roles": None,
                "panes": [P("OS:workOS.1", ctx="120k (12%)", role="main")]}
        text = "\n".join(t for t, _ in dash.compose(snap, 58))
        self.assertIn("┌", text)
        self.assertIn("main", text)


if __name__ == "__main__":
    unittest.main()


class TestCompactHint(unittest.TestCase):
    """「该压了」是这个仓库唯一会催他动手的提示，换视图时最容易弄丢。
    2026-09-03 改成框的时候就丢过一次，是旧用例逮到的。"""

    def test_超线的行带该压了(self):
        line = dash.member_line(P("a:w.1", ctx=f"{dash.COMPACT_KB + 1}k (50%)"),
                                46, tail_w=8)
        self.assertIn("该压了", line)

    def test_没超线的不带(self):
        line = dash.member_line(P("a:w.1", ctx="10k (1%)"), 46, tail_w=8)
        self.assertNotIn("该压了", line)

    def test_读不出的不催(self):
        """没有结论就不该催人干活。"""
        self.assertFalse(dash.needs_compact(P("a:w.1", ctx=None)))

    def test_同一框里每行显示宽度一致_哪怕只有一个超线(self):
        ms = [P("a:w.1", ctx=f"{dash.COMPACT_KB + 5}k (50%)"), P("a:w.2", ctx="9k (1%)")]
        ws = {dash.display_width(dash.member_line(m, 46, tail_w=8)) for m in ms}
        self.assertEqual(len(ws), 1)

    def test_行宽恒等于给定宽度_预留与否都一样(self):
        """预留的列是从坐标列里让出去的，不是把行撑长 —— 撑长会顶破框。"""
        for tw in (0, 8):
            self.assertEqual(
                dash.display_width(dash.member_line(P("a:w.1", ctx="9k (1%)"), 46, tw)),
                46)

    def test_没人超线时坐标列更宽(self):
        long = P("a:一个相当长的窗口名字.1", ctx="9k (1%)")
        self.assertLess(dash.member_line(long, 46, tail_w=8).count("…"),
                        2)                              # 只是别炸
        a = dash.member_line(long, 46, tail_w=0)
        b = dash.member_line(long, 46, tail_w=8)
        self.assertNotEqual(a, b)                       # 预留确实改变了排版

    def test_compose_单列宽框给出文字提示(self):
        """宽度决定形状：单列时框有 52 宽，装得下「该压了」。
        94 宽会排成两列、每框 43 宽 —— 那时退到 `!`，见 TestHintNeverDisappears。"""
        snap = {"at": "", "checks": None, "services": None, "collector": None,
                "stale": None, "roles": None,
                "panes": [P("OS:w.1", ctx=f"{dash.COMPACT_KB + 20}k (60%)")]}
        text = "\n".join(t for t, _ in dash.compose(snap, 58))
        self.assertIn("该压了", text)


class TestFitColumns(unittest.TestCase):
    def test_宽度够就多放几个(self):
        self.assertEqual(dash.fit_columns(200, box_min=34, gap=2), 5)

    def test_刚好一个的宽度只放一个(self):
        self.assertEqual(dash.fit_columns(34, box_min=34, gap=2), 1)

    def test_窄到放不下一个也得放一个(self):
        """挤一点好过整节消失。"""
        self.assertEqual(dash.fit_columns(10, box_min=34, gap=2), 1)

    def test_两个框加空隙刚好(self):
        self.assertEqual(dash.fit_columns(34 * 2 + 2, box_min=34, gap=2), 2)

    def test_差一列就放不下第二个(self):
        self.assertEqual(dash.fit_columns(34 * 2 + 1, box_min=34, gap=2), 1)


class TestColWidth(unittest.TestCase):
    def test_并排的框宽度相等且不超总宽(self):
        for total in (52, 90, 100, 196, 240):
            k = dash.fit_columns(total)
            w = dash.col_width(total, k)
            self.assertLessEqual(w * k + dash.BOX_GAP * (k - 1), total)

    def test_余数留右边不摊给各框(self):
        """摊了各框宽度就不齐，成员行的列会对不上。"""
        self.assertEqual(dash.col_width(101, 2, gap=1), 50)

    def test_不会低于最小框宽(self):
        self.assertGreaterEqual(dash.col_width(10, 5), dash.BOX_MIN)


class TestPack(unittest.TestCase):
    def test_按每行个数切开(self):
        self.assertEqual(dash.pack([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_每行至少一个(self):
        self.assertEqual(dash.pack([1, 2], 0), [[1], [2]])

    def test_空的给空(self):
        self.assertEqual(dash.pack([], 3), [])


class TestRightFit(unittest.TestCase):
    def test_够宽就给全称(self):
        s = dash.right_fit({"total": 9, "live": 9, "run": 2, "hot": 0}, 40)
        self.assertIn("干过活", s)

    def test_窄了降级但总数不丢(self):
        s = dash.right_fit({"total": 9, "live": 9, "run": 2, "hot": 0}, 12)
        self.assertIn("9", s)
        self.assertLessEqual(dash.display_width(s) + 2, 12)

    def test_该压的数量参与降级不被优先砍掉(self):
        """框窄到放不下逐行「该压了」时，这个数字是唯一还看得见的信号。"""
        s = dash.right_fit({"total": 9, "live": 9, "run": 0, "hot": 4}, 20)
        self.assertIn("4", s)
        self.assertIn("压", s)

    def test_窄到极限给空串而不是半句话(self):
        s = dash.right_fit({"total": 100, "live": 100, "run": 9, "hot": 9}, 3)
        self.assertEqual(s, "")

    def test_没有该压的就不提(self):
        s = dash.right_fit({"total": 3, "live": 1, "run": 0, "hot": 0}, 40)
        self.assertNotIn("压", s)


class TestTailFor(unittest.TestCase):
    def test_没人超线不留(self):
        self.assertEqual(dash.tail_for(80, False), 0)

    def test_够宽给全称(self):
        self.assertEqual(dash.tail_for(80, True), 2 + dash.display_width("该压了"))

    def test_不够宽就退到零_让记号列接手(self):
        """留 8 列会把坐标列挤到认不出是哪个窗口，那时宁可砍提示。"""
        self.assertEqual(dash.tail_for(33, True), 0)

    def test_临界宽度_坐标列至少留得住(self):
        w = dash.MEMBER_FIXED + dash.MIN_COORD + 2 + dash.display_width("该压了")
        self.assertGreater(dash.tail_for(w, True), 0)
        self.assertEqual(dash.tail_for(w - 1, True), 0)


class TestHintNeverDisappears(unittest.TestCase):
    """**三种宽度下都要看得见「有人该压了」，只是形状不同。**
    2026-09-03 栽过两次：第一次整个丢了，第二次在窄框里被截成「该…」。"""

    HOT = None

    def setUp(self):
        self.HOT = P("a:w.1", ctx=f"{dash.COMPACT_KB + 100}k (54%)")

    def test_宽框给文字(self):
        self.assertIn("该压了", dash.member_line(self.HOT, 44, dash.tail_for(44, True)))

    def test_窄框给记号(self):
        line = dash.member_line(self.HOT, 33, dash.tail_for(33, True))
        self.assertTrue(line.startswith(dash.COMPACT_MARK))

    def test_正在跑的保留在跑记号_不被感叹号顶掉(self):
        """正在跑的会话你现在也压不了它，先知道它在跑更有用。"""
        hot_run = P("a:w.1", ctx=f"{dash.COMPACT_KB + 100}k (54%)", status="running")
        line = dash.member_line(hot_run, 33, 0)
        self.assertTrue(line.startswith("▶"))

    def test_窄框下框头仍然报该压的数量(self):
        s = dash.right_fit({"total": 1, "live": 1, "run": 0, "hot": 1}, 18)
        self.assertIn("压", s)

    def test_compose_窄宽度下提示不消失(self):
        snap = {"at": "", "checks": None, "services": None, "collector": None,
                "stale": None, "roles": None,
                "panes": [P(f"s{i}:w.1", ctx=f"{dash.COMPACT_KB + 50}k (52%)")
                          for i in range(5)]}
        text = "\n".join(t for t, _ in dash.compose(snap, 200))
        self.assertTrue("该压" in text or dash.COMPACT_MARK in text)


class TestGridLayout(unittest.TestCase):
    def test_同一行里所有框逐行等宽(self):
        """上下边框要逐行对得齐，只在底部补空行会让下边框错位。"""
        snap = {"at": "", "checks": None, "services": None, "collector": None,
                "stale": None, "roles": None,
                "panes": [P("a:w.1"), P("a:w.2"), P("a:w.3"), P("b:w.1")]}
        lines = [t for t, _ in dash.compose(snap, 120)]
        box = [x for x in lines if "┌" in x or "└" in x or "│" in x]
        self.assertTrue(box)
        self.assertEqual(len({dash.display_width(x) for x in box}), 1)

    def test_全画_不再有另有几个小组(self):
        ps = [P(f"s{i}:w.{j}") for i in range(8) for j in range(1, 9)]
        _, lost_g, lost_m = dash.group_panes(ps)
        self.assertEqual((lost_g, lost_m), (0, 0))

    def test_每组的成员一个不少(self):
        ps = [P(f"a:w.{j}") for j in range(1, 21)]
        g, _, _ = dash.group_panes(ps)
        self.assertEqual(len(g[0][1]), 20)
