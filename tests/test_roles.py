# -*- coding: utf-8 -*-
"""角色到底在不在干活。

用户 2026-08-28 原话：「就是现在他们都启动着，可很多都死掉了，或者从来就没有真的
激活过，干过活，总觉得缺少管理和检查」。

当时实测：声明六个角色，`doctor` / `duty` / `watch` 上下文 **0k**（一句话都没说过），
`remote` 的 pane 根本不存在，而心跳给这三个全报 `input`（健康）。原因是旧判据只问
「pane 在不在」——**「起来了」和「干过活」是两件事**。

这批用例守的核心只有一条：**五种状态必须分得开**，特别是
`unknown`（上下文读不出）绝不能报成 `never`（从没干过活）。后者是结论，
前者是没有结论；把没有结论说成结论，就是编造。

全部用造的数据，不碰 data/、不抓屏。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import console                                                   # noqa: E402
import dash                                                      # noqa: E402
import fleet                                                     # noqa: E402

AT = 1_787_900_000.0
HOUR = 3600.0


def pane(coord="OS:workOS.1", pid="%1", ctx="210k (21%)", seen=AT - HOUR):
    return {"coord": coord, "pane": pid, "ctx": ctx, "seen": seen}


class RoleState(unittest.TestCase):
    def test_pane不存在是missing(self):
        r = console.role_state("remote", None, AT)
        self.assertEqual(r["state"], "missing")
        self.assertIsNone(r["pane"])

    def test_上下文0k是never(self):
        r = console.role_state("duty", pane(ctx="0k (0%)"), AT)
        self.assertEqual(r["state"], "never")

    def test_上下文读不出是unknown不是never(self):
        """整个文件最重要的一条。`None` 当 0 处理 → 报出「从来没干过活」，
        那是编造。读不出就说读不出。"""
        for bad in (None, "", "?", "读不出"):
            r = console.role_state("duty", pane(ctx=bad), AT)
            self.assertEqual(r["state"], "unknown", bad)

    def test_never和unknown在屏幕上也不同(self):
        self.assertNotEqual(dash.ROLE_MARK["never"], dash.ROLE_MARK["unknown"])
        self.assertNotEqual(dash.ROLE_TONE["never"], dash.ROLE_TONE["unknown"])

    def test_心跳新且干过活才是live(self):
        r = console.role_state("main", pane(seen=AT - HOUR), AT)
        self.assertEqual(r["state"], "live")

    def test_心跳太旧是stale(self):
        r = console.role_state("watch", pane(seen=AT - 72 * HOUR), AT)
        self.assertEqual(r["state"], "stale")
        self.assertIn("72.0 小时前", r["note"])

    def test_阈值边界(self):
        h = console.ROLE_STALE_HOURS
        self.assertEqual(console.role_state("a", pane(seen=AT - h * HOUR + 60), AT)["state"],
                         "live")
        self.assertEqual(console.role_state("a", pane(seen=AT - h * HOUR - 60), AT)["state"],
                         "stale")

    def test_心跳读不出是unknown不是live(self):
        """`float(None)` 抛异常时如果兜成 0，`at - 0` 是个天文数字 → stale；
        但兜成 `at` 就会报 live（死掉的说成健康）。两个都不许，只能 unknown。"""
        for bad in (None, "", "从没有", {}):
            r = console.role_state("duty", pane(seen=bad), AT)
            self.assertEqual(r["state"], "unknown", bad)

    def test_0k优先于心跳(self):
        """0k 是「从没对话」的铁证，心跳再新也改不了这个结论。"""
        r = console.role_state("duty", pane(ctx="0k (0%)", seen=AT - 60), AT)
        self.assertEqual(r["state"], "never")

    def test_ctx解析跟fleet对账(self):
        for s in ("210k (21%)", "0k (0%)", "300k", None, "", "垃圾", "ctx 300k (30%)"):
            self.assertEqual(console._ctx_kb(s), fleet._ctx_kb(s), repr(s))


class RoleReport(unittest.TestCase):
    def test_声明坐标要能对上运行期坐标(self):
        """声明用 `sess:win#idx`，pane 那边是 `sess:win.idx`。
        这一步换错分隔符，六个角色会全报 missing。"""
        rep = console.role_report([pane("OS:workOS.1", "%1")],
                                  {"OS:workOS#1": "main"}, AT)
        self.assertEqual(rep[0]["state"], "live")
        self.assertEqual(rep[0]["coord"], "OS:workOS.1")

    def test_按角色名排序(self):
        rep = console.role_report([], {"OS:a#1": "watch", "OS:b#1": "desk"}, AT)
        self.assertEqual([r["role"] for r in rep], ["desk", "watch"])

    def test_窗口名带中文也能对上(self):
        rep = console.role_report([pane("OS:值班.1", "%9")], {"OS:值班#1": "duty"}, AT)
        self.assertEqual(rep[0]["state"], "live")

    def test_没有声明就是空(self):
        self.assertEqual(console.role_report([pane()], {}, AT), [])

    def test_pane多余不报错(self):
        rep = console.role_report([pane("OS:x.1", "%1"), pane("OS:y.1", "%2")],
                                  {"OS:x#1": "main"}, AT)
        self.assertEqual(len(rep), 1)


class DashWiring(unittest.TestCase):
    """判据对了不代表接线对了 —— 今天已经栽过两次（force-push、dtwatch_mod）。"""

    def base(self, **over):
        s = {"at": "16:00:00", "checks": [], "services": [], "collector": [],
             "stale": 0, "panes": [pane()], "roles": None}
        s.update(over)
        return s

    def test_角色区块真的画出来了(self):
        rep = console.role_report([pane("OS:workOS.1", "%1")], {"OS:workOS#1": "main"}, AT)
        out = "\n".join(t for t, _ in dash.compose(self.base(roles=rep), 100))
        self.assertIn("角色", out)
        self.assertIn("1/1 在干活", out)

    def test_只列不正常的(self):
        rep = [console.role_state("main", pane(), AT),
               console.role_state("duty", pane(ctx="0k (0%)"), AT)]
        out = "\n".join(t for t, _ in dash.compose(self.base(roles=rep), 100))
        self.assertIn("1/2 在干活", out)
        self.assertIn("duty", out)          # 坏的要点名
        self.assertNotIn("● main", out)     # 好的不占屏幕

    def test_角色读不出说读不出(self):
        out = "\n".join(t for t, _ in dash.compose(self.base(roles=None), 100))
        self.assertIn("读不出", out)
        self.assertNotIn("0/0 在干活", out)

    def test_panes读不出时角色必须也是读不出(self):
        """不能拿空 pane 列表去比对 —— 那会把六个角色全报成 missing，
        一个采集故障看起来就像「全死了」。"""
        saved = {n: getattr(console, n) for n in ("collect_panes", "declared_roles")}
        console.collect_panes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        console.declared_roles = lambda: {"OS:a#1": "main"}
        for n in ("collect_checks", "collect_services", "collect_collector"):
            saved[n] = getattr(console, n)
            setattr(console, n, lambda *a, **k: [])
        saved_dt, dash.dtwatch = dash.dtwatch, None
        try:
            snap = dash.snapshot(with_ctx=False)
        finally:
            for n, fn in saved.items():
                setattr(console, n, fn)
            dash.dtwatch = saved_dt
        self.assertIsNone(snap["panes"])
        self.assertIsNone(snap["roles"])         # ← 不是 [] 也不是全 missing


if __name__ == "__main__":
    unittest.main()
