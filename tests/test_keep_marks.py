# -*- coding: utf-8 -*-
"""`keep_marks` —— 收尾话截断时必须保住「中断线」标记。

这批用例是补票：2026-08-25 的抽层删掉了 `MARK_RE` 的定义、留下了引用，
`keep_marks` 从此在**正文超过 limit 时**抛 NameError，被 `beat_fleet` 的
`except Exception` 接住记成「beat 失败（不影响收尾）」—— 3 天 230 次没人看见。
短正文提前 return，所以从来没在测试或手测里炸过。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet                                                     # noqa: E402


class KeepMarks(unittest.TestCase):
    def test_短正文原样返回(self):
        self.assertEqual(fleet.keep_marks("干完了 @7ac5", 400), "干完了 @7ac5")

    def test_超长正文不许抛异常(self):
        """**这条就是那 3 天没人拦住的洞。**

        短正文在 `len(text) <= limit` 那里提前 return，根本走不到用 MARK_RE
        的那一行。所以「测了 keep_marks」不等于「测了 keep_marks」——
        必须专门喂一条比 limit 长的。
        """
        long = "改完了。" * 200 + " @7ac5 你接一下"
        self.assertGreater(len(long), 400)
        out = fleet.keep_marks(long, 400)          # 会 NameError 的就是这一步
        self.assertTrue(out)

    def test_标记被前置到开头(self):
        # 漏事形态：标记落在第 300 字，截断把它切掉 —— 中断线静默失灵，
        # 主会话不知道「这句话在叫我」。
        long = "背景很长。" * 120 + " 结论：@7ac5 你接一下"
        out = fleet.keep_marks(long, 200)
        self.assertIn("@7ac5", out)
        self.assertTrue(out.startswith("[标记 "), out[:40])

    def test_会话号和项目短名两种标记都认(self):
        # 公开仓库：fixture 不放真实项目名，`@proj-a` 一样覆盖「带连字符」这个形状
        for mark in ("@7ac5", "@2f9b1c3d", "@main", "@proj-a"):
            with self.subTest(标记=mark):
                long = "很长的正文。" * 120 + " " + mark
                self.assertIn(mark, fleet.keep_marks(long, 200))

    def test_不把邮箱当标记(self):
        # @ 后面跟点号的那种不该被当成会话标记塞进开头。
        long = "很长的正文。" * 120 + " 发给 a@b.com 看看"
        out = fleet.keep_marks(long, 200)
        self.assertNotIn("[标记 @b]", out)

    def test_最多前置4个(self):
        marks = " ".join("@%04x" % i for i in range(9))
        long = "正文。" * 120 + " " + marks
        out = fleet.keep_marks(long, 200)
        self.assertLessEqual(out.split("]")[0].count("@"), 4)

    def test_没有标记时不加空的方括号(self):
        long = "纯粹很长的正文没有任何标记。" * 60
        self.assertFalse(fleet.keep_marks(long, 200).startswith("["))

    def test_截断后长度受控(self):
        long = "正文。" * 400 + " @7ac5"
        out = fleet.keep_marks(long, 200)
        self.assertLess(len(out), 260)
        self.assertTrue(out.endswith("…"))


if __name__ == "__main__":
    unittest.main()
