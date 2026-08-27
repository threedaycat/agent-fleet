"""「在等回信」的登记侧 —— prune / add / open_claims / resolve_cid 和接线。

判据那半在 test_awaiting.py（match_awaiting）。这份管**存写和接线**：
登记写进去长什么样、什么时候该被扔掉、以及投递链路有没有真的读到它。

这一层所有的坏法同样不抛异常：
  - 登记没被读到 → 回信照旧落回哨兵视图，看起来像「他没回」
  - 登记该扔没扔 → 一个早没了的会话永久占着某人的私聊通道
  - 存活过滤查不到就全丢 → 所有登记静默失效，且没人会发现

⚠️ 不碰 data/：全部纯函数 + 内存数据，`python3 tests/_audit_run.py` 盯着。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dtwatch as dw

SID_A = "aaaa1111-0000-0000-0000-000000000000"
SID_B = "bbbb2222-0000-0000-0000-000000000000"
# 通道 id 直接照抄真实形状（2026-08-27 量的 data/ 里 67 个通道）：
#   - **长度有 27 和 47 两种**，不是一种 —— 定长 fixture 会让「按长度截断/校验」
#     这类 bug 活下来；
#   - 任意两个通道的最长公共前缀只有 5 个字符（`cidyI` 这种），
#     且**没有一个通道 id 是另一个的子串**。
# 所以「精确匹配 vs 子串匹配」光靠两个 id 是测不出来的（两边行为一样），
# 真正要钉的是**截断的 id 不许匹配** —— 输出里到处印 `cid[:12]…`，
# 迟早有人把印出来的那截当 --cid 传回来。见 test_截断的cid不许匹配。
CID_张 = "cidZZfake0000000AAAAAAAAA=="
CID_李 = "cidZZfake1111111BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
NOW = dt.datetime(2026, 8, 27, 10, 0, 0)


def claim(sid=SID_A, cid=CID_张, minutes=60, label=""):
    return {"cid": cid, "sid": sid, "label": label, "at": dw.ts(NOW),
            "until": dw.ts(NOW + dt.timedelta(minutes=minutes))}


def dm(cid=CID_张, sender="张三", time="2026-08-27 09:00:00", single=True):
    return {"id": "m" + time[-2:], "cid": cid, "conv": sender, "single": single,
            "sender": sender, "sender_id": "u1", "time": time,
            "text": "在的", "level": "high", "flags": ["dm"]}


class 扔掉不该留的(unittest.TestCase):

    def test_过期的扔掉(self):
        self.assertEqual(dw.prune_claims([claim(minutes=-1)], NOW), [])
        self.assertEqual(len(dw.prune_claims([claim(minutes=1)], NOW)), 1)

    def test_正好到点算过期(self):
        """跟 match_awaiting 必须是同一条边界，否则会出现「判据认它、
           存写扔它」的错位：写进去立刻消失，或者反过来。"""
        c = claim(minutes=0)
        self.assertEqual(dw.prune_claims([c], NOW), [])
        self.assertEqual(dw.match_awaiting([c], dm(), NOW), ("", ""))

    def test_写坏的扔掉_三个字段缺一不可(self):
        """缺 until 的如果被留下，就是 match_awaiting 文档里说的「永久劫持」
           的另一半 —— 那边不认它，这边却让它永远占着 awaiting.json。"""
        for bad in ({"sid": SID_A, "until": dw.ts(NOW + dt.timedelta(hours=1))},
                    {"cid": CID_张, "until": dw.ts(NOW + dt.timedelta(hours=1))},
                    {"cid": CID_张, "sid": SID_A}):
            with self.subTest(登记=sorted(bad)):
                self.assertEqual(dw.prune_claims([bad], NOW), [])

    def test_不改入参(self):
        """登记列表在一次命令里会被读、算、写三次，就地改会让后两次看到
           不一样的东西。"""
        src = [claim(minutes=-1), claim(sid=SID_B)]
        快照 = [dict(c) for c in src]
        dw.prune_claims(src, NOW)
        self.assertEqual(src, 快照)

    def test_空和None都当没有(self):
        self.assertEqual(dw.prune_claims([], NOW), [])
        self.assertEqual(dw.prune_claims(None, NOW), [])


class 登记(unittest.TestCase):

    def test_追加在末尾_后登记的才会赢(self):
        """match_awaiting 是从后往前找的。要是 add_claim 插在前面，
           「后登记的赢」这条规则就反了 —— 而两个会话等同一个人时，
           回信会投给先登记的那个，且不报错。"""
        c = dw.add_claim([claim(sid=SID_A)], CID_张, SID_B, "张三", NOW)
        self.assertEqual(c[-1]["sid"], SID_B)
        self.assertEqual(dw.match_awaiting(c, dm(), NOW)[0], SID_B)
        # ⚠️ 只断言「新的在末尾」不够 —— 新登记若把旧的全冲掉，它也在末尾。
        # 变异测试就是这么漏过去的。别人的登记必须还在：登记等张三，
        # 不该把「等李四」那条抹了，而抹了不报错。
        c2 = dw.add_claim([claim(sid=SID_A, cid=CID_李, label="李四")],
                          CID_张, SID_B, "张三", NOW)
        self.assertEqual(len(c2), 2)
        self.assertEqual(dw.match_awaiting(c2, dm(cid=CID_李), NOW)[0], SID_A)

    def test_写的时候顺手扔掉过期的_文件不会无限长(self):
        old = [claim(minutes=-5), claim(sid=SID_B, cid=CID_李, minutes=-1)]
        c = dw.add_claim(old, CID_张, SID_A, "张三", NOW)
        self.assertEqual(len(c), 1)

    def test_同一个通道重复登记是续期不是错误(self):
        c = dw.add_claim([claim(sid=SID_A, minutes=1)], CID_张, SID_A, "张三", NOW, hours=3)
        self.assertEqual(dw.match_awaiting(c, dm(), NOW)[0], SID_A)
        晚两小时 = NOW + dt.timedelta(hours=2)
        self.assertEqual(dw.match_awaiting(c, dm(), 晚两小时)[0], SID_A)

    def test_until_是_at_加_hours(self):
        c = dw.add_claim([], CID_张, SID_A, "张三", NOW, hours=12)
        self.assertEqual(c[0]["until"], dw.ts(NOW + dt.timedelta(hours=12)))

    def test_不改入参(self):
        src = [claim()]
        快照 = [dict(x) for x in src]
        dw.add_claim(src, CID_李, SID_B, "李三", NOW)
        self.assertEqual(src, 快照)


class 存活过滤(unittest.TestCase):

    def test_死会话的登记不参与路由(self):
        """登记方的 pane 已经没了。留着它就是把回信投进一个不存在的窗口 ——
           写得进去、不报错、没人看见。"""
        cs = [claim(sid=SID_A), claim(sid=SID_B, cid=CID_李)]
        self.assertEqual([c["sid"] for c in dw.open_claims(cs, {SID_B}, NOW)], [SID_B])

    def test_查不到谁活着时_不过滤而不是全丢(self):
        """**失败方向是故意选的。** fleet 问不到（导入失败、状态文件坏了）
           时若返回空，就是所有登记一起静默失效 —— 表现是「这功能好像没生效」，
           查起来没有任何线索。宁可投给一个可能已经没了的会话：那至少
           在 tmux 里看得见。"""
        cs = [claim(sid=SID_A)]
        self.assertEqual(len(dw.open_claims(cs, None, NOW)), 1)
        self.assertEqual(dw.open_claims(cs, set(), NOW), [])   # 明确说「一个都不活」才清空

    def test_过期的也一并扔掉(self):
        cs = [claim(sid=SID_A, minutes=-1)]
        self.assertEqual(dw.open_claims(cs, {SID_A}, NOW), [])


class 从历史里认通道(unittest.TestCase):

    def test_认出最近的那个通道(self):
        recs = [dm(cid=CID_李, time="2026-08-20 09:00:00"),
                dm(cid=CID_张, time="2026-08-27 09:00:00")]
        self.assertEqual(dw.resolve_cid(recs, "张三"), (CID_张, 2))

    def test_候选多于一个要带回数字(self):
        """重名的人各有各的通道。猜错就是把回信投给在等另一个人的会话，
           所以数字要带回去让调用方决定拦不拦。"""
        recs = [dm(cid=CID_张), dm(cid=CID_李, time="2026-08-27 09:30:00")]
        cid, n = dw.resolve_cid(recs, "张三")
        self.assertEqual(n, 2)
        self.assertEqual(cid, CID_李)          # 最近的那个

    def test_只认单聊_群消息不算(self):
        """群里有同名的人说话，不代表你跟他有私聊通道。
           拿群 cid 去登记，等于把整个群的消息都劫持给一个会话。"""
        recs = [dm(cid="cid群==", single=False)]
        self.assertEqual(dw.resolve_cid(recs, "张三"), ("", 0))

    def test_没私聊过的人认不出(self):
        self.assertEqual(dw.resolve_cid([dm()], "从没聊过的人"), ("", 0))
        self.assertEqual(dw.resolve_cid([], "张三"), ("", 0))
        self.assertEqual(dw.resolve_cid(None, "张三"), ("", 0))

    def test_空名字不匹配任何人(self):
        """空 --to 必须什么都认不出。

        ⚠️ 只喂「sender 有值」的记录测不出来 —— 那种记录空名字本来就匹配不上，
        守卫在不在都过。真正会出事的是**sender 和 conv 都为空**的记录
        （采集口径变过一次，机器人/系统消息就可能这样）：那时
        `"" in ("", "")` 命中，空 --to 会把这条的通道登记走。
        变异测试抓到的。"""
        self.assertEqual(dw.resolve_cid([dm()], ""), ("", 0))
        空记录 = dict(dm(), sender="", conv="")
        self.assertEqual(dw.resolve_cid([空记录], ""), ("", 0))
        self.assertEqual(dw.resolve_cid([空记录], None), ("", 0))

    def test_conv_和_sender_两边都认(self):
        recs = [dict(dm(), sender="", conv="张三")]
        self.assertEqual(dw.resolve_cid(recs, "张三")[0], CID_张)


class 接线(unittest.TestCase):
    """判据对了、存写对了，但没接上，等于什么都没做。"""

    def test_原本无人接的回信_有登记就归会话(self):
        recs = [dm()]
        cfg = {"route": {}}
        无 = dw.select_for_session(recs, cfg, SID_A, "high", {}, {}, NOW)
        self.assertEqual(无, [])                       # 没登记：谁都不归
        有 = dw.select_for_session(recs, cfg, SID_A, "high", {}, {}, NOW,
                                   [claim(sid=SID_A, label="张三")])
        self.assertEqual([(r["id"], label) for r, label, _ in 有],
                         [(recs[0]["id"], "张三")])

    def test_不给claims时逐字等于改动前(self):
        """安全网：现有 22 个投递用例一行没改。"""
        recs = [dm()]
        cfg = {"route": {"张三": SID_A}}
        a = dw.select_for_session(recs, cfg, SID_A, "high", {}, {}, NOW)
        b = dw.select_for_session(recs, cfg, SID_A, "high", {}, {}, NOW, None)
        c = dw.select_for_session(recs, cfg, SID_A, "high", {}, {}, NOW, [])
        self.assertEqual(len(a), 1)
        self.assertEqual([x[:2] for x in a], [x[:2] for x in b])
        self.assertEqual([x[:2] for x in a], [x[:2] for x in c])

    def test_登记压过静态表(self):
        recs = [dm()]
        cfg = {"route": {"张三": SID_B}}
        归B = dw.select_for_session(recs, cfg, SID_B, "high", {}, {}, NOW)
        self.assertEqual(len(归B), 1)
        cs = [claim(sid=SID_A, label="张三")]
        self.assertEqual(dw.select_for_session(recs, cfg, SID_B, "high", {}, {}, NOW, cs), [])
        self.assertEqual(len(dw.select_for_session(recs, cfg, SID_A, "high", {}, {}, NOW, cs)), 1)


if __name__ == "__main__":
    unittest.main()
