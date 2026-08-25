"""带审计钩子跑一遍测试：任何一次打开 data/ 下的文件都会被记下来。

比「跑前跑后对比 mtime」硬 —— 那个会被后台常驻的采集进程（push-loop 每 8 秒、
console --loop 每 10 秒）污染，分不清是测试写的还是它们写的。审计钩子只看
本进程，别人写多少次都跟它无关。

跑：python3 tests/_audit_run.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data") + os.sep

hits = []


def _hook(event, args):
    if event in ("open", "os.remove", "os.rename", "os.mkdir", "shutil.copyfile"):
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)):
                p = os.fspath(a)
                if isinstance(p, bytes):
                    p = p.decode("utf-8", "replace")
                if p.startswith(DATA):
                    hits.append((event, p))


sys.addaudithook(_hook)
sys.path.insert(0, ROOT)

loader = unittest.TestLoader()
suite = loader.discover(HERE)
res = unittest.TextTestRunner(verbosity=1).run(suite)

print("\n审计：测试进程碰过 data/ 的次数 = %d" % len(hits))
for h in hits[:20]:
    print("  ", h)
sys.exit(0 if (res.wasSuccessful() and not hits) else 1)
