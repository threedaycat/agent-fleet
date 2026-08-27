# -*- coding: utf-8 -*-
"""守候回信的判据。每个用例注释里写清它钉住的是**哪一种会静默漏事的情况**。

全部用造的数据，不碰 data/ —— 用 tests/_audit_run.py 跑可以看到这一点。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dtwatch as dw                                             # noqa: E402

# 通道 id 直接照抄真实形状（2026-08-27 量的 data/ 里 67 个通道）：
#   - **长度有 27 和 47 两种**，不是一种 —— 定长 fixture 会让「按长度截断/校验」
#     这类 bug 活下来；
#   - 任意两个通道的最长公共前缀只有 5 个字符（`cidyI` 这种），
#     且**没有一个通道 id 是另一个的子串**。
# 所以「精确匹配 vs 子串匹配」光靠两个 id 是测不出来的（两边行为一样），
# 真正要钉的是**截断的 id 不许匹配** —— 输出里到处印 `cid[:12]…`，
# 迟早有人把印出来的那截当 --cid 传回来。见 test_截断的cid不许匹配。
CID_甲 = "cidZZfake0000000AAAAAAAAA=="
CID_乙 = "cidZZfake1111111BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
SID_甲 = "70c7e4c2-90df-4f33-9f2a-59b427671086"
SID_乙 = "8d3553e9-8615-4bed-8c94-35bc956cc0c4"

T0 = "2026-08-27 10:00:00"


def rec(cid=CID_甲, time="2026-08-27 10:05:00", sender="小甲",
        sender_id="DwFAKE0000000000000000000000000甲", text="收到",
        mid=None, single=True):
    """一条 inbox 记录。字段照 cmd_poll 真正写进去的那些，不多不少。"""
    return {
        "id": mid or ("msg" + time.replace(" ", "").replace(":", "")
                      + "/" + cid[-6:]),
        "cid": cid, "conv": sender if single else "某个群",
        "single": single, "sender": sender, "sender_id": sender_id,
        "time": time, "text": text,
        "collected_at": "2026-08-27 10:09:00", "level": "high", "flags": [],
    }


class NewReplies(unittest.TestCase):
    def test_只认本通道(self):
        # 漏事形态：两个通道的 id 前 35 个字符一样，子串/前缀匹配会把别人的
        # 回信当成我等的那个人回了 —— 然后我按错的内容接着办。
        recs = [rec(cid=CID_乙, sender="小乙")]
        self.assertEqual(dw.new_replies(recs, CID_甲, T0), [])
        self.assertEqual(len(dw.new_replies(recs, CID_乙, T0)), 1)

    def test_截断的cid不许匹配(self):
        # 漏事形态：`cid[:12]…` 到处印在输出里（await --list、pending、日志），
        # 迟早有人把印出来的那截当 --cid 传回来。子串匹配下它「能用」——
        # 于是同前缀的另一个通道的消息被当成回信。实测真通道最长公共前缀只有
        # 5 个字符，所以只靠两个完整 id 是**测不出**精确/子串之别的（两边都对），
        # 必须专门钉截断这一例。
        recs = [rec(cid=CID_甲)]
        self.assertEqual(dw.new_replies(recs, CID_甲[:12], T0), [])
        self.assertEqual(dw.new_replies(recs, "cid", T0), [])
        self.assertEqual(len(dw.new_replies(recs, CID_甲, T0)), 1)

    def test_基线是严格大于(self):
        # 漏事形态：基线取「现在」，而恰好同一秒采到一条我发之前的消息 ——
        # 用 >= 就会把它当回信，假唤起一次，然后我按一条旧消息作决定。
        recs = [rec(time=T0)]
        self.assertEqual(dw.new_replies(recs, CID_甲, T0), [])
        self.assertEqual(len(dw.new_replies(recs, CID_甲, "2026-08-27 09:59:59")), 1)

    def test_基线之后的才算(self):
        recs = [rec(time="2026-08-27 09:00:00", text="旧的"),
                rec(time="2026-08-27 10:30:00", text="新的")]
        got = dw.new_replies(recs, CID_甲, T0)
        self.assertEqual([r["text"] for r in got], ["新的"])

    def test_按时间升序(self):
        # 漏事形态：多条一起到，顺序乱了，读的人以为最后一句是结论，其实是第一句。
        recs = [rec(time="2026-08-27 12:00:00", text="三"),
                rec(time="2026-08-27 10:30:00", text="一"),
                rec(time="2026-08-27 11:00:00", text="二")]
        self.assertEqual([r["text"] for r in dw.new_replies(recs, CID_甲, T0)],
                         ["一", "二", "三"])

    def test_from_按姓名认(self):
        # 群里人多时盯一个人。调用方手上通常只有姓名，没有 openDingTalkId。
        recs = [rec(sender="小甲", text="甲说"),
                rec(sender="小乙", sender_id="DwFAKE0000000000000000000000000乙",
                    text="乙说", time="2026-08-27 10:06:00")]
        got = dw.new_replies(recs, CID_甲, T0, only_from="小乙")
        self.assertEqual([r["text"] for r in got], ["乙说"])

    def test_from_按sender_id认(self):
        recs = [rec(sender="小甲", text="甲说")]
        got = dw.new_replies(recs, CID_甲, T0,
                             only_from="DwFAKE0000000000000000000000000甲")
        self.assertEqual([r["text"] for r in got], ["甲说"])

    def test_from_不匹配就一条都不给(self):
        # 漏事形态：--from 写错（比如给了 userId 而不是 sender_id），如果实现是
        # 「匹配不上就当没过滤」，就会把群里所有人的话都当成他回了。
        recs = [rec(sender="小甲")]
        self.assertEqual(dw.new_replies(recs, CID_甲, T0, only_from="不存在的人"), [])

    def test_没cid或没基线一律空(self):
        # 漏事形态：cid 认错成空串，如果实现把空串当「不过滤」，
        # 就会把**所有通道**的消息都当成回信。
        recs = [rec()]
        self.assertEqual(dw.new_replies(recs, "", T0), [])
        self.assertEqual(dw.new_replies(recs, CID_甲, ""), [])

    def test_没时间字段的记录不参与(self):
        # 采集器理论上总写 time，但一条坏记录不该把整个通道弄死或假命中。
        recs = [{"id": "x", "cid": CID_甲, "sender": "小甲", "text": "没时间"}]
        self.assertEqual(dw.new_replies(recs, CID_甲, T0), [])

    def test_空输入不炸(self):
        self.assertEqual(dw.new_replies(None, CID_甲, T0), [])
        self.assertEqual(dw.new_replies([], CID_甲, T0), [])

    def test_不排除自己是有意的(self):
        # 采集器在入库前就把自己发的滤掉了（cmd_poll 里 sender_id == self 那一支
        # 只更新 self_last、不写 inbox）。这条钉住那个前提：如果哪天采集器改成
        # 把自己的也写进来，这个测试仍然通过，但 new_replies 会把自己的话当回信 ——
        # 所以这里同时留个记号，改采集器的人要看到。
        import inspect
        self.assertIn("self_last", inspect.getsource(dw.cmd_poll))


class CollectorGap(unittest.TestCase):
    def test_从没跑过是None(self):
        self.assertIsNone(dw.collector_gap_minutes({}, dw.parse_ts(T0)))
        self.assertIsNone(dw.collector_gap_minutes(None, dw.parse_ts(T0)))

    def test_时间戳坏了是None不是0(self):
        # 漏事形态：坏时间戳当成 0 分钟 = 「采集器很健康」，于是安静等满 12 小时。
        self.assertIsNone(dw.collector_gap_minutes(
            {"last_poll": "昨天下午"}, dw.parse_ts(T0)))

    def test_算得出分钟数(self):
        gap = dw.collector_gap_minutes(
            {"last_poll": "2026-08-27 09:30:00"}, dw.parse_ts(T0))
        self.assertAlmostEqual(gap, 30.0, places=3)


class Verdict(unittest.TestCase):
    def setUp(self):
        self.at = dw.parse_ts("2026-08-27 10:10:00")
        self.deadline = dw.parse_ts("2026-08-27 22:00:00")
        self.fresh = {"last_poll": "2026-08-27 10:08:00"}

    # ⚠️ 不能写 `state or self.fresh`：`{}`（从没跑过）是 falsy，会被悄悄换成
    # 健康的 fixture，于是「从没跑过要报 blind」那条用例测的根本不是它想测的东西。
    _MISSING = object()

    def v(self, recs, state=_MISSING, at=None, deadline=None):
        return dw.watch_verdict(recs, CID_甲, T0, "",
                                self.fresh if state is self._MISSING else state,
                                at or self.at, deadline or self.deadline)

    def test_没消息就继续等(self):
        self.assertEqual(self.v([]), ("", None))

    def test_有回信就命中(self):
        action, payload = self.v([rec(time="2026-08-27 10:05:00")])
        self.assertEqual(action, "reply")
        self.assertEqual(len(payload), 1)

    def test_采集器停了要出声不是安静等(self):
        # 这是这个仓库栽过 8 次的形状：盯一个可能没人在写的面，
        # 「他没回」和「我看不见」长得一模一样。
        action, why = self.v([], state={"last_poll": "2026-08-27 09:00:00"})
        self.assertEqual(action, "blind")
        self.assertIn("70", why)          # 70 分钟

    def test_采集器从没跑过也是blind(self):
        self.assertEqual(self.v([], state={})[0], "blind")

    def test_回信优先于blind(self):
        # 消息已经落盘了，采集器此刻健不健康不影响「他回了」这个结论。
        # 反过来实现（先判 blind）就会在采集器刚挂的那一刻把已经到手的回信丢掉。
        action, _ = self.v([rec(time="2026-08-27 10:05:00")],
                           state={"last_poll": "2026-08-01 09:00:00"})
        self.assertEqual(action, "reply")

    def test_到点了才expire(self):
        # 采集器必须是**相对那个 at** 新鲜的，否则先撞上 blind —— 到期这条判据
        # 就永远测不到。（这正是 blind 排在 expire 前面的效果：采集器停着的时候
        # 「到期了他没回」是个没有信息量的结论。）
        at = self.deadline
        fresh_then = {"last_poll": dw.ts(at - dt.timedelta(minutes=2))}
        self.assertEqual(self.v([], state=fresh_then, at=at)[0], "expire")
        one_sec_before = at - dt.timedelta(seconds=1)
        self.assertEqual(self.v([], state=fresh_then, at=one_sec_before)[0], "")

    def test_回信优先于expire(self):
        # 漏事形态：最后一轮同时满足「有回信」和「到点」，先判到期就把回信丢了。
        action, _ = self.v([rec(time="2026-08-27 10:05:00")],
                           state={"last_poll": dw.ts(self.deadline)},
                           at=self.deadline)
        self.assertEqual(action, "reply")

    def test_hours为0时第一轮仍然查回信(self):
        # 「循环要写成先做一轮再判超时」那条坑的判据版：deadline == 起算时刻，
        # 若实现先判超时，「命中」和「不存在」会返回同一句话 —— 那种测试
        # 什么也证明不了。
        at = dw.parse_ts(T0)
        action, _ = dw.watch_verdict([rec(time="2026-08-27 09:59:00")], CID_甲,
                                     "2026-08-27 09:58:00", "", self.fresh, at, at)
        self.assertEqual(action, "reply")

    def test_stale阈值可调(self):
        state = {"last_poll": "2026-08-27 10:00:00"}      # 10 分钟前
        self.assertEqual(dw.watch_verdict([], CID_甲, T0, "", state, self.at,
                                          self.deadline, stale_minutes=20)[0], "")
        self.assertEqual(dw.watch_verdict([], CID_甲, T0, "", state, self.at,
                                          self.deadline, stale_minutes=5)[0], "blind")


class DropClaim(unittest.TestCase):
    def claim(self, cid=CID_甲, sid=SID_甲, until="2026-08-27 22:00:00"):
        return {"cid": cid, "sid": sid, "label": "小甲",
                "at": T0, "until": until}

    def test_只撤cid和sid都对上的(self):
        # 漏事形态：只按 cid 撤，就会把别的会话在同一个通道上的登记也清掉 ——
        # 而且是静默的，那个会话不会知道自己不再收得到回信了。
        claims = [self.claim(), self.claim(sid=SID_乙)]
        left = dw.drop_claim(claims, CID_甲, SID_甲, dw.parse_ts(T0))
        self.assertEqual([c["sid"] for c in left], [SID_乙])

    def test_同一会话别的通道不动(self):
        claims = [self.claim(), self.claim(cid=CID_乙)]
        left = dw.drop_claim(claims, CID_甲, SID_甲, dw.parse_ts(T0))
        self.assertEqual([c["cid"] for c in left], [CID_乙])

    def test_顺手扔掉过期的(self):
        claims = [self.claim(cid=CID_乙, until="2026-08-27 09:00:00")]
        self.assertEqual(dw.drop_claim(claims, CID_甲, SID_甲, dw.parse_ts(T0)), [])

    def test_撤不存在的不炸(self):
        self.assertEqual(dw.drop_claim([], CID_甲, SID_甲, dw.parse_ts(T0)), [])


class ResolveKind(unittest.TestCase):
    def setUp(self):
        # 同一个名字既是一个人也是一个群名 —— 真实会发生（"张三"和"张三对接群"不算，
        # 但"运维"这种既是人的花名也是群名的很常见）。
        self.recs = [
            dict(rec(cid=CID_甲, sender="小甲", single=True), time="2026-08-27 10:00:00"),
            dict(rec(cid=CID_乙, sender="别人", single=False), conv="小甲",
                 time="2026-08-27 11:00:00"),
        ]

    def test_默认只认私聊(self):
        # 漏事形态：混着认，「等小甲私聊回我」的登记落到一个同名的群上，
        # 他真回私聊时反而不算命中。
        self.assertEqual(dw.resolve_cid(self.recs, "小甲")[0], CID_甲)

    def test_group只认群(self):
        self.assertEqual(dw.resolve_cid(self.recs, "小甲", "group")[0], CID_乙)

    def test_any两个都算候选(self):
        cid, n = dw.resolve_cid(self.recs, "小甲", "any")
        self.assertEqual(n, 2)
        self.assertEqual(cid, CID_乙)          # 最近的那个赢

    def test_认不出返回空(self):
        self.assertEqual(dw.resolve_cid(self.recs, "没这个人"), ("", 0))
        self.assertEqual(dw.resolve_cid(self.recs, "小甲", "group"),
                         (CID_乙, 1))


if __name__ == "__main__":
    unittest.main()
