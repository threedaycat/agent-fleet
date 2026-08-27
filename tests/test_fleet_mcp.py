# -*- coding: utf-8 -*-
"""fleet-mcp 的输出契约。每个用例注释里写清它钉住的是**哪一种会静默漏事的情况**。

全部用造的数据，不碰 data/ —— 用 tests/_audit_run.py 跑可以看到这一点。
契约测试的关键是**负对照**：喂进去带毒的记录，断言毒没出去。
只断言「白名单字段都在」是正对照，它在实现完全没过滤的情况下也通过。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet_mcp as fm                                          # noqa: E402


def poisoned(extra=None):
    """一条把每个 DENY 字段都塞满可识别毒物的记录。

    毒物按 DENY **自身**生成，所以以后往 DENY 里加字段，这些用例自动覆盖它 ——
    不需要有人记得回来补一行。
    """
    rec = {k: "LEAK-%s" % k for k in fm.DENY}
    rec.update(extra or {})
    return rec


class Scrub(unittest.TestCase):
    def test_任意深度的DENY键都删掉(self):
        # 漏事形态：只在顶层删，嵌一层的照样出去。工具返回里天然有嵌套
        # （sessions 是 list of dict，items 也是）。
        leak = {k: 1 for k in fm.DENY}
        o = {"ok": 1, "深一层": dict(leak, 深两层=dict(leak)),
             "列表": [dict(leak), "普通字符串", 42, None]}
        out = fm.scrub(o)
        flat = repr(out)
        for k in fm.DENY:
            self.assertNotIn(k, flat, "DENY 字段 %s 漏出去了" % k)
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["列表"][1:], ["普通字符串", 42, None])

    def test_不许顺手删白名单字段(self):
        # 反方向的漏事：过滤写太狠，把该出的字段也删了，模型看到空结果
        # 会以为「没有会话」而不是「工具坏了」。
        keep = {k: "v" for k in fm.SESSION_KEYS + fm.QUEUE_KEYS + fm.AWAIT_KEYS}
        self.assertEqual(fm.scrub(keep), keep)

    def test_scrub看键不看内容(self):
        # 明确记下这一层的限度：DENY 挡的是**键名**。一个 cid 出现在
        # text 的正文里，scrub 拦不住，也不该拦（那是别人发的消息原文）。
        o = {"text": "他把 cid 发在群里了：cidZZfake0000000AAAAAAAAA=="}
        self.assertEqual(fm.scrub(o), o)


class Pick(unittest.TestCase):
    def test_只有白名单活下来(self):
        got = fm.pick(poisoned({"who": "甲 fun:1", "无关字段": "x"}),
                      fm.SESSION_KEYS)
        self.assertEqual(set(got), set(fm.SESSION_KEYS))

    def test_None就是None不是空字典(self):
        # 「查不到这个会话」和「查到了但字段全空」是两件事，模型得能分辨。
        self.assertIsNone(fm.pick(None, fm.SESSION_KEYS))
        self.assertEqual(fm.pick({}, ("a",)), {"a": None})


class SessionRow(unittest.TestCase):
    def rec(self, **kw):
        base = dict(poisoned(), pane="%275", tmux="OS:2.1", known="live",
                    window_name="✳ workOS", cwd="/Users/lsy/workos",
                    state="idle", age="1844", project="workos", note="在改判据")
        base.update(kw)
        return base

    def test_哨兵不许当数据报出去(self):
        # 漏事形态：fleet.build_sessions 用 age=10**6 表示「记得这个会话但
        # 不知道多久没动」。原样报出去就是 idle_seconds=1000000，
        # 模型会说「闲了 11 天」—— 那是**撒谎**，而且看起来完全合理。
        row = fm.session_row(self.rec(age=fm.AGE_UNKNOWN), "甲")
        self.assertIsNone(row["idle_seconds"])
        self.assertEqual(row["known"], "live")     # 但来源要说清楚
        row2 = fm.session_row(self.rec(age=fm.AGE_UNKNOWN + 5), "甲")
        self.assertIsNone(row2["idle_seconds"], "比哨兵还大的也是不知道")

    def test_正常的age照常出(self):
        self.assertEqual(fm.session_row(self.rec(age="1844"), "甲")["idle_seconds"],
                         1844)

    def test_坏age不炸也不冒充0(self):
        # 漏事形态：坏值当 0 = 「刚刚才动过」，比 null 更糟。
        for bad in ("", None, "刚刚", [], {}):
            with self.subTest(age=bad):
                self.assertIsNone(fm.session_row(self.rec(age=bad), "甲")["idle_seconds"])

    def test_note要截断(self):
        # 这一层存在的意义是减少 context。实测有会话的 note 是几百字带代码块的，
        # 原样带出去就反了。
        long = "x" * 900
        row = fm.session_row(self.rec(note=long), "甲")
        self.assertLess(len(row["note"]), 200)
        self.assertIn("截断", row["note"])

    def test_短note一字不改(self):
        row = fm.session_row(self.rec(note="在改判据"), "甲")
        self.assertEqual(row["note"], "在改判据")

    def test_alive走routable不看pane字段有没有值(self):
        # 漏事形态：2026-08-25 实测 107 个会话里 45 个 known=gone 的死会话
        # pane 字段照样有值。只看有没有值 → 死会话被当收件人 → 指令静默消失。
        dead = self.rec(pane="%44", known="gone")
        self.assertFalse(fm.session_row(dead, "甲")["alive"])
        self.assertTrue(fm.session_row(self.rec(), "甲")["alive"])
        nopane = self.rec(pane="")
        self.assertFalse(fm.session_row(nopane, "甲")["alive"])

    def test_不带session_id出去(self):
        # 用户的口径：说会话用 tmux 名 + 坐标，绝不甩 session-id。
        row = fm.session_row(self.rec(sid="70c7e4c2-90df-4f33-9f2a-59b427671086"),
                             "甲 fun:1")
        self.assertNotIn("sid", row)
        self.assertNotIn("70c7e4c2", repr(row))
        self.assertEqual(row["who"], "甲 fun:1")

    def test_名字由调用方给不在这里重新拼(self):
        # 人看的会话名只有 fleet.disp_of 一处算得出，复制一份就会漂。
        import inspect
        src = inspect.getsource(fm.session_row)
        body = src.replace(fm.session_row.__doc__ or "", "")
        self.assertNotIn("disp_of", body)
        self.assertNotIn("window_name\", \"\").lstrip", body)


class QueueRow(unittest.TestCase):
    def rec(self, **kw):
        base = dict(poisoned(), id="msgFAKE0001", level="high", flags=["at_me"],
                    conv="某个群", sender="小甲", single=False,
                    time="2026-08-27 10:00:00", text="这条给你")
        base.update(kw)
        return base

    def test_毒物一个都不出去(self):
        row = fm.queue_row(self.rec(), "小甲", "")
        for k in fm.DENY:
            self.assertNotIn(k, row, "%s 漏了" % k)
        self.assertNotIn("LEAK", repr(row))

    def test_message_id必须出(self):
        # mark 吃的是 message id。不给 id，模型就没法把条目关掉 ——
        # 这个仓库的历史缺口正是「台账不回收」。
        self.assertEqual(fm.queue_row(self.rec(), "", "")["id"], "msgFAKE0001")

    def test_重投要说成人话不是丢个时间戳(self):
        # 漏事形态：把 prev 时间戳丢给模型让它自己判「这是不是重投」。
        # 「投过又没人 mark」曾经让一条必达消息挂 25 小时，这件事该被说出来。
        plain = fm.queue_row(self.rec(), "小甲", "")
        self.assertEqual(plain["redelivery"], "")
        again = fm.queue_row(self.rec(), "小甲", "2026-08-27 09:30:00")
        self.assertIn("2026-08-27 09:30:00", again["redelivery"])
        self.assertIn("mark", again["redelivery"])


class AwaitRow(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 27, 10, 0, 0)

    def test_算剩余分钟(self):
        row = fm.awaiting_row({"label": "小甲", "until": "2026-08-27 12:30:00"},
                              "甲 fun:1", self.NOW)
        self.assertEqual(row["expired_in_minutes"], 150)

    def test_坏时间戳是None不是0(self):
        # 漏事形态：坏值当 0 = 「马上过期」，模型会去重挂一个不该挂的守候。
        row = fm.awaiting_row({"label": "小甲", "until": "明天中午"},
                              "甲", self.NOW)
        self.assertIsNone(row["expired_in_minutes"])

    def test_没记名字要说出来不能空着(self):
        # 漏事形态：label 为空时给个空串，读的人以为「在等一个没有名字的人」，
        # 分不清是没登记名字还是数据坏了。
        row = fm.awaiting_row({"until": "2026-08-27 12:00:00"}, "甲", self.NOW)
        self.assertIn("没记名字", row["who_we_wait_for"])

    def test_不带sid出去(self):
        row = fm.awaiting_row({"label": "小甲", "sid": "70c7e4c2-90df",
                               "cid": "cidZZfake0000000AAAAAAAAA==",
                               "until": "2026-08-27 12:00:00"}, "甲", self.NOW)
        self.assertNotIn("70c7e4c2", repr(row))
        self.assertNotIn("cidZZfake", repr(row))


class Boundary(unittest.TestCase):
    """最后一道：把每个工具的 fn 换成「返回带毒记录」，走 handle() 看毒有没有出去。

    前面几个类测的是整形函数。这个类测的是**边界** —— 将来有人加了新工具、
    忘了走 pick()，scrub 这道兜底还在不在。
    """
    def call(self, name, args=None):
        r = fm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args or {}}})
        return r["result"]["content"][0]["text"]

    def test_每个工具的边界都不漏毒(self):
        for t in fm.TOOLS:
            with self.subTest(工具=t["name"]):
                orig = t["fn"]
                t["fn"] = lambda a: {"rows": [poisoned({"who": "甲"})],
                                     "深": {"更深": poisoned()}}
                try:
                    body = self.call(t["name"])
                finally:
                    t["fn"] = orig
                for k in fm.DENY:
                    self.assertNotIn('"%s"' % k, body, "%s 漏了" % k)
                self.assertNotIn("LEAK", body)

    def test_工具抛异常按isError回不按JSON_RPC_error(self):
        # JSON-RPC error 会让有些客户端直接断开，模型看不到原因也就没法换个查法。
        t = fm.TOOLS[0]
        orig = t["fn"]

        def boom(a):
            raise RuntimeError("毒-LEAK-cid")
        t["fn"] = boom
        try:
            r = fm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": t["name"], "arguments": {}}})
        finally:
            t["fn"] = orig
        self.assertNotIn("error", r)
        self.assertTrue(r["result"]["isError"])
        self.assertIn("RuntimeError", r["result"]["content"][0]["text"])

    def test_没有这个工具是JSON_RPC_error(self):
        r = fm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "不存在的工具", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_一期不许有写工具(self):
        """**这条是给二期立的闸。** 一期只读，写操作走 desk 审批 ——
           理由不是洁癖：实测 --allowedTools / --disallowedTools 不是边界
           （agent 用 ToolSearch 找到 Monitor，那个工具入参里有 command）。
           边界拦不住的东西就不能做成工具。加写工具时这条会红，那时要连同
           SERVICE 说明一起改，不是顺手把断言删掉。"""
        self.assertEqual(sorted(t["name"] for t in fm.TOOLS),
                         ["awaiting", "fleet_status", "my_queue"])
        for t in fm.TOOLS:
            desc = t["description"]
            for 写 in ("投递", "send-keys", "发送", "标记", "写入"):
                if 写 in desc and "不" not in desc:
                    self.fail("%s 的说明像个写工具：%s" % (t["name"], desc))

    def test_协议握手回客户端要的版本(self):
        # 写死一个版本会跟新客户端对不上，而对不上的表现是「工具一个都不出现」。
        r = fm.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"], "2099-01-01")
        r2 = fm.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}})
        self.assertEqual(r2["result"]["protocolVersion"], fm.FALLBACK_PROTOCOL)

    def test_通知不回复(self):
        # 对通知（没有 id）回一条报文会让有些客户端报协议错。
        self.assertIsNone(fm.handle({"jsonrpc": "2.0",
                                     "method": "notifications/initialized"}))
        self.assertIsNone(fm.handle({"jsonrpc": "2.0", "method": "谁知道这是啥"}))

    def test_带id的initialized不许回错误(self):
        """**这条才是「认得 initialized」和「靠 rid is None 兜底」的分界线。**

        没有 id 的通知两种实现都不回（末尾 `if rid is None: return None` 兜住了），
        所以光测无 id 的通知**测不出**有没有真的认得这个方法 —— 那个变异体
        因此存活过一轮。有客户端会把 initialized 当请求发（带 id），
        这时落到「method not found」会让握手直接失败，而失败的表现是
        「工具一个都不出现」，极难查。"""
        for m in ("notifications/initialized", "initialized"):
            with self.subTest(方法=m):
                r = fm.handle({"jsonrpc": "2.0", "id": 7, "method": m})
                if r is not None:
                    self.assertNotIn("error", r, "%s 带 id 时回了错误" % m)

    def test_tools_list给全三个且带schema(self):
        r = fm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = r["result"]["tools"]
        self.assertEqual(len(tools), 3)
        for t in tools:
            self.assertEqual(set(t), {"name", "description", "inputSchema"})
            self.assertNotIn("fn", t)      # 别把 lambda 序列化出去


if __name__ == "__main__":
    unittest.main()


class Truncation(unittest.TestCase):
    """静默截断读起来跟「全都在这儿」一模一样。"""

    def test_截断了要说出来(self):
        # 漏事形态：工具返回 40 条、系统里有 63 条，模型拿 40 条当全集下结论。
        # 第一次真跑 claude -p 时它自己发现了（"工具只返回了前 40 条"）——
        # 但设计不该靠模型注意到。
        n = fm.truncation_note(matched=63, limit=40, unknown_idle=0)
        self.assertIn("63", n)
        self.assertIn("40", n)
        self.assertIn("别拿", n)

    def test_没截断就不要制造噪音(self):
        self.assertEqual(fm.truncation_note(matched=5, limit=40, unknown_idle=0), "")
        self.assertEqual(fm.truncation_note(matched=40, limit=40, unknown_idle=0), "",
                         "刚好等于 limit 不算截断")

    def test_哨兵条数也要说(self):
        # 「闲最久的是谁」这个结论只在能读出时长的那批里成立。
        n = fm.truncation_note(matched=5, limit=40, unknown_idle=20)
        self.assertIn("20", n)
        self.assertIn("null", n)
        self.assertIn("known", n)

    def test_两种情况同时出现都要说(self):
        n = fm.truncation_note(matched=63, limit=40, unknown_idle=20)
        self.assertIn("63", n)
        self.assertIn("20", n)
