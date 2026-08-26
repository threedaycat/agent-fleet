"""entitled() / owner_of_quote() 的测试 —— 「这条手机指令该不该由我执行」。

错了同样**不报错**：指令被别的会话吃掉，他在手机上看到贴了 OK，
以为有人在干，实际上干的是不相干的那个会话，或者压根没人干。

铁律（2026-07-30 定，写在 target_of 的文档里）：收件人**永远是主会话**，
只有「引用了某条播报」和「文字里点名了会话标签」两个例外。
「按上一个说话的会话」这条规则已经整个删掉 —— 它跟消息内容毫无关系、纯靠运气，
实测把两条自检消息派给了不相干的项目会话。下面有一个用例专门钉这件事。

全部用 `dtcc.FakeRoutes` 跑，不碰 `data/cc/state.json`，也不碰 fleet / tmux。

跑：python3 -m unittest discover -s tests -v
"""

import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dtcc


MAIN = "7ac5aaaa-1111-2222-3333-444455556666"
PROJ = "3f21bbbb-1111-2222-3333-444455556666"
OTHER = "9d0ccccc-1111-2222-3333-444455556666"

CFG = {"cc": {"main_session": MAIN}}

LONG = "这是一条很长的播报开头用来撑过四十个字的边界" * 3   # 63 字


def bc(sid, text):
    """造一条播报环记录，head 的算法跟 note_broadcast 一致。"""
    return {"sid": sid, "head": dtcc.head_of(text), "at": "2026-08-25 10:00:00"}


def routes(broadcasts=None, panes=None, dead=()):
    """造一份假会话表。`dead` 里的 sid 是 pane 已经不在 tmux 里的那种 ——
    记录还带着 pane 值（那是「上次见到它在哪」，第 3 层的记忆），
    但 `known` 标着 gone。判死本身在 fleet.build_sessions 里做，这里只造结论。"""
    sessions = {}
    for sid, pane in (panes or {}).items():
        r = {"pane": pane, "known": "live"}
        if sid in dead:
            r["known"] = "gone"
        sessions[sid] = r
    return dtcc.FakeRoutes(broadcasts=broadcasts, sessions=sessions)


class OwnerOfQuote(unittest.TestCase):
    def test_原文一致就归发它的会话(self):
        src = routes([bc(PROJ, "【CC 3f21】接口联调完了，等你确认")])
        self.assertEqual(
            dtcc.owner_of_quote("【CC 3f21】接口联调完了，等你确认", src), PROJ)

    def test_钉钉把引用截断了也要认出来(self):
        """钉钉引用长消息时只带前面一截。认不出来 = 这条回复被当成新指令，
        默认派回主会话，而他明明是在回项目会话那条播报。"""
        src = routes([bc(PROJ, "【CC 3f21】" + LONG)])
        self.assertEqual(dtcc.owner_of_quote("【CC 3f21】" + LONG[:18], src), PROJ)

    def test_引用里换行被压成空格也认得出(self):
        """head_of 会把连续空白归一成一个空格；引用带回来的换行不能算不一致。"""
        src = routes([bc(PROJ, "【CC 3f21】第一行 第二行")])
        self.assertEqual(dtcc.owner_of_quote("【CC 3f21】第一行\n\n  第二行", src), PROJ)

    def test_同一段话播报过两次归最近那条(self):
        """从后往前找。归错了就是指令进了一个可能已经关掉的老会话。"""
        src = routes([bc(PROJ, "【CC】日报草稿好了"), bc(OTHER, "【CC】日报草稿好了")])
        self.assertEqual(dtcc.owner_of_quote("【CC】日报草稿好了", src), OTHER)

    def test_认不出来返回空(self):
        """认不出就该退回默认收件人，绝不能瞎猜一个 —— 猜错就是派错人。"""
        src = routes([bc(PROJ, "【CC 3f21】接口联调完了")])
        self.assertEqual(dtcc.owner_of_quote("完全不相干的一句话", src), "")

    def test_空引用返回空(self):
        self.assertEqual(dtcc.owner_of_quote("", routes([bc(PROJ, "【CC】x")])), "")
        self.assertEqual(dtcc.owner_of_quote("   \n ", routes([bc(PROJ, "【CC】x")])), "")

    def test_播报环是空的返回空(self):
        self.assertEqual(dtcc.owner_of_quote("【CC】随便什么", routes([])), "")

    def test_跳过head为空的脏记录(self):
        """环里混进一条 head 空的记录，不能让它匹配上所有引用。"""
        src = routes([{"sid": OTHER, "head": ""}, bc(PROJ, "【CC】正经播报")])
        self.assertEqual(dtcc.owner_of_quote("【CC】正经播报", src), PROJ)

    def test_前四十字相同就算命中_已知的宽边界(self):
        """现有行为：只比前 40 字。两条播报开头一样、结尾不同会被认成同一条。

        这不是新加的判据，是把现状钉下来 —— 真要收紧得先有人拍板，
        改了会影响所有「引用截断」的正常路径。
        """
        src = routes([bc(PROJ, LONG + "甲"), bc(OTHER, LONG + "乙")])
        self.assertEqual(dtcc.owner_of_quote(LONG + "甲", src), OTHER)


class NamedSession(unittest.TestCase):
    def test_点名四位会话标签(self):
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        self.assertEqual(dtcc.named_session(CFG, "3f21 那个接口先别动", src), PROJ)

    def test_大小写不敏感(self):
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        self.assertEqual(dtcc.named_session(CFG, "3F21 那个接口先别动", src), PROJ)

    def test_标签嵌在长串里不算点名(self):
        """要独立成词。否则一段 uuid 或哈希里凑巧含这四个字符，
        整条指令就被劫给一个他压根没提的会话。"""
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        self.assertEqual(dtcc.named_session(CFG, "日志里有个 ab3f21cd 的串", src), "")

    def test_项目名不算点名(self):
        """收紧过一次：原来也认项目短名，结果他天天提项目名 ——
        「项目A 那个先别推」是在跟主会话聊天，却被劫给了那个项目会话。"""
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        self.assertEqual(dtcc.named_session(CFG, "项目A 那个先别推", src), "")

    def test_文字为空返回空(self):
        self.assertEqual(dtcc.named_session(CFG, "", routes(panes={PROJ: "%2"})), "")


class Entitled(unittest.TestCase):
    def test_认不出自己就放行(self):
        """拿不到 sid 时退回旧行为，绝不能把整条遥控通道锁死。"""
        self.assertEqual(dtcc.entitled(CFG, {"text": "看一下"}, "", routes()),
                         (True, "no-sid"))

    def test_默认收件人是主会话(self):
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        ok, why = dtcc.entitled(CFG, {"text": "看一下进度"}, MAIN, src)
        self.assertTrue(ok)
        self.assertEqual(why, "mine(default→main)")

    def test_不是给我的就不许拿(self):
        """项目会话不能吃掉本该主会话处理的指令 —— 这是「指令被别的会话吃掉」的主线。"""
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        ok, why = dtcc.entitled(CFG, {"text": "看一下进度"}, PROJ, src)
        self.assertFalse(ok)
        self.assertEqual(why, "belongs-to-7ac5(default→main)")

    def test_主人的pane没了才放开(self):
        """主人的 pane 真的不在了（会话表里查不到）才让别人捡，
        否则这条指令会永远卡在一个不存在的会话名下。"""
        src = routes(panes={PROJ: "%2"})          # MAIN 不在会话表里
        ok, why = dtcc.entitled(CFG, {"text": "看一下进度"}, PROJ, src)
        self.assertTrue(ok)
        self.assertEqual(why, "owner-pane-gone(7ac5)")

    def test_pane字段是空串也算没了(self):
        src = routes(panes={MAIN: "", PROJ: "%2"})
        ok, why = dtcc.entitled(CFG, {"text": "看一下进度"}, PROJ, src)
        self.assertTrue(ok)
        self.assertEqual(why, "owner-pane-gone(7ac5)")

    def test_没配主会话就谁来谁接(self):
        """没人可派的时候放行，别把通道弄死。

        这条路径上 `target_of` 会写一行日志到 `data/cc/cc.log` —— 判据函数里
        还留着一处 IO，这次按规矩没改它（改了就是动现有行为），改用 mock 挡住。
        **不挡的话这个测试会往生产日志里写字**，是审计钩子（`tests/_audit_run.py`）
        抓出来的，肉眼和 mtime 对比都看不出来（后台 push-loop 每 8 秒写同一行）。
        """
        src = routes(panes={PROJ: "%2"})
        with mock.patch.object(dtcc, "logline") as logged:
            ok, why = dtcc.entitled({"cc": {}}, {"text": "看一下进度"}, PROJ, src)
        self.assertTrue(ok)
        self.assertEqual(why, "unrouted")
        self.assertEqual(logged.call_count, 1)      # 没人可派这件事必须留痕

    def test_引用了播报就归播报的主人(self):
        src = routes([bc(PROJ, "【CC 3f21】接口联调完了")], panes={MAIN: "%1", PROJ: "%2"})
        cmd = {"text": "那就发吧", "quoted_text": "【CC 3f21】接口联调完了"}
        self.assertEqual(dtcc.entitled(CFG, cmd, PROJ, src), (True, "mine(quote)"))
        ok, why = dtcc.entitled(CFG, cmd, MAIN, src)
        self.assertFalse(ok)
        self.assertEqual(why, "belongs-to-3f21(quote)")

    def test_引用认不出就退回主会话(self):
        """认不出的引用不能顺势派给「最近说话的那个」—— 那正是错派的病根。"""
        src = routes([bc(PROJ, "【CC 3f21】接口联调完了")], panes={MAIN: "%1", PROJ: "%2"})
        cmd = {"text": "那就发吧", "quoted_text": "一条根本没播报过的话"}
        self.assertEqual(dtcc.entitled(CFG, cmd, MAIN, src), (True, "mine(default→main)"))

    def test_点名优先于默认但引用优先于点名(self):
        """他既引用了 A 的播报又在文字里打了 B 的标签时，以引用为准 ——
        引用是明确在回那条消息，标签可能只是顺口提到。"""
        src = routes([bc(PROJ, "【CC 3f21】接口联调完了")],
                     panes={MAIN: "%1", PROJ: "%2", OTHER: "%3"})
        cmd = {"text": "9d0c 也一起看看", "quoted_text": "【CC 3f21】接口联调完了"}
        self.assertEqual(dtcc.entitled(CFG, cmd, PROJ, src), (True, "mine(quote)"))
        cmd2 = {"text": "9d0c 也一起看看"}
        self.assertEqual(dtcc.entitled(CFG, cmd2, OTHER, src), (True, "mine(named)"))

    def test_别人刚播报过也不改变收件人(self):
        """钉死「按上一个说话的会话」这条规则已经删掉：
        环里最后一条是 OTHER 发的，但他没引用，指令照样归主会话。"""
        src = routes([bc(OTHER, "【CC 9d0c】我这边跑完了")],
                     panes={MAIN: "%1", OTHER: "%3"})
        ok, why = dtcc.entitled(CFG, {"text": "那先这样"}, OTHER, src)
        self.assertFalse(ok)
        self.assertEqual(why, "belongs-to-7ac5(default→main)")


class 死会话不许当收件人(unittest.TestCase):
    """2026-08-25 修掉的缺口。修之前 `pane_of()` 只问「会话表里有没有 pane 值」，
    不问「这个 pane 还在不在 tmux 里」——实测 107 个会话里 **45 个** `known=gone`
    却仍带着 pane 值（`464f4f8d → %134`，`%134` 早不在 `tmux list-panes` 里；
    这 45 个里还有 2026-08-01 事故的 `%44` 和购票会话 `7ac5`，都是真用过的）。

    后果是**指令静默消失**：活会话收到指令回一句「不是我的，是 464f 的」，
    而 464f 早关了，`entitled()` 里「主人的 pane 真没了才放开」那条后路
    永远不触发。三个用例分别钉住修好之后的三处表现。
    """

    def test_死会话查不到pane(self):
        src = routes(panes={MAIN: "%134", PROJ: "%2"}, dead={MAIN})
        self.assertEqual(dtcc.pane_of(MAIN, src), "")
        self.assertEqual(dtcc.pane_of(PROJ, src), "%2")

    def test_主人死了那条后路才真的能走(self):
        src = routes(panes={MAIN: "%134", PROJ: "%2"}, dead={MAIN})
        ok, why = dtcc.entitled(CFG, {"text": "看一下进度"}, PROJ, src)
        self.assertTrue(ok)
        self.assertEqual(why, "owner-pane-gone(7ac5)")

    def test_死会话不进点名的候选池(self):
        src = routes(panes={MAIN: "%1", OTHER: "%134"}, dead={OTHER})
        self.assertEqual(dtcc.named_session(CFG, "9d0c 处理一下", src), "")

    def test_活着的照样正常归属(self):
        """修完不能矫枉过正 —— 12:01 那个方向的错误在这条链上的表现就是
        「所有收件人一次性消失」，比原来的缺口更糟。"""
        src = routes(panes={MAIN: "%1", PROJ: "%2"})
        ok, why = dtcc.entitled(CFG, {"text": "看一下进度"}, PROJ, src)
        self.assertFalse(ok)
        self.assertEqual(why, "belongs-to-7ac5(default→main)")

    def test_判据跟fleet那边是同一条(self):
        """`dtcc.routable_sessions` 和 `fleet.routable` 必须永远同意。
        两份判据各自演化就是两份真相 —— 这个仓库栽过的跟头正是这个。"""
        import fleet
        for rec in ({"pane": "%7", "known": "live"},
                    {"pane": "%7", "known": "remembered"},
                    {"pane": "%7", "known": "gone"},
                    {"pane": "%7"},
                    {"pane": "", "known": "transcript"},
                    {}):
            self.assertEqual(bool(dtcc.routable_sessions({"s": rec})),
                             fleet.routable(rec), rec)


class NoSpeakerRule(unittest.TestCase):
    """结构性约束：路由判据不许去问「最后说话的是谁」。

    两个函数的文档里都写着「没有 speaker 规则」，但文档拦不住下一次手滑 ——
    这两个用例拦得住：只要有人在判据里调回 speaker_sid()，测试就红。
    """

    def test_target_of不问最后说话的是谁(self):
        self.assertNotIn("speaker_sid(", inspect.getsource(dtcc.target_of))

    def test_entitled不问最后说话的是谁(self):
        self.assertNotIn("speaker_sid(", inspect.getsource(dtcc.entitled))

    def test_假路由源不提供speaker(self):
        """FakeRoutes 只给播报环和会话表两样东西。多给一样，
        判据就有机会偷偷用上它。"""
        self.assertFalse(hasattr(dtcc.FakeRoutes(), "speaker"))


if __name__ == "__main__":
    unittest.main()
