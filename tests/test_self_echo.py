# -*- coding: utf-8 -*-
"""「别把自己推出去的通知当成他的指令」这条判据。

2026-08-28 坐实的反馈环：系统推到自聊天的每一条提醒，8 秒后被 push-loop
读回来当成他打的指令，派给一个 Claude 会话并叫醒它。实测三条
`[push] target=workos/5380` 全是系统自己的推送（【时效】/【哨兵】/【待你拍板】）。

原来的防线是前缀黑名单 `OUT_MARKERS`，dtwatch 推的前缀一个都不在里面。
现在改成按记账认。这批用例钉的就是"记账认"必须比"名单认"更耐加新通知。

全部用造的数据，不碰 data/。
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dtwatch as dw                                             # noqa: E402

AT = dt.datetime(2026, 8, 28, 11, 20, 0)
哨兵 = "【哨兵】有人点你的名还没处理： · 08-28 10:15 某人（私聊）新闻小程序也是单独的后台配置吗"


def ring(*pairs):
    return [{"head": dw.send_head(t), "at": dw.ts(a)} for t, a in pairs]


class SendHead(unittest.TestCase):
    def test_压平空白(self):
        # 钉钉那边多行消息读回来时空白会变，指纹必须先归一化。
        self.assertEqual(dw.send_head("第一段\n\n  第二段"), "第一段 第二段")

    def test_取前80字(self):
        self.assertEqual(len(dw.send_head("甲" * 500)), 80)

    def test_空值不炸(self):
        self.assertEqual(dw.send_head(None), "")
        self.assertEqual(dw.send_head("   \n  "), "")


class IsSelfEcho(unittest.TestCase):
    def test_认得出自己刚推的(self):
        self.assertTrue(dw.is_self_echo(哨兵, ring((哨兵, AT)), AT))

    def test_他自己打的不算(self):
        # 反方向的漏事：误杀他的话＝他打的指令没人接，比反馈环还糟。
        self.assertFalse(dw.is_self_echo("测试回路 收到请回我一句",
                                         ring((哨兵, AT)), AT))

    def test_长得像但不一字不差的不算(self):
        # 要求指纹**完全相等**。前缀/子串匹配会误杀他引用我们播报时自己写的话。
        改了一字 = 哨兵.replace("新闻小程序", "新闻小程序们")
        self.assertFalse(dw.is_self_echo(改了一字, ring((哨兵, AT)), AT))

    def test_只带同样前缀的不算(self):
        # 这条是「记账认」和「名单认」的分界：他要是自己打了个
        # 「【哨兵】怎么老是推」，名单认会把它当系统消息丢掉，记账认不会。
        self.assertFalse(dw.is_self_echo("【哨兵】怎么老是推这个",
                                         ring((哨兵, AT)), AT))

    def test_他引用了整条推送再加自己的话_必须当指令(self):
        """**这条才是"完全相等"和"子串匹配"的分界线，第一轮我漏了。**

        他在手机上最自然的动作就是长按那条推送→引用→加一句"这条帮我回一下"。
        子串匹配下账本里的指纹是他这句话的子串 → **他的指令被静默丢掉**。
        那比反馈环还糟：反馈环是多干活，这个是他说了话没人接。

        变异测试里"指纹改成子串匹配"活过一轮，就是因为前面那几条用例
        改的都是**中间**的字（原文因此不再是子串），恰好绕开了这个形状。
        """
        他的话 = 哨兵 + " 这条帮我回一下"
        self.assertFalse(dw.is_self_echo(他的话, ring((哨兵, AT)), AT))

    def test_他在推送前面加话也算他的(self):
        self.assertFalse(dw.is_self_echo("先别管这个 " + 哨兵,
                                         ring((哨兵, AT)), AT))

    def test_账本里的空指纹不许撞上任何东西(self):
        # 写坏一行（head 为空）不该变成"什么都算回声"的万能钥匙。
        坏账本 = [{"head": "", "at": dw.ts(AT)}]
        self.assertFalse(dw.is_self_echo("", 坏账本, AT))
        self.assertFalse(dw.is_self_echo("他随便说的一句话", 坏账本, AT))

    def test_超出窗口不再认(self):
        # 老指纹不能永远有效，否则他哪天真打了一模一样的话会被吞。
        久 = AT - dt.timedelta(minutes=dw.SENT_SELF_WINDOW_MINUTES + 1)
        self.assertFalse(dw.is_self_echo(哨兵, ring((哨兵, 久)), AT))

    def test_窗口边界内还算(self):
        刚好 = AT - dt.timedelta(minutes=dw.SENT_SELF_WINDOW_MINUTES)
        self.assertTrue(dw.is_self_echo(哨兵, ring((哨兵, 刚好)), AT))

    def test_坏时间戳跳过不误判(self):
        # 漏事形态：坏时间戳当"刚刚"，于是一条本该派活的消息被吞。
        self.assertFalse(dw.is_self_echo(哨兵, [{"head": dw.send_head(哨兵),
                                                 "at": "上午"}], AT))

    def test_空账本一律不算回声(self):
        # 失败方向：认不出就当指令（今天的行为），不退步。
        self.assertFalse(dw.is_self_echo(哨兵, [], AT))
        self.assertFalse(dw.is_self_echo(哨兵, None, AT))

    def test_空正文不算(self):
        self.assertFalse(dw.is_self_echo("", ring((哨兵, AT)), AT))

    def test_多行推送读回来空白变了也认得出(self):
        推 = "【待你拍板】新增 1 条\n\n[ob-260828-1] 回 某人（私聊）\n  拟回：好的"
        读回来 = "【待你拍板】新增 1 条  [ob-260828-1] 回 某人（私聊）   拟回：好的"
        self.assertTrue(dw.is_self_echo(读回来, ring((推, AT)), AT))


class NoBlacklist(unittest.TestCase):
    """这批钉的是「不许退回名单认」。"""

    def test_dtwatch的通知前缀没有一个在dtcc的名单里(self):
        """**这就是当初漏掉的全部原因。** 保留这条断言不是为了让它一直红——
        它现在就是红的事实（名单确实不含这些），而是为了说明：靠名单去追
        新增的通知种类，追不上。真正的防线是记账。"""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import dtcc
        for pre in ("【哨兵】", "【时效·约会】", "【待你拍板】", "【台账任务 t1】", "【picker】"):
            with self.subTest(前缀=pre):
                self.assertFalse(dtcc.is_outbound(pre),
                                 "%s 居然进名单了——那说明有人又去补名单了，"
                                 "请确认记账那条路还在" % pre)
                # 而记账认得出来
                self.assertTrue(dw.is_self_echo(pre + "正文", ring((pre + "正文", AT)), AT))

    def test_记账在发送出口而不是调用方(self):
        # 漏事形态：把记账写在每个调用 send_reminder 的地方，
        # 下一个调用方忘了写，那条通知就又变成指令了。
        import inspect
        src = inspect.getsource(dw.send_reminder)
        self.assertIn("note_self_send", src)


if __name__ == "__main__":
    unittest.main()
