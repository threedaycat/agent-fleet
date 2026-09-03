"""picker 附加条目：舰队口径。

盯的是 2026-09-03 那个真问题：待投递队列里，会话已经不在 `fleet.sessions()` 里的，
以前会退回 `sid[:8]` 显示成一串 id，而且按回车跳无可跳。
`fleet.disp_of` 的 docstring 早就写死「不露 session_id 碎片」，那条退路是个漏洞。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import picker_items as pi                               # noqa: E402


class TestSplitQueues(unittest.TestCase):
    def test_有坐标的留下_没坐标的进孤儿(self):
        live = {"a": {"tmux": "OS:w.1"}}
        ok, orphan = pi.split_queues(live, {"a": 3, "b": 5})
        self.assertEqual(ok, [("a", 3)])
        self.assertEqual(orphan, [("b", 5)])

    def test_记录在但叫不出名字的也算孤儿(self):
        """判据是「给不给得出坐标」，不是「记录在不在」。
        第一版写成 `live.get(sid)` 的真假，`{}` falsy 直接判错。"""
        ok, orphan = pi.split_queues({"a": {}}, {"a": 3}, label=lambda r: "")
        self.assertEqual((ok, orphan), ([], [("a", 3)]))

    def test_叫得出名字就留下_哪怕记录是空字典(self):
        ok, _ = pi.split_queues({"a": {}}, {"a": 3}, label=lambda r: "OS:w.1")
        self.assertEqual(ok, [("a", 3)])

    def test_孤儿不是被丢掉而是被交出来(self):
        """**这条是重点。** 静默丢弃 = 把 625 条投不出去的消息说成 0 条。"""
        _, orphan = pi.split_queues({}, {"x": 439, "y": 186})
        self.assertEqual(sum(n for _, n in orphan), 625)

    def test_已经算进卡住的不重复出现(self):
        live = {"a": {}}
        ok, _ = pi.split_queues(live, {"a": 3}, taken=["a"], label=lambda r: "OS:w.1")
        self.assertEqual(ok, [])

    def test_零条的不算队列(self):
        ok, orphan = pi.split_queues({"a": {}}, {"a": 0, "b": 0},
                                     label=lambda r: "OS:w.1")
        self.assertEqual((ok, orphan), ([], []))

    def test_两边都按条数降序(self):
        live = {"a": {}, "b": {}}
        ok, orphan = pi.split_queues(live, {"a": 1, "b": 9, "c": 2, "d": 7},
                                     label=lambda r: "OS:w.1")
        self.assertEqual([n for _, n in ok], [9, 1])
        self.assertEqual([n for _, n in orphan], [7, 2])

    def test_空输入不炸(self):
        self.assertEqual(pi.split_queues({}, {}), ([], []))
        self.assertEqual(pi.split_queues(None, None), ([], []))


class TestRoleCoords(unittest.TestCase):
    def test_声明键换成运行期坐标(self):
        self.assertEqual(pi.role_coords({"OS:workOS#2": "desk"}), {"OS:workOS.2"})

    def test_窗口名里有冒号时只切第一个(self):
        """session 名后面的第一个冒号才是分隔符，窗口名里再有冒号是窗口名的一部分。"""
        self.assertEqual(pi.role_coords({"OS:a:b#1": "x"}), {"OS:a:b.1"})

    def test_残缺的键直接跳过不造假坐标(self):
        self.assertEqual(pi.role_coords({"OS:win": "x", "nohash": "y", "": "z"}), set())

    def test_空的给空集合(self):
        self.assertEqual(pi.role_coords({}), set())
        self.assertEqual(pi.role_coords(None), set())


class TestRoleAlerts(unittest.TestCase):
    def test_live_不出现(self):
        self.assertEqual(pi.role_alerts([{"role": "main", "state": "live"}]), [])

    def test_unknown_不出现(self):
        """读不出**不是结论**。排进待办会让他去修一个可能不存在的问题。"""
        self.assertEqual(pi.role_alerts([{"role": "x", "state": "unknown"}]), [])

    def test_三种坏状态都留下(self):
        rep = [{"role": "a", "state": "stale"}, {"role": "b", "state": "never"},
               {"role": "c", "state": "missing"}]
        self.assertEqual([r["role"] for r in pi.role_alerts(rep)], ["c", "b", "a"])

    def test_同状态按角色名排_顺序稳定(self):
        rep = [{"role": "z", "state": "stale"}, {"role": "a", "state": "stale"}]
        self.assertEqual([r["role"] for r in pi.role_alerts(rep)], ["a", "z"])

    def test_没有角色名也不炸(self):
        self.assertEqual(len(pi.role_alerts([{"state": "stale"}])), 1)

    def test_空的给空(self):
        self.assertEqual(pi.role_alerts([]), [])
        self.assertEqual(pi.role_alerts(None), [])


class TestNoSessionIdInRows(unittest.TestCase):
    """红线：附加条目里不许出现 session-id 碎片。"""

    def test_system_lines_里的孤儿行不露_sid(self):
        lines = pi.system_lines(orphans=[("7ac581c7-8af3-4d74-86a3-d3316c444341", 439)])
        joined = "\n".join(lines)
        self.assertNotIn("7ac581c7", joined)
        self.assertIn("439", joined)                    # 数字必须还在，不能静默吞掉

    def test_角色行显示坐标而不是_pane_id(self):
        lines = pi.system_lines(alerts=[{"role": "desk", "state": "stale",
                                         "coord": "OS:workOS.2", "pane": "%77",
                                         "note": "116k"}])
        joined = "\n".join(lines)
        self.assertIn("OS:workOS.2", joined)
        self.assertNotIn("%77", joined)

    def test_没有_pane_的角色退回角色名而不是空白(self):
        lines = pi.system_lines(alerts=[{"role": "watch", "state": "missing",
                                         "coord": None, "note": "声明了但 pane 不存在"}])
        self.assertIn("watch", "\n".join(lines))

    def test_没有孤儿时不出这一行(self):
        self.assertEqual(len(pi.system_lines()), 2)     # 区头 + 系统状态


class TestCmdListSourceHasNoSidFallback(unittest.TestCase):
    """`else sid[:8]` 那个退路必须真的没了 —— 注释里写着不算。"""

    def test_源码里不存在_sid_切片退路(self):
        import ast
        import inspect
        src = ast.unparse(ast.parse(inspect.getsource(pi.cmd_list)))
        self.assertNotIn("sid[:8]", src)


if __name__ == "__main__":
    unittest.main()
