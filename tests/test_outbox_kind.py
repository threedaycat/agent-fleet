# -*- coding: utf-8 -*-
"""outbox 里两种东西 —— 推送同一条管道，**计数必须分开**。

用户 2026-08-28 问「嗯 outbox 吗？」，说的是 duty 上报的事该不该塞进 outbox。
查了字段之后的答复是「是，但不能直接塞」：

- 草稿（`kind="draft"`）：有收件人 `to_label`、有拟好的 `draft`，批准后要发出去；
- 拿主意（`kind="ask"`）：没有收件人、没有拟稿，`suggest` 是给**他**的建议，
  不是给对方的回复。

硬塞在一起有个具体后果：console 那行写的是「**待他拍板的草稿** N 条」，
混进去他看到 5 条会以为有 5 条待发消息，其实几条根本没有收件人。

这个文件守两件事：**分得开**，以及**老记录不能因为没有 kind 字段就消失**。

全部用造的数据，不碰 data/。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dtwatch                                                   # noqa: E402
import duty                                                      # noqa: E402

AT = dt.datetime(2026, 8, 28, 19, 0, 0)


def draft(entries=(), **kw):
    kw.setdefault("to_label", "甲")
    kw.setdefault("cid", "cidXXXX")
    kw.setdefault("draft", "好的，明天给你")
    return dtwatch.make_draft(list(entries), AT, **kw)


def ask(entries=(), **kw):
    kw.setdefault("about_text", "他 08-11 要个测试入口，17 天没动静")
    kw.setdefault("suggest", "问一下还要不要补")
    return dtwatch.make_ask(list(entries), AT, **kw)


class EntryKind(unittest.TestCase):
    def test_没有kind字段算草稿(self):
        """⚠️ **没有 kind = 草稿**，不是「未知」。kind 是 2026-08-28 才加的，
        在那之前落盘的每一条都是草稿。当成未知会让老记录从计数里消失。"""
        self.assertEqual(dtwatch.entry_kind({"id": "ob-1"}), dtwatch.KIND_DRAFT)

    def test_认得出两种(self):
        self.assertEqual(dtwatch.entry_kind({"kind": "ask"}), dtwatch.KIND_ASK)
        self.assertEqual(dtwatch.entry_kind({"kind": "draft"}), dtwatch.KIND_DRAFT)

    def test_不认识的值退回草稿(self):
        for bad in ("", None, "什么鬼", 3, []):
            self.assertEqual(dtwatch.entry_kind({"kind": bad}), dtwatch.KIND_DRAFT, bad)

    def test_空记录不炸(self):
        self.assertEqual(dtwatch.entry_kind({}), dtwatch.KIND_DRAFT)
        self.assertEqual(dtwatch.entry_kind(None), dtwatch.KIND_DRAFT)


class MakeAsk(unittest.TestCase):
    def test_没有收件人也没有拟稿(self):
        """有收件人就意味着「有条消息要发」，那是谎话。"""
        e = ask()
        self.assertEqual(e["to_label"], "")
        self.assertEqual(e["cid"], "")
        self.assertEqual(e["draft"], "")

    def test_带上建议(self):
        self.assertEqual(ask()["suggest"], "问一下还要不要补")

    def test_kind是ask(self):
        self.assertEqual(dtwatch.entry_kind(ask()), dtwatch.KIND_ASK)

    def test_草稿的suggest是空(self):
        self.assertEqual(draft()["suggest"], "")

    def test_两种字段形状一样(self):
        """同一个 ndjson 里混着，形状不一样下游就得到处判空。"""
        self.assertEqual(set(draft()), set(ask()))

    def test_编号跟草稿连着排(self):
        a = draft()
        b = ask([a])
        c = draft([a, b])
        self.assertEqual([a["id"], b["id"], c["id"]],
                         ["ob-260828-1", "ob-260828-2", "ob-260828-3"])


class Counting(unittest.TestCase):
    def setUp(self):
        self.a = draft()
        self.b = ask([self.a])
        self.old = dict(draft([self.a, self.b]))
        self.old.pop("kind")                  # 模拟 kind 字段之前落盘的老记录
        self.all = [self.a, self.b, self.old]

    def test_草稿数不含ask(self):
        ids = [e["id"] for e in dtwatch.pending_drafts(self.all)]
        self.assertNotIn(self.b["id"], ids)
        self.assertEqual(len(ids), 2)          # 新草稿 + 老记录

    def test_老记录算进草稿(self):
        self.assertIn(self.old["id"], [e["id"] for e in dtwatch.pending_drafts(self.all)])

    def test_ask数只有ask(self):
        self.assertEqual([e["id"] for e in dtwatch.pending_asks(self.all)], [self.b["id"]])

    def test_两边加起来等于全部(self):
        self.assertEqual(len(dtwatch.pending_drafts(self.all))
                         + len(dtwatch.pending_asks(self.all)),
                         len(dtwatch.pending_entries(self.all)))

    def test_推送候选两种都要(self):
        """挡在推送外面的话，duty 上报的事就永远到不了他手机。"""
        self.assertEqual(len(dtwatch.unnotified(self.all)), 3)

    def test_处理过的不再算(self):
        done = dict(self.b, status="sent")
        self.assertEqual(dtwatch.pending_asks([self.a, done]), [])


class FormatPush(unittest.TestCase):
    def test_ask不写成回某人(self):
        """写成「回 ?」他会去找一条并不存在的待发消息。"""
        out = dtwatch.format_push([ask()], 1)
        self.assertIn("要你拿个主意", out)
        self.assertNotIn("回 ?", out)
        self.assertNotIn("拟回", out)

    def test_ask带上是什么事和建议(self):
        out = dtwatch.format_push([ask()], 1)
        self.assertIn("什么事：", out)
        self.assertIn("我建议：", out)

    def test_全是ask时不说不能从手机发(self):
        """根本没有要发的东西，那句话只会让他去找一条不存在的消息。"""
        out = dtwatch.format_push([ask()], 1)
        self.assertNotIn("不能从手机发", out)
        self.assertIn("回一句你的判断", out)

    def test_有草稿时那句话要在(self):
        out = dtwatch.format_push([draft()], 1)
        self.assertIn("不能从手机发", out)

    def test_混着时头部把成分说清楚(self):
        a = draft()
        out = dtwatch.format_push([a, ask([a])], 2)
        self.assertIn("1 条待发", out)
        self.assertIn("1 条等你拿主意", out)

    def test_只有草稿时头部不啰嗦(self):
        out = dtwatch.format_push([draft()], 1)
        self.assertIn("新增 1 条", out)
        self.assertNotIn("等你拿主意", out)

    def test_草稿仍然带拟稿(self):
        out = dtwatch.format_push([draft()], 1)
        self.assertIn("拟回：", out)


class PushToOutbox(unittest.TestCase):
    def test_没有要报的就不碰文件(self):
        self.assertEqual(duty.push_to_outbox([], AT), 0)

    def test_落盘失败要报出来不能静默(self):
        """静默失败会让他以为「没有事要处理」—— 那正是这条通道要消灭的失效模式。"""
        saved = dtwatch.read_outbox
        dtwatch.read_outbox = lambda: (_ for _ in ()).throw(OSError("盘满了"))
        try:
            n = duty.push_to_outbox([{"id": "m1", "why": "w", "suggest": "s"}], AT)
        finally:
            dtwatch.read_outbox = saved
        self.assertEqual(n, -1)                # 不是 0 —— 0 意味着「没有要报的」

    def test_一轮多条编号不重复(self):
        saved_r, saved_a = dtwatch.read_outbox, dtwatch.append_outbox
        written = []
        dtwatch.read_outbox = lambda: []
        dtwatch.append_outbox = written.extend
        try:
            n = duty.push_to_outbox(
                [{"id": f"m{i}", "why": f"w{i}", "suggest": f"s{i}"} for i in range(3)], AT)
        finally:
            dtwatch.read_outbox, dtwatch.append_outbox = saved_r, saved_a
        self.assertEqual(n, 3)
        self.assertEqual(len({e["id"] for e in written}), 3)
        self.assertTrue(all(dtwatch.entry_kind(e) == dtwatch.KIND_ASK for e in written))
        self.assertTrue(all(e["by"] == "duty" for e in written))


class NotifyWiring(unittest.TestCase):
    """判据对了不代表接线对了。`push_outbox` 全仓只有手敲 `outbox notify` 会调到，
    没有任何定时任务碰它 —— 所以 duty 写完 ask 必须自己推一下，否则那条 ask
    躺在 ndjson 里，跟没写一样。"""

    def setUp(self):
        self.saved = {n: getattr(dtwatch, n) for n in ("load_json", "push_outbox")}

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(dtwatch, n, v)

    def test_读不到配置就不推(self):
        dtwatch.load_json = lambda *a, **k: None
        dtwatch.push_outbox = lambda *a: self.fail("配置都读不到就不该推")
        self.assertEqual(duty.notify_outbox(), -1)

    def test_必须把真配置传下去(self):
        """给 `{}` 的话免打扰时段和最小间隔全部失效 —— 那两条正是
        「不因为是自动化就绕过约束」的口径。"""
        seen = {}
        dtwatch.load_json = lambda *a, **k: {"quiet_hours": [0, 7], "x": 1}
        dtwatch.push_outbox = lambda cfg, at: seen.update(cfg=cfg) or {"ok": True,
                                                                       "pushed": 2}
        self.assertEqual(duty.notify_outbox(), 2)
        self.assertEqual(seen["cfg"], {"quiet_hours": [0, 7], "x": 1})

    def test_推失败返回负一(self):
        dtwatch.load_json = lambda *a, **k: {"a": 1}
        dtwatch.push_outbox = lambda cfg, at: {"ok": False, "err": "免打扰"}
        self.assertEqual(duty.notify_outbox(), -1)

    def test_抛异常也不炸(self):
        dtwatch.load_json = lambda *a, **k: {"a": 1}
        dtwatch.push_outbox = lambda cfg, at: (_ for _ in ()).throw(RuntimeError("x"))
        self.assertEqual(duty.notify_outbox(), -1)

    def test_不自己拼推送文本也不自己发(self):
        """提醒通道只有 `send_reminder` 一条，duty 不许绕过去自己发。

        ⚠️ 断言必须看**代码**，不能看源文本。今天已经栽过四次：三次是
        docstring 里出现关键字让断言假过，第四次是**注释** ——
        `cmd_run` 的注释里写着「由 send_reminder 管」，`inspect.getsource`
        照样能搜到。`ast.unparse` 一次去掉注释，docstring 再单独剥。
        """
        import ast
        import inspect

        def code_only(f):
            tree = ast.parse(inspect.getsource(f).lstrip())
            body = tree.body[0]
            if (body.body and isinstance(body.body[0], ast.Expr)
                    and isinstance(body.body[0].value, ast.Constant)
                    and isinstance(body.body[0].value.value, str)):
                body.body.pop(0)               # 剥掉 docstring
            return ast.unparse(tree)           # unparse 不带注释

        for fn in ("notify_outbox", "push_to_outbox", "cmd_run"):
            src_fn = code_only(getattr(duty, fn))
            for forbidden in ("send_reminder", "format_push", "requests"):
                self.assertNotIn(forbidden, src_fn, f"{fn} 里不该有 {forbidden}")
        self.assertIn("push_outbox", inspect.getsource(duty))

    def test_这条断言真的看得见代码(self):
        """上一条如果 `code_only` 写错（比如把整个函数体都吃掉），它会永远通过。
        用一个**确实存在**的调用证明断言还在看东西。"""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(duty.notify_outbox).lstrip())
        tree.body[0].body.pop(0)
        src = ast.unparse(tree)
        self.assertIn("push_outbox", src)
        self.assertIn("CONFIG_PATH", src)


if __name__ == "__main__":
    unittest.main()


class AiTag(unittest.TestCase):
    """发消息一律不带「通过AI发送」角标。

    用户的长期口径：`dws` 默认 `--ai-tag=true`，那个角标会被引用、被截图、
    被带进汇报。2026-08-28 才发现全仓**一处都没加过** —— 在这之前每条提醒
    都带着角标发出去了。

    断言看代码不看源文本（`ast.unparse` 去注释）—— 今天第四次栽在这上面，
    而这条尤其危险：注释里写着 `--ai-tag=false` 也能让断言假过。
    """

    def _code(self, fn):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(fn).lstrip())
        b = tree.body[0]
        if (b.body and isinstance(b.body[0], ast.Expr)
                and isinstance(b.body[0].value, ast.Constant)
                and isinstance(b.body[0].value.value, str)):
            b.body.pop(0)
        return ast.unparse(tree)

    def test_两条发送路径都带上了(self):
        import dtcc
        for fn in (dtwatch.send_reminder, dtcc.send):
            src = self._code(fn)
            self.assertIn("message", src)
            self.assertIn("--ai-tag=false", src,
                          f"{fn.__name__} 少了 --ai-tag=false")

    def test_全仓没有别的发送路径(self):
        """新增一处发送而忘了加角标参数，这条会红。"""
        import os
        import re
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for name in os.listdir(base):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(base, name), encoding="utf-8").read()
            for m in re.finditer(r'"chat",\s*"message",\s*"send"', src):
                tail = src[m.start():m.start() + 400]
                if "--ai-tag=false" not in tail:
                    hits.append(f"{name}:{src[:m.start()].count(chr(10)) + 1}")
        self.assertEqual(hits, [], f"这些发送点没加 --ai-tag=false：{hits}")
