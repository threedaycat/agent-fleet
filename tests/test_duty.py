# -*- coding: utf-8 -*-
"""duty 派活器的判据 —— 重点是「怎么知道它干对了」。

用户 2026-08-28 原话：「我再想是不是应该要有一个 agent 系统来调度这些呀，正好我也想
学习一下做 agent」。当时实测：六个角色里四个从没干过活，角色名在派活逻辑里出现 0 次。

一个 harness 有四件事要回答：什么时候叫它、喂什么、怎么调、**怎么知道它干对了**。
前三件写错了看得见，第四件写错了看不见 —— 模型编一个不存在的 id、或者悄悄漏掉几条，
输出照样是一个格式正确的 JSON。所以这个文件绝大部分在守第四件：

1. **不许编造 id**：上报的每个 id 必须来自这一批输入；
2. **不许悄悄漏**：输入的每一条都要恰好出现一次；
3. 任何一条不过 → 整轮作废、**不写报告**。半个报告比没有报告更糟 ——
   下游分不清少的那半是「判断为不用报」还是「根本没看」。

全部用造的数据，不碰 data/、不调模型。
"""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import duty                                                      # noqa: E402

AT = dt.datetime(2026, 8, 28, 18, 0, 0)


def rec(mid="m1", time="2026-08-20 10:00:00", sender="甲", text="要个数", single=False):
    return {"id": mid, "time": time, "sender": sender, "text": text,
            "single": single, "conv": "某群", "flags": ["at_me"], "内部": "不该外流"}


def blob(esc=(), absorbed=()):
    return json.dumps({"escalate": [dict(id=i, why="w", suggest="s") for i in esc],
                       "absorbed": list(absorbed)}, ensure_ascii=False)


class Project(unittest.TestCase):
    def test_只导出白名单字段(self):
        p = duty.project(rec())
        self.assertEqual(set(p), {"id", "time", "from", "where", "text"})
        self.assertNotIn("内部", p)          # 上游加字段不会悄悄流出去
        self.assertNotIn("flags", p)

    def test_正文截断(self):
        p = duty.project(rec(text="啊" * 500))
        self.assertLessEqual(len(p["text"]), duty.TEXT_CHARS + 1)
        self.assertTrue(p["text"].endswith("…"))

    def test_短正文不加省略号(self):
        self.assertEqual(duty.project(rec(text="短"))["text"], "短")

    def test_私聊和群分得开(self):
        self.assertEqual(duty.project(rec(single=True))["where"], "私聊")
        self.assertEqual(duty.project(rec(single=False))["where"], "某群")

    def test_换行折成空格(self):
        self.assertEqual(duty.project(rec(text="a\n\nb  c"))["text"], "a b c")


class PickBatch(unittest.TestCase):
    def items(self, n=5):
        return [duty.project(rec(f"m{i}", f"2026-08-{10 + i:02d} 10:00:00"))
                for i in range(n)]

    def test_滤掉报过的(self):
        b = duty.pick_batch(self.items(3), {"m1"}, cap=9)
        self.assertEqual([x["id"] for x in b], ["m0", "m2"])

    def test_从旧到新(self):
        """积压 319 条时，按新到旧排会让最旧的永远轮不到 ——
        而「已经错过时效」本身就是要上报的事。"""
        b = duty.pick_batch(self.items(4), set(), cap=9)
        self.assertEqual([x["id"] for x in b], ["m0", "m1", "m2", "m3"])

    def test_截到上限(self):
        self.assertEqual(len(duty.pick_batch(self.items(30), set(), cap=12)), 12)

    def test_全报过就空(self):
        self.assertEqual(duty.pick_batch(self.items(2), {"m0", "m1"}, cap=9), [])

    def test_没时间的不崩(self):
        b = duty.pick_batch([{"id": "x"}, duty.project(rec("y"))], set(), cap=9)
        self.assertEqual(len(b), 2)


class ParseReport(unittest.TestCase):
    IDS = ["m1", "m2", "m3"]

    def ok(self, text):
        rep, why = duty.parse_report(text, self.IDS)
        self.assertIsNotNone(rep, f"本该通过却被拒：{why}")
        return rep

    def bad(self, text, expect):
        rep, why = duty.parse_report(text, self.IDS)
        self.assertIsNone(rep, f"本该被拒却通过了：{rep}")
        self.assertIn(expect, why)

    def test_正常(self):
        rep = self.ok(blob(esc=["m1"], absorbed=["m2", "m3"]))
        self.assertEqual(rep["escalate"][0]["id"], "m1")
        self.assertEqual(rep["absorbed"], ["m2", "m3"])

    def test_编造id被拒(self):
        """最典型的幻觉。格式完全正确，所以只有这条判据挡得住。"""
        self.bad(blob(esc=["m1", "m999"], absorbed=["m2", "m3"]), "编造")

    def test_漏了被拒(self):
        self.bad(blob(esc=["m1"], absorbed=["m2"]), "漏了")

    def test_重复被拒(self):
        self.bad(blob(esc=["m1"], absorbed=["m1", "m2", "m3"]), "多次")

    def test_全部消化也算合法(self):
        rep = self.ok(blob(absorbed=["m1", "m2", "m3"]))
        self.assertEqual(rep["escalate"], [])

    def test_围栏被剥掉(self):
        self.ok("```json\n" + blob(esc=["m1"], absorbed=["m2", "m3"]) + "\n```")

    def test_不是JSON被拒(self):
        self.bad("我看了一下，m1 需要你处理。", "不是 JSON")

    def test_不是对象被拒(self):
        self.bad('["m1","m2","m3"]', "不是一个对象")

    def test_字段类型不对被拒(self):
        self.bad('{"escalate": "m1", "absorbed": []}', "必须都是数组")

    def test_条目没有id被拒(self):
        self.bad('{"escalate": [{"why":"w"}], "absorbed": ["m1","m2","m3"]}', "没有 id")

    def test_多给的字段被丢掉(self):
        """白名单：模型多给的字段一概不收，免得下游哪天依赖一个没约定过的字段。"""
        raw = json.dumps({"escalate": [{"id": "m1", "why": "w", "suggest": "s",
                                        "priority": "P0", "自作主张": 1}],
                          "absorbed": ["m2", "m3"], "note": "多余"})
        rep = self.ok(raw)
        self.assertEqual(set(rep["escalate"][0]), set(duty.ITEM_KEYS))
        self.assertEqual(set(rep), set(duty.REPORT_KEYS))

    def test_空批次时空输出合法(self):
        rep, why = duty.parse_report('{"escalate":[],"absorbed":[]}', [])
        self.assertEqual(rep, {"escalate": [], "absorbed": []})

    def test_失败时报告绝不半成品(self):
        """拒的时候必须是 None，不能是「尽力而为」的部分结果。"""
        for t in ("垃圾", blob(esc=["m1"], absorbed=["m2"]), '{"escalate":1}'):
            rep, _ = duty.parse_report(t, self.IDS)
            self.assertIsNone(rep, t[:20])


class ShouldRun(unittest.TestCase):
    def test_没跑过就跑(self):
        go, why = duty.should_run({}, AT)
        self.assertTrue(go)
        self.assertIn("还没跑过", why)

    def test_间隔内不跑(self):
        st = {"last_run": (AT - dt.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")}
        go, why = duty.should_run(st, AT, 60)
        self.assertFalse(go)
        self.assertIn("10 分钟前", why)

    def test_到点了跑(self):
        st = {"last_run": (AT - dt.timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S")}
        self.assertTrue(duty.should_run(st, AT, 60)[0])

    def test_时间戳读不出当成刚跑过(self):
        """读不出**不等于**没跑过。当成没跑过，一个坏时间戳就会让它每分钟叫一次模型。"""
        for bad in ("昨天", "", 123, None, {}):
            st = {"last_run": bad}
            go, why = duty.should_run(st, AT)
            if bad in ("", None):
                self.assertTrue(go, bad)          # 空 = 真的没跑过
            else:
                self.assertFalse(go, bad)
                self.assertIn("读不出", why)

    def test_不跑也必须给理由(self):
        """静默跳过看起来像「跑过了没事」。"""
        st = {"last_run": AT.strftime("%Y-%m-%d %H:%M:%S")}
        go, why = duty.should_run(st, AT, 60)
        self.assertFalse(go)
        self.assertTrue(why.strip())


class BuildPrompt(unittest.TestCase):
    def test_角色定义原样进去(self):
        p = duty.build_prompt("你叫 duty，只读。", "本机口径", [duty.project(rec("m1"))])
        self.assertIn("你叫 duty，只读。", p)
        self.assertIn("本机口径", p)

    def test_id都在提示词里(self):
        batch = [duty.project(rec("mA")), duty.project(rec("mB"))]
        p = duty.build_prompt("r", "t", batch)
        self.assertIn("mA", p)
        self.assertIn("mB", p)

    def test_输出契约写进去了(self):
        p = duty.build_prompt("r", "t", [duty.project(rec("m1"))])
        self.assertIn("escalate", p)
        self.assertIn("absorbed", p)
        self.assertIn("不许漏", p)
        self.assertIn("不许出现别的", p)

    def test_没有本机口径也能拼(self):
        p = duty.build_prompt("r", "", [duty.project(rec("m1"))])
        self.assertIn("没有本机口径", p)


class ReadOnly(unittest.TestCase):
    def test_不给模型任何工具(self):
        """duty.md 的边界：只读，不改代码、不改配置、不发消息。
        所以 `--allowedTools ""` —— 它只需要看数据然后回 JSON。"""
        import inspect
        src = inspect.getsource(duty.ask_claude)
        src = src.replace(duty.ask_claude.__doc__ or "", "")   # docstring 不算实现
        self.assertIn('"--allowedTools", ""', src)
        for forbidden in ("Bash", "Write", "Edit", "--dangerously"):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
