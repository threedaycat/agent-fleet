# -*- coding: utf-8 -*-
"""把死掉的角色拉起来 —— 以及**哪些不许拉**。

用户 2026-08-28 原话：「desk 死掉了，你不应该让这个系统有一个维护状态吗？
就是把死掉的拉起来？？」

需要这一层是因为 `cmd_up` 补不了 live session 里缺失的窗口 —— 它撞见已存在的
session 就整个跳过。所以 `remote` 那个 pane 一旦没了，再也不会被建起来。

**这个文件一半在测「不修」。** 「把死掉的拉起来」听起来只有一种动作，实际上
四种不正常各有各的处理，而且三种「修了会更糟」：

| 状态 | 动作 | 为什么 |
|---|---|---|
| `missing` | 建 | pane 真的不存在 |
| `never` | 不动 | pane 在，它不是死了是没活干。重启没用 |
| `stale` | 不动 | pane 有上下文，重启 = 丢上下文，那是破坏 |
| `unknown` | 不动 | 读不出，不知道该做什么。不猜 |

全部用造的数据 + 临时目录，不碰 data/、不动 tmux。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet_up                                                  # noqa: E402

AT = 1_787_900_000.0
MIN = 60.0


def r(role, state, pane="%1"):
    return {"role": role, "state": state, "pane": pane, "coord": "OS:x.1", "note": ""}


def actions(plan):
    return {p["role"]: p["action"] for p in plan}


class HealPlan(unittest.TestCase):
    def test_只有missing才建(self):
        plan = fleet_up.heal_plan(
            [r("remote", "missing", None), r("main", "live"),
             r("duty", "never"), r("watch", "stale"), r("doctor", "unknown")], AT)
        self.assertEqual(actions(plan), {"remote": "build", "main": "skip",
                                         "duty": "skip", "watch": "skip",
                                         "doctor": "skip"})

    def test_三种不修的理由各不相同(self):
        """如果三种都给同一句话，那这层判据就白写了 —— 用户看不出为什么不修。"""
        plan = fleet_up.heal_plan(
            [r("a", "never"), r("b", "stale"), r("c", "unknown")], AT)
        whys = [p["why"] for p in plan]
        self.assertEqual(len(set(whys)), 3, whys)
        for w in whys:
            self.assertTrue(w.strip())

    def test_never的理由必须说重启没用(self):
        """这是最容易被误修的一个：pane 在、看起来能重启，重启完还是 0k。"""
        plan = fleet_up.heal_plan([r("duty", "never")], AT)
        self.assertIn("重启没用", plan[0]["why"])

    def test_stale的理由必须说会丢上下文(self):
        plan = fleet_up.heal_plan([r("watch", "stale")], AT)
        self.assertIn("上下文", plan[0]["why"])

    def test_冷却期内不重试(self):
        """一个角色文件坏了的话，没有冷却会让它每分钟建一个失败的窗口。"""
        plan = fleet_up.heal_plan([r("remote", "missing", None)], AT,
                                  {"remote": AT - 10 * MIN}, cooldown_minutes=60)
        self.assertEqual(plan[0]["action"], "skip")
        self.assertIn("10 分钟前", plan[0]["why"])

    def test_冷却期过了再试(self):
        plan = fleet_up.heal_plan([r("remote", "missing", None)], AT,
                                  {"remote": AT - 61 * MIN}, cooldown_minutes=60)
        self.assertEqual(plan[0]["action"], "build")

    def test_没试过就直接建(self):
        plan = fleet_up.heal_plan([r("remote", "missing", None)], AT, {})
        self.assertEqual(plan[0]["action"], "build")

    def test_上次时间读不出不当成没试过(self):
        """读不出 ≠ 没试过。当成没试过就等于没有冷却。"""
        for bad in ("昨天", {}, [], "null"):
            plan = fleet_up.heal_plan([r("remote", "missing", None)], AT,
                                      {"remote": bad})
            self.assertEqual(plan[0]["action"], "skip", bad)
            self.assertIn("读不出", plan[0]["why"])

    def test_上次时间是0不算读不出(self):
        """0 是 epoch，是个合法时间戳（远古），冷却早就过了 → 该建。"""
        plan = fleet_up.heal_plan([r("remote", "missing", None)], AT, {"remote": 0})
        self.assertEqual(plan[0]["action"], "build")

    def test_空报告给空计划(self):
        self.assertEqual(fleet_up.heal_plan([], AT), [])

    def test_未知状态也不动(self):
        plan = fleet_up.heal_plan([r("x", "什么鬼")], AT)
        self.assertEqual(plan[0]["action"], "skip")
        self.assertTrue(plan[0]["why"].strip())


class HealLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = fleet_up.HEAL_LOG
        fleet_up.HEAL_LOG = os.path.join(self.dir, "heal_log.ndjson")

    def tearDown(self):
        fleet_up.HEAL_LOG = self.saved

    def test_读得出上次尝试(self):
        """⚠️ 这条是个真实 bug 的回归测试：`heal_attempts` 用了 `json` 而
        `fleet_up` 顶层**没有** `import json`（只有某个函数里有个局部的）。
        日志文件不存在时函数提前返回，所以一直没炸 —— 跟 MARK_RE 那次一模一样，
        只在特定路径才触发的 NameError。"""
        with open(fleet_up.HEAL_LOG, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "remote", "at": 111.0}) + "\n")
        self.assertEqual(fleet_up.heal_attempts(), {"remote": 111.0})

    def test_后写覆盖先写(self):
        with open(fleet_up.HEAL_LOG, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "remote", "at": 111.0}) + "\n")
            f.write(json.dumps({"role": "remote", "at": 222.0}) + "\n")
        self.assertEqual(fleet_up.heal_attempts()["remote"], 222.0)

    def test_坏行跳过不炸(self):
        with open(fleet_up.HEAL_LOG, "w", encoding="utf-8") as f:
            f.write("这不是 json\n")
            f.write(json.dumps({"role": "a", "at": 1.0}) + "\n")
            f.write("\n")
        self.assertEqual(fleet_up.heal_attempts(), {"a": 1.0})

    def test_没有role的行不收(self):
        """收进来会变成 `{None: …}`，然后 `heal_plan` 拿一个 None 键去查冷却。"""
        with open(fleet_up.HEAL_LOG, "w", encoding="utf-8") as f:
            f.write(json.dumps({"at": 1.0, "ok": True}) + "\n")
            f.write(json.dumps({"role": "", "at": 2.0}) + "\n")
            f.write(json.dumps({"role": "a", "at": 3.0}) + "\n")
        self.assertEqual(fleet_up.heal_attempts(), {"a": 3.0})

    def test_文件不存在给空(self):
        self.assertEqual(fleet_up.heal_attempts(), {})

    def test_写进去读得回来(self):
        fleet_up.note_attempt("remote", 333.0, True, "")
        self.assertEqual(fleet_up.heal_attempts(), {"remote": 333.0})

    def test_写的是追加不是覆盖(self):
        """`save_json` 是原子替换但**没有锁**，并发写者会互相盖掉（outbox 踩过）。"""
        fleet_up.note_attempt("a", 1.0, True)
        fleet_up.note_attempt("b", 2.0, False, "坏了")
        self.assertEqual(set(fleet_up.heal_attempts()), {"a", "b"})


class RoleWindow(unittest.TestCase):
    CFG = {"sessions": [
        {"name": "OS", "windows": [
            {"name": "workOS", "panes": [{"role": "main"}, {"role": "desk"}]},
            {"name": "dtremote", "panes": [{"role": "remote"}]}]},
        {"name": "proj", "windows": [{"name": "w", "panes": [{}]}]}]}

    def test_找得到(self):
        sess, w = fleet_up.role_window(self.CFG, "remote")
        self.assertEqual(sess, "OS")
        self.assertEqual(w["name"], "dtremote")

    def test_同窗口第二个pane也找得到(self):
        sess, w = fleet_up.role_window(self.CFG, "desk")
        self.assertEqual((sess, w["name"]), ("OS", "workOS"))

    def test_找不到给两个None(self):
        self.assertEqual(fleet_up.role_window(self.CFG, "不存在"), (None, None))

    def test_没有role的pane不匹配(self):
        self.assertEqual(fleet_up.role_window(self.CFG, None), (None, None))

    def test_空配置不炸(self):
        self.assertEqual(fleet_up.role_window({}, "main"), (None, None))


if __name__ == "__main__":
    unittest.main()
