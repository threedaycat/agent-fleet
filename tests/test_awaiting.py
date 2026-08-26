"""「在等回信」的登记怎么认回原主 —— `match_awaiting()` / `route_of(claims=)`。

这一层要解决的具体缺口：Claude 私聊完一个同事、等对方回话，回信来了没有任何
地方记录过「是我发的、我在等」，于是落回哨兵视图给人看，没有会话拿到，
**而且不报错**。实测全部历史 759 条单聊里 602 条（79%）就是这么散掉的。

每个用例的注释写清它钉住的是**哪一种会静默漏事的情况** —— 这一层所有的坏法
都不抛异常：要么投给错的会话，要么谁都没投，两种都得靠盯着才发现。

范围：**只有纯判据**。登记侧（发的时候写一笔）、死会话过滤的接线、
把发送包一层，都不在这一批里。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dtwatch as dw

SID_A = "aaaa1111-0000-0000-0000-000000000000"
SID_B = "bbbb2222-0000-0000-0000-000000000000"
# 用真实形状的 cid。实测长这样：cidZZfake0000000AAAAAAAAA=
# —— **全都以 "cid" 开头、都是长 base64**。一开始我写的是 "cid张==" / "cid李=="，
# 前 4 个字符就分岔了，于是「把精确比较写成前缀/子串匹配」这个坑（`match_broadcast`
# 正栽在它的 40 字符前缀上）在测试里根本露不出来 —— 变异测试抓到的。
CID_张 = "cidAaBbCcDdEeFf0011/2233445566778899AaBbCc="
CID_李 = "cidAaBbCcDdEeFf0011/2233445566778899DdEeFf="

NOW = dt.datetime(2026, 8, 25, 16, 0, 0)


def rec(cid=CID_张, sender="张三", conv="张三", single=True):
    """一条单聊记录。字段名跟 data/inbox.ndjson 里真实记录一致
       （id/cid/conv/single/sender/sender_id/time/text/level/flags）。"""
    return {"id": "m1", "cid": cid, "conv": conv, "single": single,
            "sender": sender, "sender_id": "uid1", "time": "2026-08-25 15:59:00",
            "text": "好的我看下", "level": "high", "flags": ["dm"]}


def claim(sid=SID_A, cid=CID_张, minutes=60, label=""):
    """一条登记。`until` = NOW + minutes。"""
    c = {"cid": cid, "sid": sid, "at": dw.ts(NOW),
         "until": dw.ts(NOW + dt.timedelta(minutes=minutes))}
    if label:
        c["label"] = label
    return c


class 认回原主(unittest.TestCase):

    def test_cid_命中就归登记的会话(self):
        """漏了这条 = Claude 等的回信永远到不了它手里，人得手工转述。"""
        self.assertEqual(dw.match_awaiting([claim()], rec(), NOW), (SID_A, ""))

    def test_登记排在静态表之前(self):
        """静默漏事：这个人**同时**配了静态路由。如果静态表赢，
           那么「谁发起的对话谁收回信」就永远不成立 —— 回信被投给
           配置里写的那个会话，发起方一直等不到，而且两边都不报错。"""
        cfg = {"route": {"张三": SID_B}}
        self.assertEqual(dw.route_of(rec(), cfg), (SID_B, "张三"))          # 没登记：静态表
        self.assertEqual(dw.route_of(rec(), cfg, [claim(sid=SID_A)], NOW),
                         (SID_A, "张三"))                                   # 有登记：登记赢

    def test_两条登记同一个通道_后登记的赢(self):
        """两个会话先后私聊了同一个人。规则跟 match_broadcast 一致
           （「他引用的总是最近那条」）。**这是真实歧义，不是边角**——
           定不下来就会随列表顺序漂。"""
        got = dw.match_awaiting([claim(sid=SID_A), claim(sid=SID_B)], rec(), NOW)
        self.assertEqual(got, (SID_B, ""))

    def test_不同通道互不串台(self):
        """静默漏事最难查的一种：投出去了、有会话收到了，但是**错的那个**。
           收到的会话会认真处理一条跟它无关的回信。"""
        claims = [claim(sid=SID_A, cid=CID_张), claim(sid=SID_B, cid=CID_李)]
        self.assertEqual(dw.match_awaiting(claims, rec(cid=CID_张), NOW)[0], SID_A)
        self.assertEqual(dw.match_awaiting(claims, rec(cid=CID_李), NOW)[0], SID_B)

    def test_登记里的_label_原样用_没写就退回对方名字(self):
        self.assertEqual(dw.match_awaiting([claim(label="等威总确认")], rec(), NOW),
                         (SID_A, "等威总确认"))
        self.assertEqual(dw.route_of(rec(), {}, [claim()], NOW), (SID_A, "张三"))


class 认不回来的时候(unittest.TestCase):
    """每条都必须**落回原行为**（静态表 / 给人看），绝不能变成黑洞。
       今天上午修的就是这一类：`if r["id"] in ledger: continue` 让一条
       投出去没人接的条目挂了 25 小时，全程不报错。"""

    def test_cid_不同就落回静态表(self):
        cfg = {"route": {"张三": SID_B}}
        self.assertEqual(dw.route_of(rec(cid=CID_张), cfg,
                                     [claim(sid=SID_A, cid=CID_李)], NOW),
                         (SID_B, "张三"))

    def test_过期了落回静态表_不是黑洞(self):
        """登记过期后回信必须重新变成「给人看」，人还能处理。
           要是这里返回一个空 sid 又不落回，就是谁都收不到。"""
        old = claim(minutes=-1)
        self.assertEqual(dw.match_awaiting([old], rec(), NOW), ("", ""))
        self.assertEqual(dw.route_of(rec(), {"route": {"张三": SID_B}}, [old], NOW),
                         (SID_B, "张三"))
        self.assertEqual(dw.route_of(rec(), {}, [old], NOW), ("", ""))   # 没静态表 → 给人

    def test_正好到点算过期(self):
        """边界。跟 select_for_session 的 snooze 同一套（`until > 现在` 才有效）。
           今天上午在「满 15 分钟重投」上栽过同一个边界 —— 我先按直觉写了
           「正好 15 分钟不重投」，实际代码是 `waited < 15*60` 所以会重投。
           这里直接钉住，不靠直觉。"""
        正好 = claim(minutes=0)
        self.assertEqual(正好["until"], dw.ts(NOW))
        self.assertEqual(dw.match_awaiting([正好], rec(), NOW), ("", ""))
        差一秒 = {**正好, "until": dw.ts(NOW + dt.timedelta(seconds=1))}
        self.assertEqual(dw.match_awaiting([差一秒], rec(), NOW), (SID_A, ""))

    def test_until_缺了算不命中_不算永不过期(self):
        """**方向是故意选的。** 一条写坏的登记若被当成永不过期，就是
           永久劫持这个通道：那个人以后所有私聊都投给一个可能早没了的会话，
           且不报错。宁可漏路由（落回给人，看得见），不可劫持（看不见）。"""
        坏 = {"cid": CID_张, "sid": SID_A, "at": dw.ts(NOW)}
        self.assertEqual(dw.match_awaiting([坏], rec(), NOW), ("", ""))
        self.assertEqual(dw.match_awaiting([{**坏, "until": ""}], rec(), NOW), ("", ""))
        self.assertEqual(dw.match_awaiting([{**坏, "until": None}], rec(), NOW), ("", ""))

    def test_sid_为空的登记跳过_不把通道弄死(self):
        """一条 sid 写空的登记不能吃掉这个通道 —— 那会让后面那条好的登记
           永远轮不到（认回来一个空 sid，route_of 又不落回）。"""
        claims = [claim(sid=SID_A), {**claim(), "sid": ""}]
        self.assertEqual(dw.match_awaiting(claims, rec(), NOW), (SID_A, ""))

    def test_记录没有_cid_就不命中_哪怕名字对得上(self):
        """实测 14198 条记录每条都有 cid，但采集口径变过一次。缺了就落回，
           **绝不拿名字硬猜** —— 名字重名/改名都会把回信投给错的会话。

           ⚠️ 这个用例最早只喂 `{}` / `{"cid": ""}`，都不带 `sender`；
           而真实记录**永远有 sender**，于是「cid 缺了就退回按名字认」这条
           错路根本没被走到，变异测试照样绿。现在每条都带上名字。"""
        for bad in ({}, {"cid": ""}, {"cid": None},
                    {"sender": "张三", "conv": "张三"},
                    {"cid": "", "sender": "张三", "conv": "张三"}):
            with self.subTest(记录=bad):
                self.assertEqual(dw.match_awaiting([claim()], bad, NOW), ("", ""))

    def test_两个只差尾巴的_cid_不许串台(self):
        """真实 cid 共享 "cid" 前缀、只在中后段分岔。把精确比较写成前缀或
           子串匹配，这两个就会互相命中 —— 投给错的会话，且不报错。
           `match_broadcast` 就栽在它那个 40 字符前缀上（notes 里记着）。"""
        self.assertEqual(CID_张[:35], CID_李[:35])       # 前 35 个字符一模一样
        claims = [claim(sid=SID_A, cid=CID_张)]
        self.assertEqual(dw.match_awaiting(claims, rec(cid=CID_张), NOW)[0], SID_A)
        self.assertEqual(dw.match_awaiting(claims, rec(cid=CID_李), NOW), ("", ""))

    def test_登记侧要是把人名写进_cid_也不许命中(self):
        """**这条是给还没写的登记侧提前立的约束。**

        登记侧（发私聊时记一笔）现在还不存在。它最可能犯的错是把「张三」
        而不是通道 id 写进 `cid`。真发生的话，判据这边必须仍然不命中 ——
        否则重名/改名就会把回信投给错的会话。

        （变异测试里「cid 缺了退回按名字认」那条改法之所以杀不掉，就是因为
        它要跟这个错误**同时**发生才有害。这里把另一半也钉上。）"""
        按名字登记 = {"cid": "张三", "sid": SID_A,
                      "until": dw.ts(NOW + dt.timedelta(hours=1))}
        self.assertEqual(dw.match_awaiting([按名字登记], rec(), NOW), ("", ""))
        self.assertEqual(
            dw.match_awaiting([按名字登记], {"sender": "张三", "conv": "张三"}, NOW),
            ("", ""))

    def test_空登记和_None_都当没有(self):
        self.assertEqual(dw.match_awaiting([], rec(), NOW), ("", ""))
        self.assertEqual(dw.match_awaiting(None, rec(), NOW), ("", ""))


class 接线约束(unittest.TestCase):

    def test_不给_claims_时行为跟改动前逐字相同(self):
        """这是这一批改动的安全网：现有 14 个 route_of 用例一行都不用改。"""
        cfg = {"route": {"项目群": SID_A, "张三": SID_B}}
        self.assertEqual(dw.route_of(rec(conv="项目群"), cfg), (SID_A, "项目群"))
        self.assertEqual(dw.route_of(rec(conv="", sender="张三"), cfg), (SID_B, "张三"))
        self.assertEqual(dw.route_of(rec(conv="没配的群", sender="没配的人"), cfg),
                         ("", ""))
        self.assertEqual(dw.route_of(rec(), cfg, None), (SID_B, "张三"))
        self.assertEqual(dw.route_of(rec(), cfg, []), (SID_B, "张三"))

    def test_给了_claims_不给_at_当场抛错(self):
        """**故意选的失败方向。** 忘了传时钟时，悄悄按「永不过期」走是
           静默劫持；抛错是吵闹的、当场能看见的。route_of 自己去调 now()
           倒是不会抛，但那会让它不再是纯函数 —— 它能被测就因为不看时钟。"""
        with self.assertRaises(ValueError):
            dw.route_of(rec(), {}, [claim()])
        with self.assertRaises(ValueError):
            dw.route_of(rec(), {}, [claim()], None)

    def test_判死不在这一层(self):
        """`match_awaiting` **不检查会话还活着** —— 判死只有
           `fleet.build_sessions()` 一处（拿 tmux list-panes 算，写在 `known`
           字段）。这里再问一遍就是第二份真相。所以一条指向死会话的登记
           在这一层照样命中，剔掉它是调用方的事。

           这个用例存在的意义是：谁哪天想「顺手在这儿也判一下死」，
           先来看这条注释。"""
        import inspect
        # 只看**函数体**，不看 docstring —— 文档里正说着「判死不在这里做」，
        # 那几个词当然在。今天上午在 entitled 上踩过同一个坑：
        # assertNotIn("speaker", 源码) 撞在了「没有 speaker 规则」这句注释上。
        src = inspect.getsource(dw.match_awaiting)
        body = src.replace(dw.match_awaiting.__doc__ or "", "", 1)
        for 不该出现 in ("tmux", "list-panes", "known", "subprocess", "load_json"):
            self.assertNotIn(不该出现, body, "函数体里出现了 %s" % 不该出现)
        # 指向死会话的登记照样返回它 —— 过滤是上层责任
        self.assertEqual(dw.match_awaiting([claim(sid=SID_A)], rec(), NOW)[0], SID_A)

    def test_纯判据不看时钟(self):
        """同一批输入 + 同一个 at，跑两次结果必须一样；换 at 才该变。"""
        c = [claim(minutes=10)]
        self.assertEqual(dw.match_awaiting(c, rec(), NOW),
                         dw.match_awaiting(c, rec(), NOW))
        晚一点 = NOW + dt.timedelta(minutes=20)
        self.assertEqual(dw.match_awaiting(c, rec(), 晚一点), ("", ""))

    def test_不改动传进来的登记列表(self):
        """route_of 在采集链路里会被同一批记录调很多次，
           就地改列表会让第二次的结果跟第一次不同。"""
        claims = [claim(sid=SID_A), claim(sid=SID_B, cid=CID_李)]
        快照 = [dict(c) for c in claims]
        dw.match_awaiting(claims, rec(), NOW)
        self.assertEqual(claims, 快照)


if __name__ == "__main__":
    unittest.main()
