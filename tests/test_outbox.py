# -*- coding: utf-8 -*-
"""待拍板草稿的判据。每个用例注释里写清它钉住的是**哪一种会静默漏事的情况**。

这一批的病根只有一个：**拟好的回复只活在会话上下文里**，所以推不出去、
数不出来、会话一没就没了。2026-08-28 用户原话：
「这个不太行，因为没有跟我说要回复的消息是啥」。

全部用造的数据，不碰 data/。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dtwatch as dw                                             # noqa: E402

CID_甲 = "cidZZfake0000000AAAAAAAAA=="
CID_乙 = "cidZZfake1111111BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
AT = dt.datetime(2026, 8, 28, 10, 15, 0)


def draft(id="ob-260828-1", at="2026-08-28 10:15:00", to="小甲",
          status="pending", notified="", **kw):
    d = {"id": id, "at": at, "to_label": to, "cid": CID_甲, "single": True,
         "about_id": "", "about_text": "", "draft": "拟好的回复",
         "by": "", "status": status, "notified_at": notified}
    d.update(kw)
    return d


class Fold(unittest.TestCase):
    def test_后写的覆盖先写的(self):
        # 存的是只追加的 ndjson：drop / 标记已推送都是追加一条补丁行。
        # 折叠错了就是「丢过的草稿又冒出来」或「推过的又推一遍」。
        rows = [draft(), {"id": "ob-260828-1", "status": "dropped"}]
        got = dw.fold_outbox(rows)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["status"], "dropped")
        self.assertEqual(got[0]["to_label"], "小甲", "补丁行不该把别的字段抹掉")

    def test_按创建时间升序(self):
        rows = [draft(id="b", at="2026-08-28 11:00:00"),
                draft(id="a", at="2026-08-28 10:00:00")]
        self.assertEqual([e["id"] for e in dw.fold_outbox(rows)], ["a", "b"])

    def test_没有id的行直接丢(self):
        # 写坏一行不该把整个 outbox 弄死。
        self.assertEqual(dw.fold_outbox([{"draft": "没有 id"}, None, 42]), [])

    def test_空输入不炸(self):
        self.assertEqual(dw.fold_outbox(None), [])
        self.assertEqual(dw.fold_outbox([]), [])


class NextId(unittest.TestCase):
    def test_当天递增(self):
        # 编号要短到他能在手机上一眼看清、手打出来。
        self.assertEqual(dw.next_draft_id([], AT), "ob-260828-1")
        self.assertEqual(dw.next_draft_id([draft(id="ob-260828-1")], AT),
                         "ob-260828-2")

    def test_跨天从1重开(self):
        old = [draft(id="ob-260827-9", at="2026-08-27 10:00:00")]
        self.assertEqual(dw.next_draft_id(old, AT), "ob-260828-1")

    def test_不按条数按最大序号(self):
        # 漏事形态：按 len(entries)+1 编号，中间丢过一条就会撞号 ——
        # 撞号之后 fold 会把两条不同的草稿合成一条，其中一条**静默消失**。
        got = dw.next_draft_id([draft(id="ob-260828-1"), draft(id="ob-260828-5")], AT)
        self.assertEqual(got, "ob-260828-6")

    def test_坏id不影响编号(self):
        self.assertEqual(dw.next_draft_id([draft(id="ob-260828-乱写")], AT),
                         "ob-260828-1")

    def test_不用随机数(self):
        # 同样输入要算出同样结果，否则测试得打桩、重放也对不上。
        self.assertEqual(dw.next_draft_id([], AT), dw.next_draft_id([], AT))


class MakeDraft(unittest.TestCase):
    def test_id不重复(self):
        a = dw.make_draft([], AT, "小甲", CID_甲, "稿一")
        b = dw.make_draft([a], AT, "小乙", CID_乙, "稿二")
        self.assertNotEqual(a["id"], b["id"])

    def test_落盘要带cid但推送不带(self):
        # cid 是发送句柄：二期的守护进程要靠它发，所以必须落盘；
        # 但人认的是姓名，推送里给 cid 没有意义，还占手机屏幕。
        d = dw.make_draft([], AT, "小甲", CID_甲, "稿")
        self.assertEqual(d["cid"], CID_甲)
        self.assertNotIn(CID_甲, dw.format_push([d], 1))

    def test_新草稿一定是pending且未通知(self):
        d = dw.make_draft([], AT, "小甲", CID_甲, "稿")
        self.assertEqual(d["status"], "pending")
        self.assertEqual(d["notified_at"], "")


class Selection(unittest.TestCase):
    def test_只算pending(self):
        rows = [draft(id="a"), draft(id="b", status="dropped")]
        self.assertEqual([e["id"] for e in dw.pending_drafts(rows)], ["a"])

    def test_未通知和已通知要分得开(self):
        # 漏事形态：把「推送被免打扰挡掉」当成「已经给他看过了」。
        # 挡掉了就得留着下次补推，否则草稿静默躺着，跟没拟一样。
        rows = [draft(id="a", notified=""),
                draft(id="b", notified="2026-08-28 10:16:00")]
        self.assertEqual([e["id"] for e in dw.unnotified(rows)], ["a"])

    def test_丢掉的就算没通知过也不再推(self):
        rows = [draft(id="a", status="dropped", notified="")]
        self.assertEqual(dw.unnotified(rows), [])


class FormatPush(unittest.TestCase):
    def test_必须带上拟的回复(self):
        """**这条是这次的直接教训。** 2026-08-28 哨兵推送长这样：
           「有人点你的名还没处理：黄居乐（私聊）思远，新闻小程序也是单独的
           后台配置吗」—— 只有别人问了什么，没有我们拟的回复。
           用户原话：「这个不太行，因为没有跟我说要回复的消息是啥」。"""
        d = draft(draft="是的，新闻是单独一套，后台前端直接编进后端二进制了")
        body = dw.format_push([d], 1)
        self.assertIn("是的，新闻是单独一套", body)

    def test_必须带上一共几条(self):
        # 「我也不知道他到底有几条消息需要发送」——数目本身就是他要的信息。
        body = dw.format_push([draft(id="a")], 5)
        self.assertIn("5", body)

    def test_只有一条时不要说废话(self):
        body = dw.format_push([draft(id="a")], 1)
        self.assertNotIn("共 1 条", body)

    def test_必须带上编号和对方(self):
        body = dw.format_push([draft(id="ob-260828-7", to="小甲")], 1)
        self.assertIn("ob-260828-7", body)
        self.assertIn("小甲", body)

    def test_条数多了不刷屏但要报数(self):
        # 漏事形态：一次推 10 条几百字，他一条都不会读；
        # 或者只推前 3 条**不说还有 7 条**，他以为就这 3 条。
        many = [draft(id="ob-260828-%d" % i) for i in range(1, 11)]
        body = dw.format_push(many, 10)
        self.assertIn("ob-260828-1", body)
        self.assertNotIn("ob-260828-9", body)
        self.assertIn("还有 7 条", body)

    def test_长草稿截断但不吞掉(self):
        body = dw.format_push([draft(draft="甲" * 900)], 1)
        self.assertIn("…", body)
        self.assertLess(len(body), 600)

    def test_换行压成一行(self):
        # 钉钉推送里多段正文会把卡片撑得没法读。
        body = dw.format_push([draft(draft="第一段\n\n第二段")], 1)
        self.assertIn("第一段 第二段", body)

    def test_要说清一期还不能从手机发(self):
        # 漏事形态：推送写得像能回「发1」，他在手机上打了字、等回音，
        # 而根本没人在听 —— 那比不推还糟。
        body = dw.format_push([draft()], 1)
        self.assertIn("还不能从手机发", body)

    def test_没有新的就不推空消息(self):
        self.assertEqual(dw.format_push([], 3), "")

    def test_群和私聊要分得开(self):
        # 发错场合的代价不对称：群消息会被引用、被截图。
        self.assertIn("私聊", dw.format_push([draft(single=True)], 1))
        self.assertIn("群", dw.format_push([draft(single=False)], 1))


class Patches(unittest.TestCase):
    def test_标记已通知只动notified_at(self):
        rows = [draft(id="a")] + dw.mark_notified(["a"], AT)
        got = dw.fold_outbox(rows)[0]
        self.assertEqual(got["notified_at"], "2026-08-28 10:15:00")
        self.assertEqual(got["status"], "pending", "标记通知不该改状态")
        self.assertEqual(got["draft"], "拟好的回复")

    def test_丢弃保留正文(self):
        # 丢掉的草稿正文要留着 —— 「我当时到底拟了什么」是可查的，
        # 否则丢错了就无从追。
        rows = [draft(id="a"), dw.drop_patch("a", AT, "他自己回了")]
        got = dw.fold_outbox(rows)[0]
        self.assertEqual(got["status"], "dropped")
        self.assertEqual(got["draft"], "拟好的回复")
        self.assertEqual(got["drop_reason"], "他自己回了")

    def test_丢弃之后不再算待拍板(self):
        rows = [draft(id="a"), dw.drop_patch("a", AT)]
        self.assertEqual(dw.pending_drafts(rows), [])


class Collapse(unittest.TestCase):
    def test_短的不动(self):
        self.assertEqual(dw.collapse("短", 10), "短")

    def test_正好等于上限不加省略号(self):
        self.assertEqual(dw.collapse("甲乙丙", 3), "甲乙丙")

    def test_空值不炸(self):
        self.assertEqual(dw.collapse(None, 5), "")
        self.assertEqual(dw.collapse("", 5), "")


if __name__ == "__main__":
    unittest.main()
