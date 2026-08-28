# -*- coding: utf-8 -*-
"""自动压缩的判据。**兜底，不抢正常判断。**

用户 2026-08-28 原话：「那这个会有主动的 compress 吗？甚至是主动的 clear 吗？
因为我不能每次都手动做这些吧」。当时的现状是：`fleet.py compact` 写得很细，
但**全仓零调用方，只能手敲**；本机 7 个定时任务没有一个碰上下文。

阈值故意比他 CLAUDE.md 里那条 200k 高。那条是给**会话自己**用的——会话知道
自己有没有活干到一半。外部看门狗看不出「闲着」和「等我确认下一步」的区别。

全部用造的数据，不碰 data/、不抓屏。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet                                                     # noqa: E402

AT = dt.datetime(2026, 8, 28, 15, 0, 0)


def rec(state="idle", pane="%275", known="live", age="1800"):
    return {"sid": "s1", "pane": pane, "known": known, "state": state,
            "tmux": "OS:2.1", "window_name": "workOS", "cwd": "/Users/lsy/workos",
            "project": "workos", "age": age, "note": ""}


class ShouldCompact(unittest.TestCase):
    def ok(self, *a, **kw):
        return fleet.should_compact(*a, **kw)

    def test_超阈值且空闲就压(self):
        yes, why = self.ok(rec(), 420, 300, AT)
        self.assertTrue(yes)
        self.assertIn("420", why)

    def test_没超就不压(self):
        yes, why = self.ok(rec(), 180, 300, AT)
        self.assertFalse(yes)
        self.assertIn("阈值", why)

    def test_刚好等于阈值要压(self):
        # 边界：写成 > 的话卡在阈值上的会话永远不压。
        self.assertTrue(self.ok(rec(), 300, 300, AT)[0])
        self.assertFalse(self.ok(rec(), 299, 300, AT)[0])

    def test_读不出数字一律不压(self):
        """**这条最重要。** `parse_ctx_usage` 解析不到时老实返回 None。
           把 None 当"没超"会漏压（无害），当"超了"会**在不知道状态的情况下
           往别人输入框发 /compact**（有害）。页脚格式以后变了就会走到这条。"""
        yes, why = self.ok(rec(), None, 300, AT)
        self.assertFalse(yes)
        self.assertIn("读不出", why)

    def test_busy不压(self):
        # 压缩本质是往别人输入框发字，会打断正在输出的会话。
        yes, why = self.ok(rec(state="busy"), 999, 300, AT)
        self.assertFalse(yes)
        self.assertIn("正在跑", why)

    def test_pane没了不压(self):
        # 走 routable：不是看 pane 字段有没有值。2026-08-25 实测 107 个会话里
        # 45 个 known=gone 的死会话 pane 字段照样有值。
        self.assertFalse(self.ok(rec(known="gone"), 999, 300, AT)[0])
        self.assertFalse(self.ok(rec(pane=""), 999, 300, AT)[0])

    def test_冷却期内不压(self):
        # 压完可能还是很高（长会话压一次降不到阈值以下）。没有冷却就会每轮
        # 都压它一次 —— 那比不压更糟。
        刚压过 = fleet.ts(AT - dt.timedelta(minutes=10))
        yes, why = self.ok(rec(), 999, 300, AT, last_at=刚压过, cooldown_minutes=45)
        self.assertFalse(yes)
        self.assertIn("冷却", why)

    def test_冷却期外要压(self):
        久 = fleet.ts(AT - dt.timedelta(minutes=46))
        self.assertTrue(self.ok(rec(), 999, 300, AT, last_at=久, cooldown_minutes=45)[0])

    def test_冷却台账坏了不当成刚压过(self):
        # 漏事形态：坏时间戳当"刚刚压过"→ 永远不压，而且没人知道为什么。
        self.assertTrue(self.ok(rec(), 999, 300, AT, last_at="上午")[0])

    def test_不压也要给理由(self):
        # 看门狗静默不动时，「它判断过了」和「它挂了」必须能分开。
        for kb, r in ((None, rec()), (100, rec()), (999, rec(state="busy")),
                      (999, rec(known="gone"))):
            with self.subTest(kb=kb, state=r["state"], known=r["known"]):
                yes, why = self.ok(r, kb, 300, AT)
                self.assertFalse(yes)
                self.assertTrue(why.strip(), "不压必须说为什么")


class PickTargets(unittest.TestCase):
    def rows(self, *kbs):
        """⚠️ **每行必须给不同的 pane。**

        第一版这个 helper 让所有行共用 `pane="%275"`（`rec()` 的默认值），
        比生产干净——真实会话每个都有自己的 pane。按 pane 去重之后，
        三条用例全塌成一条并失败，**不是因为它们要测的东西错了**。
        今天第三次栽在"fixture 比生产干净"上。
        """
        return [("s%d" % i, rec(pane="%%%d" % (100 + i)), kb)
                for i, kb in enumerate(kbs)]

    def test_按占用从高到低(self):
        take, _ = fleet.pick_compact_targets(self.rows(320, 800, 500), 300, AT, cap=3)
        self.assertEqual([t[2] for t in take], [800, 500, 320])

    def test_一轮有上限(self):
        # 每次压缩都要发字并等它降（最多 90 秒）。一轮压十个会把巡检拖成十几
        # 分钟，而这中间会话状态早变了——拿旧读数去压是另一种"用过期的面推断"。
        take, skip = fleet.pick_compact_targets(self.rows(900, 800, 700, 600), 300,
                                                AT, cap=2)
        self.assertEqual([t[2] for t in take], [900, 800])
        self.assertEqual(len(skip), 2)

    def test_超额的也要报理由不是静默丢掉(self):
        _, skip = fleet.pick_compact_targets(self.rows(900, 800), 300, AT, cap=1)
        self.assertTrue(any("名额" in why for _, why in skip))

    def test_没有够阈值的就一个都不选(self):
        take, skip = fleet.pick_compact_targets(self.rows(100, 200), 300, AT)
        self.assertEqual(take, [])
        self.assertEqual(len(skip), 2)

    def test_空输入不炸(self):
        self.assertEqual(fleet.pick_compact_targets([], 300, AT), ([], []))
        self.assertEqual(fleet.pick_compact_targets(None, 300, AT), ([], []))

    def test_读不出数字的排不进去也不影响别人(self):
        rows = [("a", rec(), None), ("b", rec(), 500)]
        take, skip = fleet.pick_compact_targets(rows, 300, AT)
        self.assertEqual([t[0] for t in take], ["b"])
        self.assertTrue(any("读不出" in why for _, why in skip))


class NoClear(unittest.TestCase):
    def test_不许出现clear(self):
        """**这条是给未来立的闸。** `/clear` 会把开 pane 时喂进去的角色定义
           一起切掉，那个 pane 之后就是个没有角色的裸 Claude。
           「角色被压淡」的正解是每轮注入，不是清空。"""
        import inspect
        for f in (fleet.should_compact, fleet.pick_compact_targets,
                  fleet.cmd_autocompact):
            src = inspect.getsource(f)
            src = src.replace(f.__doc__ or "", "")      # 剥掉 docstring 再断言
            self.assertNotIn("/clear", src, "%s 里出现了 /clear" % f.__name__)

    def test_复用已有的compact不重写一份(self):
        import inspect
        src = inspect.getsource(fleet.cmd_autocompact)
        src = src.replace(fleet.cmd_autocompact.__doc__ or "", "")
        self.assertIn("cmd_compact(", src, "必须走已有那条（三道护栏 + 重发原任务）")
        self.assertNotIn("tmux_send(", src, "不许自己往 pane 发字，那是第二份真相")


if __name__ == "__main__":
    unittest.main()


class MinIdleAndDedupe(unittest.TestCase):
    def rec2(self, age, state="idle", pane="%275", known="live"):
        return {"sid": "s1", "pane": pane, "known": known, "state": state,
                "tmux": "OS:2.1", "window_name": "workOS", "cwd": "/Users/lsy/workos",
                "project": "workos", "age": age, "note": ""}

    def test_刚动过不压(self):
        """**这条是第一次真跑 dry-run 就撞上的。** state=idle 分不清「闲了
           半小时」和「两轮对话之间闲了 20 秒」——后者正是有人在跟它说话的时候。
           第一次 dry-run 选中的正是当时正在对话的那个会话（523k、idle）。"""
        yes, why = fleet.should_compact(self.rec2("20"), 999, 300, AT)
        self.assertFalse(yes)
        self.assertIn("还在动", why)

    def test_安静够久了才压(self):
        yes, _ = fleet.should_compact(self.rec2("1200"), 999, 300, AT)
        self.assertTrue(yes)

    def test_刚好等于min_idle要压(self):
        self.assertTrue(fleet.should_compact(
            self.rec2("900"), 999, 300, AT, min_idle_seconds=900)[0])
        self.assertFalse(fleet.should_compact(
            self.rec2("899"), 999, 300, AT, min_idle_seconds=900)[0])

    def test_age是哨兵值不压(self):
        # fleet.build_sessions 用 age=10**6 表示"记得但不知道多久没动"。
        # 原样当"安静了很久"处理就是把哨兵当数据用——今天已经在 fleet_mcp
        # 那边治过一次同样的坑，这里不能重犯。
        yes, why = fleet.should_compact(self.rec2(10**6), 999, 300, AT)
        self.assertFalse(yes)
        self.assertIn("哨兵", why)

    def test_age读不出不压(self):
        for bad in ("", None, "刚刚"):
            with self.subTest(age=bad):
                self.assertFalse(fleet.should_compact(self.rec2(bad), 999, 300, AT)[0])

    def test_同一个pane只算一次(self):
        """2026-08-28 第一次 dry-run 就撞上：同一个 pane 被三个不同的 sid
           指着（会话在同一个 pane 里重开过），真跑会把同一个窗口压三遍。"""
        rows = [("sidA", self.rec2("1000", pane="%458"), 500),
                ("sidB", self.rec2("1000", pane="%458"), 500),
                ("sidC", self.rec2("1000", pane="%458"), 500)]
        take, skip = fleet.pick_compact_targets(rows, 300, AT)
        self.assertEqual(len(take), 1)
        self.assertEqual(sum(1 for _, why in skip if "共用 pane" in why), 2)

    def test_不同pane都保留(self):
        rows = [("sidA", self.rec2("1000", pane="%1"), 500),
                ("sidB", self.rec2("1000", pane="%2"), 400)]
        take, _ = fleet.pick_compact_targets(rows, 300, AT)
        self.assertEqual(len(take), 2)


if __name__ == "__main__":
    unittest.main()


class ClassifyResult(unittest.TestCase):
    """压完一个之后算什么。三类不许混成一个 "compacted"。"""

    def test_降了算成功(self):
        kind, note = fleet.classify_compact_result(500, 120, 45)
        self.assertEqual(kind, "dropped")
        self.assertEqual(note, "")

    def test_没降不算成功(self):
        """**这条钉的是我自己造过的假成功。** 第一版把"发出去了但数字没降"
           丢进 compacted 报成功。实测那次 381k 的会话压了 4 分钟才降，
           超时返回时数字还是 381 —— 报成功就是撒谎。"""
        kind, note = fleet.classify_compact_result(381, 381, 45)
        self.assertEqual(kind, "sent_not_settled")
        self.assertIn("还没降", note)
        self.assertIn("45", note, "要告诉读的人冷却多久，否则他不知道下一轮会不会重压")

    def test_涨了也不算成功(self):
        # 会话在压缩期间又被派了活，数字反而涨 —— 不能因为"我发过 /compact"就算成。
        self.assertEqual(fleet.classify_compact_result(381, 420, 45)[0],
                         "sent_not_settled")

    def test_读不出单独一类(self):
        kind, note = fleet.classify_compact_result(381, None, 45)
        self.assertEqual(kind, "unreadable")
        self.assertIn("pane", note)

    def test_压前读不出也不许算成功(self):
        # kb_before 为 None 时无法比较，只能说"不知道"，不能默认成功。
        self.assertEqual(fleet.classify_compact_result(None, 100, 45)[0],
                         "sent_not_settled")

    def test_超时不等于失败(self):
        # 实测 381k 压一次约 4 分钟，所以"超时了还没降"很常见，
        # 它是"还没到"不是"失败"——说明里必须让人看出这一点。
        _, note = fleet.classify_compact_result(381, 381, 45)
        self.assertIn("很可能还在跑", note)


class Reentrancy(unittest.TestCase):
    def test_超时默认值要够大(self):
        # 一轮 3 个 × 每个 7 分钟 = 21 分钟。手敲那条 90 秒是给小会话的，
        # 这个功能专治大会话：实测 381k 压到 84% 就已经 2 分 42 秒。
        self.assertGreaterEqual(fleet.AUTOCOMPACT_TIMEOUT_SECONDS, 300)

    def test_有锁(self):
        # 定时间隔比一轮耗时短的时候，两个进程会同时往同一个 pane 发 /compact。
        import inspect
        src = inspect.getsource(fleet.cmd_autocompact)
        self.assertIn("flock", src)
        self.assertIn("LOCK_NB", src, "拿不到锁要直接退，不许排队——"
                                      "排到上一轮跑完，这一轮的读数早过期了")


if __name__ == "__main__":
    unittest.main()
