# -*- coding: utf-8 -*-
"""「点名到他的消息算不算有人管了」这条判据。

2026-08-28 用户原话：「我已经回复了吧，你消息太慢了」。
当天实测：13:14 对方问「仓库有了吗」，13:29:57 他回了（self_last 记着），
13:30:19 哨兵**仍然**推「有人点你的名还没处理：13:14 仓库有了吗」——回完 22 秒。
同时哨兵认为「还没处理」的一共 331 条，其中 224 条他之后在同一通道说过话。

根因：`replied_after` 这个标记存在，但 stale_at_me 不认它。
而它之所以不认，是 2026-07-30 群里那次 25 小时事故的教训——
那条规矩对群是对的，套到私聊上就错了。

全部用造的数据，不碰 data/。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dtwatch as dw                                             # noqa: E402


def rec(single=True, flags=("at_me",), sender="小甲"):
    return {"id": "msgFAKE1", "cid": "cidZZfake0000000AAAAAAAAA==",
            "conv": sender if single else "某个群", "single": single,
            "sender": sender, "time": "2026-08-28 13:14:53",
            "text": "仓库有了吗", "level": "high", "flags": list(flags)}


class AtMeHandled(unittest.TestCase):
    def test_triage有记录就算管了(self):
        self.assertTrue(dw.at_me_handled(rec(), in_triage=True))

    def test_贴过表情算管了(self):
        # sweep_acks 只扫 level>low，而贴过表情的采集时已被压成 low，
        # 所以这类只有 flag、没有 triage 记录，必须在这儿单独认。
        # 不认的话第一条自动提醒就会去催他已经 OK 过的事，通道立刻不可信。
        self.assertTrue(dw.at_me_handled(rec(flags=("at_me", "acked:OK")),
                                         in_triage=False))

    def test_私聊里他回过话就算管了(self):
        """**这条就是那次假警报。** 一对一窗口里，他在对方那条之后说了话，
           基本就是在回他。"""
        self.assertTrue(dw.at_me_handled(
            rec(single=True, flags=("at_me", "replied_after", "at_me_unresolved")),
            in_triage=False))

    def test_群里他回过话不算管了(self):
        """**这条是 2026-07-30 那次 25 小时事故的判据版，不许放开。**

        某同事在项目群 @ 他本人定基线，他之后又在同群说过别的话 →
        级别被降 → `pending --level high` 里看不见 → 挂了 25 小时。
        群里几十条刷过去，「说过话」根本不等于「看见了那个 @」。
        """
        self.assertFalse(dw.at_me_handled(
            rec(single=False, flags=("at_me", "replied_after", "at_me_unresolved")),
            in_triage=False))

    def test_私聊但没回过话不算管了(self):
        self.assertFalse(dw.at_me_handled(rec(single=True, flags=("at_me",)),
                                          in_triage=False))

    def test_私聊和replied_after必须同时成立(self):
        # 漏事形态：只看 replied_after 不看 single → 群里的也放开了，
        # 那就是把 25 小时事故请回来。
        self.assertFalse(dw.at_me_handled(
            rec(single=False, flags=("at_me", "replied_after")), in_triage=False))
        # 反向：只看 single 不看 replied_after → 所有私聊都不提醒了，
        # 那哨兵在私聊上等于关掉。
        self.assertFalse(dw.at_me_handled(
            rec(single=True, flags=("at_me",)), in_triage=False))

    def test_flags缺失不炸(self):
        self.assertFalse(dw.at_me_handled({"single": True}, in_triage=False))
        self.assertFalse(dw.at_me_handled({}, in_triage=False))

    def test_acked前缀要带冒号后的内容(self):
        # `acked` 光秃秃一个词不是采集器打的标记（它打的是 acked:<表情>）。
        # 这里放宽成前缀匹配是有意的 —— 表情内容不该影响"算不算处理过"。
        self.assertTrue(dw.at_me_handled(rec(flags=("at_me", "acked:👌")),
                                         in_triage=False))


class StaleAtMeUsesIt(unittest.TestCase):
    def test_stale_at_me走的是这条判据(self):
        """漏事形态：判据抽出来了但调用方没改，于是改了没用 ——
           今天栽过一次（反馈环那个 commit 测了判据没测接线）。

           ⚠️ **断言前必须剥掉 docstring。** `stale_at_me` 的文档里就写着
           「判据在 `at_me_handled` 里」，不剥掉的话删了调用这条也照样通过 ——
           变异测试里这个变异体因此活过一轮。
           今天已经是第三次在 docstring 上栽同一个跟头了。
        """
        import inspect
        src = inspect.getsource(dw.stale_at_me)
        src = src.replace(dw.stale_at_me.__doc__ or "", "")     # 只留代码
        self.assertIn("at_me_handled", src)
        # 旧的**内联分支**要拿掉，不然是两份真相。
        # 注意别把 `r["id"] in triage` 也断言掉 —— 它现在是**传给**判据的参数，
        # 本该在。第一版断言写成那样，用例因为错误的原因失败了一次。
        self.assertNotIn('f.startswith("acked:")', src)
        self.assertNotIn('"replied_after" in', src)


if __name__ == "__main__":
    unittest.main()
