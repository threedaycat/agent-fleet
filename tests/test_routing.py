"""route_of() 的测试 —— 「这条消息归哪个 Claude 会话」。

这是整条链路上最容易**静默漏事**的一环：路由算错了，消息不会报错、不会丢，
它只是**去了错的会话**。错的那个会话看不懂，对的那个永远等不到。
所以每个用例的注释都写清它钉住的是哪一种「错了也不吭声」的情况。

跑：python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dtwatch as dw


SID_A = "aaaa1111-2222-3333-4444-555566667777"
SID_B = "bbbb1111-2222-3333-4444-555566667777"


def rec(conv="", sender="", **kw):
    r = {"id": "m1", "conv": conv, "sender": sender}
    r.update(kw)
    return r


class RouteOf(unittest.TestCase):
    def test_按会话名路由(self):
        """整个需求群路由给某个项目会话。"""
        cfg = {"route": {"项目A需求群": SID_A}}
        self.assertEqual(dw.route_of(rec(conv="项目A需求群"), cfg), (SID_A, "项目A需求群"))

    def test_按发送人名路由(self):
        """只把某个人的私聊路由过去 —— 私聊没有群名，只能靠发送人。"""
        cfg = {"route": {"张三": SID_A}}
        self.assertEqual(dw.route_of(rec(sender="张三"), cfg), (SID_A, "张三"))

    def test_会话名优先于发送人名(self):
        """两个都在表里时走会话名。

        顺序反了会静默漏事：某人在 A 群说 A 的事，却因为他本人被路由到 B，
        整条消息被投进 B 会话 —— B 看不懂，A 永远等不到。
        （fleet.py 里专门标了这两种「被路由」要分清。）
        """
        cfg = {"route": {"项目A需求群": SID_A, "张三": SID_B}}
        self.assertEqual(
            dw.route_of(rec(conv="项目A需求群", sender="张三"), cfg), (SID_A, "项目A需求群"))

    def test_没配就是空(self):
        """没配路由的消息留给哨兵会话自己处理，不能瞎猜一个目标。"""
        self.assertEqual(dw.route_of(rec(conv="闲聊群", sender="李四"), {"route": {}}), ("", ""))

    def test_config里根本没有route键(self):
        """老配置文件没有这一段，不能因此崩掉整个 pending。"""
        self.assertEqual(dw.route_of(rec(conv="任意群"), {}), ("", ""))

    def test_route为null也当没配(self):
        """`"route": null` 是手改配置时常见的写法。"""
        self.assertEqual(dw.route_of(rec(conv="任意群"), {"route": None}), ("", ""))

    def test_必须整名命中不能是子串(self):
        """「项目A」这条路由不该把「项目A客户群」也劫走。

        改成子串匹配是很自然的「优化」，但那会让一条路由悄悄吃掉一批
        名字沾边的群 —— 而且没有任何提示。
        """
        cfg = {"route": {"项目A": SID_A}}
        self.assertEqual(dw.route_of(rec(conv="项目A客户群"), cfg), ("", ""))

    def test_字典形式带标签(self):
        """路由值可以是 {"session":..., "label":...}，标签是给人看的短名。"""
        cfg = {"route": {"项目A需求群": {"session": SID_A, "label": "项目A"}}}
        self.assertEqual(dw.route_of(rec(conv="项目A需求群"), cfg), (SID_A, "项目A"))

    def test_字典没写标签就回退到键名(self):
        cfg = {"route": {"项目A需求群": {"session": SID_A}}}
        self.assertEqual(dw.route_of(rec(conv="项目A需求群"), cfg), (SID_A, "项目A需求群"))

    def test_字典标签为空串也回退到键名(self):
        cfg = {"route": {"项目A需求群": {"session": SID_A, "label": ""}}}
        self.assertEqual(dw.route_of(rec(conv="项目A需求群"), cfg), (SID_A, "项目A需求群"))

    def test_字典缺session返回空会话(self):
        """配置写漏了 session 字段时必须返回空 sid。

        要是这里回退成键名，投递方会拿「项目A需求群」当 session id 去比对，
        永远不等于任何真会话 —— 消息静默沉底。
        """
        cfg = {"route": {"项目A需求群": {"label": "项目A"}}}
        self.assertEqual(dw.route_of(rec(conv="项目A需求群"), cfg), ("", "项目A"))

    def test_空的会话名不参与匹配(self):
        """私聊记录的 conv 可能是空串；路由表里若有 "" 这个键不能被它命中。"""
        cfg = {"route": {"": SID_A, "张三": SID_B}}
        self.assertEqual(dw.route_of(rec(conv="", sender="张三"), cfg), (SID_B, "张三"))

    def test_记录缺字段不崩(self):
        """采集异常时字段可能整个缺失或是 None，路由不能因此炸掉整轮。"""
        cfg = {"route": {"张三": SID_A}}
        self.assertEqual(dw.route_of({"id": "m1"}, cfg), ("", ""))
        self.assertEqual(dw.route_of({"id": "m1", "conv": None, "sender": None}, cfg), ("", ""))

    def test_不修改传进来的记录(self):
        """route_of 是判据，不该顺手往记录上写东西。"""
        r = rec(conv="项目A需求群")
        before = dict(r)
        dw.route_of(r, {"route": {"项目A需求群": SID_A}})
        self.assertEqual(r, before)


if __name__ == "__main__":
    unittest.main()
