"""build_sessions() / routable() 的测试 —— 「这个会话现在还能不能收件」。

这一层有**两个方向的静默故障**，两边都致命，所以两边都要钉：

  1. **判死太松**：pane 早没了还当收件人。2026-08-25 实测 107 个会话里 45 个
     `known=gone` 却仍带着 pane 值，指令被判给一个不存在的收件人然后消失。
  2. **判死太狠**：把活会话判死。2026-07-31 12:01 真发生过 —— `status.json`
     被那套工具重建，条目从 23 条掉到 2 条，要是拿「第 1 层查不到」当判死依据，
     **所有路由目标一夜之间全被判成已关闭**。今天这张表 107 个，真那么判就是
     一次性清空 100 个收件人。

所以判死的**唯一依据是第 2 层**（`tmux list-panes -a` 里有没有这个 pane-id），
不是第 1 层的缺失。下面 `十二点零一分那次` 这一组就是专门盯着这条不许被改坏的。

全部喂内存字典 + 一个假的活 pane 集合，不读 status.json、不问 tmux、不碰 data/。

跑：python3 -m unittest discover -s tests -v
"""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet


AT = dt.datetime(2026, 8, 25, 12, 0, 0)


def status(*panes):
    """造 status.json 的内容。参数是 (pane, sid) 或 (pane, sid, 状态)。"""
    out = {}
    for item in panes:
        pane, sid = item[0], item[1]
        st = item[2] if len(item) > 2 else "input"
        out[pane] = {"session_id": sid, "status": st, "session": "OS",
                     "window": "3", "pane_index": "1", "window_name": "w",
                     "cwd": "/tmp/x", "updated_at": AT.timestamp() - 30}
    return out


def sidecar(*seen):
    """造 sidecar。参数是 (sid, pane)。"""
    return {sid: {"last_seen": {"pane": pane, "tmux": "OS:3.1",
                                "window_name": "w", "cwd": "/tmp/x",
                                "at": "2026-08-25 11:00:00"}}
            for sid, pane in seen}


def build(raw=None, side=None, panes=(), **kw):
    return fleet.build_sessions(raw or {}, side or {}, set(panes), AT, **kw)


class 十二点零一分那次(unittest.TestCase):
    """状态文件缩水**不等于**会话关闭。这一组不许被改坏。"""

    def test_状态文件从23条掉到2条但pane都还在_一个都不许判死(self):
        raw = status(("%1", "s1"), ("%2", "s2"))                 # 只剩 2 条
        side = sidecar(*[("s%d" % i, "%%%d" % i) for i in range(1, 24)])
        live = {"%%%d" % i for i in range(1, 24)}                 # tmux 里 23 个都在
        out, _ = build(raw, side, live)
        self.assertEqual(len(out), 23)
        dead = [s for s, r in out.items() if r["known"] == fleet.DEAD]
        self.assertEqual(dead, [], "状态文件缩水被当成了判死依据")
        self.assertEqual(sum(1 for r in out.values() if fleet.routable(r)), 23)

    def test_第一层查不到但pane还在_算remembered不算gone(self):
        out, _ = build(status(), sidecar(("s9", "%9")), {"%9"})
        self.assertEqual(out["s9"]["known"], "remembered")
        self.assertEqual(out["s9"]["state"], "unknown")
        self.assertTrue(fleet.routable(out["s9"]))

    def test_状态文件整个空了也不影响第三层(self):
        out, _ = build({}, sidecar(("s1", "%1"), ("s2", "%2")), {"%1", "%2"})
        self.assertEqual(sorted(out), ["s1", "s2"])
        self.assertTrue(all(fleet.routable(r) for r in out.values()))


class 判死只认tmux(unittest.TestCase):
    def test_第一层有记录但pane没了_判死(self):
        """2026-08-25 新加的秤：状态文件里有记录，不代表那个 pane 还在。
        以前这一层无条件标 live，一条陈旧记录就能让死会话冒充收件人。"""
        out, _ = build(status(("%134", "s1")), {}, {"%7"})
        self.assertEqual(out["s1"]["known"], fleet.DEAD)
        self.assertEqual(out["s1"]["state"], "closed")
        self.assertFalse(fleet.routable(out["s1"]))

    def test_第一层有记录且pane还在_是live(self):
        out, _ = build(status(("%7", "s1", "running")), {}, {"%7"})
        self.assertEqual(out["s1"]["known"], "live")
        self.assertEqual(out["s1"]["state"], "busy")
        self.assertTrue(fleet.routable(out["s1"]))

    def test_第三层记得但pane没了_判死(self):
        out, _ = build({}, sidecar(("s9", "%134")), {"%7"})
        self.assertEqual(out["s9"]["known"], fleet.DEAD)
        self.assertEqual(out["s9"]["state"], "closed")
        self.assertFalse(fleet.routable(out["s9"]))

    def test_判死的记录仍然留着pane值(self):
        """**故意的**：留的是「上次见到它在哪」，那正是第 3 层的记忆。
        抹掉 pane 字段等于把 12:01 的防线一起拆了 —— 下次状态文件缩水，
        这些会话连认都认不回来。所以判死写在 known 上，不是靠清字段。"""
        out, _ = build({}, sidecar(("s9", "%134")), set())
        self.assertEqual(out["s9"]["pane"], "%134")
        self.assertFalse(fleet.routable(out["s9"]))


class Routable(unittest.TestCase):
    """归属判据的唯一入口。真值表钉死，免得下游各自发明一套。"""

    def test_真值表(self):
        self.assertTrue(fleet.routable({"pane": "%7", "known": "live"}))
        self.assertTrue(fleet.routable({"pane": "%7", "known": "remembered"}))
        self.assertFalse(fleet.routable({"pane": "%7", "known": fleet.DEAD}))
        self.assertFalse(fleet.routable({"pane": "", "known": "transcript"}))
        self.assertFalse(fleet.routable({}))

    def test_没有known字段当成能收件(self):
        """老调用方造的记录没有这个字段，不能因此把它们全判死 ——
        又是 12:01 那个方向的错误，只是换个入口。"""
        self.assertTrue(fleet.routable({"pane": "%7"}))


class 分层顺序(unittest.TestCase):
    def test_第一层压过第三层(self):
        raw = status(("%7", "s1", "running"))
        out, _ = build(raw, sidecar(("s1", "%99")), {"%7", "%99"})
        self.assertEqual(out["s1"]["known"], "live")
        self.assertEqual(out["s1"]["pane"], "%7")

    def test_第三层压过第四层(self):
        """顺序反了第 3 层就变成死代码 —— 2026-07-31 真发生过一次，
        受影响的会话在看板上被标成看着能唤醒的 idle，wake 才报「pane 不在了」。"""
        out, _ = build({}, sidecar(("s9", "%9")), {"%9"},
                       transcript_age=lambda sid: 10)
        self.assertEqual(out["s9"]["known"], "remembered")
        self.assertEqual(out["s9"]["pane"], "%9")

    def test_第四层不给pane(self):
        """同一个 cwd 可能开着好几个 pane，猜 pane 会把活派错地方。
        所以第 4 层只回答「活没活」，绝不回答「在哪」。"""
        out, _ = build({}, {"s9": {"project": "p"}}, set(),
                       transcript_age=lambda sid: 10)
        self.assertEqual(out["s9"]["known"], "transcript")
        self.assertEqual(out["s9"]["pane"], "")
        self.assertFalse(fleet.routable(out["s9"]))

    def test_第四层按年龄分闲着和陈旧(self):
        a, _ = build({}, {"s9": {}}, set(), transcript_age=lambda s: 10)
        b, _ = build({}, {"s9": {}}, set(), transcript_age=lambda s: 7200)
        self.assertEqual(a["s9"]["state"], "idle")
        self.assertEqual(b["s9"]["state"], "stale")

    def test_不给transcript取法就整层跳过(self):
        out, _ = build({}, {"s9": {}}, set())
        self.assertEqual(out, {})


class 不碰文件也不改输入(unittest.TestCase):
    def test_只返回该写回的部分自己不写盘(self):
        side = {}
        out, updates = build(status(("%7", "s1")), side, {"%7"})
        self.assertEqual(side, {}, "build_sessions 改了传进来的 sidecar")
        self.assertEqual(list(updates), ["s1"])
        self.assertEqual(updates["s1"]["last_seen"]["pane"], "%7")

    def test_last_seen没变就不用写(self):
        """每 8 秒被 push-loop 调一次，没变化还写盘就是白磨盘。"""
        side = sidecar(("s1", "%7"))
        _, updates = build(status(("%7", "s1")), side, {"%7"})
        self.assertEqual(updates, {})

    def test_只要第一层的时候也照样给出该写回的部分(self):
        _, updates = build(status(("%7", "s1")), {}, {"%7"},
                           include_remembered=False)
        self.assertEqual(list(updates), ["s1"])

    def test_只要第一层就不带出记得的和transcript的(self):
        out, _ = build(status(("%7", "s1")), sidecar(("s9", "%9")), {"%7", "%9"},
                       include_remembered=False)
        self.assertEqual(list(out), ["s1"])


class 坏输入(unittest.TestCase):
    def test_状态文件不是字典也不崩(self):
        """读到半截的 json 会是 list 或 None。整条链不能因此炸掉。"""
        out, _ = build([], sidecar(("s9", "%9")), {"%9"})
        self.assertEqual(list(out), ["s9"])

    def test_没有session_id的条目跳过(self):
        raw = {"%7": {"status": "input"}, "%8": {"session_id": "  ", "status": "input"}}
        out, _ = build(raw, {}, {"%7", "%8"})
        self.assertEqual(out, {})

    def test_年龄从注进来的时刻算(self):
        raw = status(("%7", "s1"))
        raw["%7"]["updated_at"] = AT.timestamp() - 300
        out, _ = build(raw, {}, {"%7"})
        self.assertEqual(out["s1"]["age"], 300)


if __name__ == "__main__":
    unittest.main()
