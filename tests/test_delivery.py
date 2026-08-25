"""投递幂等的测试 —— 「同一条任务只投一次，但没人接住的必达条目要回来」。

这一层有两个反方向的静默故障，都不报错：

  1. **刷屏**：幂等破了，会话每次收尾都把同一条塞一遍。
  2. **黑洞**：收得太死。老实现是 `if r["id"] in ledger: continue` ——
     投过一次就**永远**不再投，不管有没有人接住。实测有一条投出去没被接住、
     triage 里一条记录都没有，挂了 25 小时才被发现。

现在的口径：只有「已 mark」才算真落地；没 mark 的**必达**条目过 15 分钟窗口重投，
非必达的保持老行为投一次就算（那些多半是私聊碎片，重投纯刷屏）。

全部在内存里跑：收件箱、两个台账、连「现在」都是参数。不碰 `data/`。

跑：python3 -m unittest discover -s tests -v
"""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dtwatch as dw


SID = "aaaa1111-2222-3333-4444-555566667777"
ELSE = "bbbb1111-2222-3333-4444-555566667777"
CFG = {"route": {"项目A需求群": SID, "别人的群": ELSE}}

NOW = dt.datetime(2026, 8, 25, 12, 0, 0)


def rec(rid, level="high", flags=("at_me",), conv="项目A需求群"):
    return {"id": rid, "conv": conv, "sender": "张三",
            "level": level, "flags": list(flags), "text": "帮我看下这个"}


def ago(minutes):
    return dw.ts(NOW - dt.timedelta(minutes=minutes))


def pick(records, triage=None, ledger=None, level="normal", at=NOW, session=SID):
    return dw.select_for_session(records, CFG, session, level,
                                 triage or {}, ledger or {}, at)


def ids(picked):
    return [r["id"] for r, _, _ in picked]


class 幂等主线(unittest.TestCase):
    def test_投一次记账之后就不再投(self):
        """这是幂等本身：投出去、记进台账、下一次收尾再问就没有了。"""
        ledger = {}
        first = pick([rec("m1")], ledger=ledger)
        self.assertEqual(ids(first), ["m1"])
        dw.stamp_delivered(ledger, [r for r, _, _ in first], NOW)
        self.assertEqual(ids(pick([rec("m1")], ledger=ledger)), [])

    def test_没记账就还会再投(self):
        """`--peek` 走的就是这条路：只看不记账，所以下一次照样能拿到。
        反过来说，忘了记账 = 刷屏。"""
        ledger = {}
        self.assertEqual(ids(pick([rec("m1")], ledger=ledger)), ["m1"])
        self.assertEqual(ids(pick([rec("m1")], ledger=ledger)), ["m1"])

    def test_记账用的是同一个时刻(self):
        picked = [rec("m1"), rec("m2")]
        ledger = dw.stamp_delivered({}, picked, NOW)
        self.assertEqual(ledger, {"m1": dw.ts(NOW), "m2": dw.ts(NOW)})


class 重投窗口(unittest.TestCase):
    def test_必达条目没人接窗口到了要重投(self):
        """钉住那个挂了 25 小时的黑洞：投过、没有任何 triage 记录、
        又是点名到他本人的，过了窗口必须回来。"""
        picked = pick([rec("m1")], ledger={"m1": ago(20)})
        self.assertEqual(ids(picked), ["m1"])
        self.assertEqual(picked[0][2], ago(20))      # 带着上次时间，调用方据此写日志

    def test_刚投过不重投(self):
        """窗口内不重投，否则每次收尾都塞一遍就是刷屏。"""
        self.assertEqual(ids(pick([rec("m1")], ledger={"m1": ago(5)})), [])

    def test_窗口边界是满15分钟就重投(self):
        """差一秒不投，整 15 分钟就投（判据是 `waited < 15*60` 才跳过）。

        把边界钉下来是因为它两边都疼：早一秒是刷屏，晚一秒是他等着的事没回来。
        """
        near = dw.ts(NOW - dt.timedelta(minutes=dw.REDELIVER_AFTER_MINUTES, seconds=-1))
        self.assertEqual(ids(pick([rec("m1")], ledger={"m1": near})), [])
        exact = dw.ts(NOW - dt.timedelta(minutes=dw.REDELIVER_AFTER_MINUTES))
        self.assertEqual(ids(pick([rec("m1")], ledger={"m1": exact})), ["m1"])

    def test_非必达的投一次就算了(self):
        """私聊碎片（"干掉"、"[语音通话] 通话时长 2:53"）级别是 high 只因为
        「私聊永远 high」，把这些也无限重投就是纯刷屏。"""
        r = rec("m1", flags=())
        self.assertEqual(ids(pick([r], ledger={"m1": ago(60)})), [])

    def test_有triage记录就不重投(self):
        """有 todo 之类的记录 = 他在跟了，别再塞。"""
        self.assertEqual(
            ids(pick([rec("m1")], triage={"m1": {"status": "todo"}}, ledger={"m1": ago(60)})),
            [])

    def test_台账时间戳坏了当成该重投(self):
        """宁可多投一次，也不能因为一个坏字符串让必达条目永远沉底。"""
        self.assertEqual(ids(pick([rec("m1")], ledger={"m1": "坏掉的时间戳"})), ["m1"])
        self.assertEqual(ids(pick([rec("m1")], ledger={"m1": ""})), ["m1"])


class 处置状态(unittest.TestCase):
    def test_已完成的不投(self):
        for status in ("done", "ignored"):
            self.assertEqual(ids(pick([rec("m1")], triage={"m1": {"status": status}})), [],
                             status)

    def test_snoozed没到期不投(self):
        until = dw.ts(NOW + dt.timedelta(hours=1))
        self.assertEqual(
            ids(pick([rec("m1")], triage={"m1": {"status": "snoozed", "until": until}})), [])

    def test_snoozed到期了要投(self):
        """到期不投 = 他按「待会儿再说」的事再也不会回来。"""
        until = dw.ts(NOW - dt.timedelta(minutes=1))
        self.assertEqual(
            ids(pick([rec("m1")], triage={"m1": {"status": "snoozed", "until": until}})), ["m1"])

    def test_snoozed没写until当成到期(self):
        self.assertEqual(ids(pick([rec("m1")], triage={"m1": {"status": "snoozed"}})), ["m1"])


class 归属与档位(unittest.TestCase):
    def test_不归本会话的不投(self):
        """投给错的会话 = 那边看不懂，这边永远等不到。"""
        self.assertEqual(ids(pick([rec("m1", conv="别人的群")])), [])

    def test_没配路由的不投(self):
        self.assertEqual(ids(pick([rec("m1", conv="没配的群")])), [])

    def test_low不塞给项目会话(self):
        """日报卡片、CI 推送、文件消息刷屏没意义。"""
        self.assertEqual(ids(pick([rec("m1", level="low")], level="normal")), [])
        self.assertEqual(ids(pick([rec("m1", level="low")], level="low")), ["m1"])

    def test_只要high的时候normal不投(self):
        self.assertEqual(ids(pick([rec("m1", level="normal")], level="high")), [])
        self.assertEqual(ids(pick([rec("m1", level="high")], level="high")), ["m1"])

    def test_level字段缺失或不认识当low(self):
        """采集写坏了字段不能让它冒充 high 插队。"""
        r = rec("m1")
        del r["level"]
        self.assertEqual(ids(pick([r], level="normal")), [])
        self.assertEqual(ids(pick([rec("m1", level="怪东西")], level="normal")), [])

    def test_带出路由标签(self):
        picked = pick([rec("m1")])
        self.assertEqual(picked[0][1], "项目A需求群")


class 判据本身不产生副作用(unittest.TestCase):
    def test_不改记录(self):
        """打 route_label 是调用方的事。判据顺手改记录的话，
        同一批记录跑两次结果就不一样了 —— 这种 bug 最难查。"""
        r = rec("m1")
        before = dict(r)
        pick([r])
        self.assertEqual(r, before)

    def test_不改台账(self):
        ledger = {"m1": ago(60)}
        triage = {"m2": {"status": "todo"}}
        pick([rec("m1"), rec("m2")], triage=triage, ledger=ledger)
        self.assertEqual(ledger, {"m1": ago(60)})
        self.assertEqual(triage, {"m2": {"status": "todo"}})

    def test_同样的输入跑两次结果一样(self):
        records = [rec("m1"), rec("m2", level="low")]
        a = ids(pick(records))
        b = ids(pick(records))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
