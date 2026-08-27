# agent-fleet

> 本地目录和远端仓库现在都叫 `agent-fleet`（2026-08-27 统一）。
> 早期叫 `dingtalk-watch` —— 那时它只做钉钉哨兵；现在它管的是会话编队，
> 钉钉只是入口之一。**旧路径 `~/Zymix/dingtalk-watch` 已经不存在**，
> 看到任何地方还这么写，那是过期文档。

钉钉消息哨兵 + 手机遥控本机 Claude 会话。零外部依赖，纯 Python 3。

**这个仓库的远端是公开仓库。** 不许把公司名、真实人名、真实群名、账号写进任何被跟踪的文件，
测试 fixture 也算（`56a4e8a` 就是补这个）。本机私有的那份在 `_local-*` 里，已 gitignore。

## 动手前先读 `_local-notes/`

> ⚠️ `notes/` 是**废弃路径**，已被 gitignore（这个仓库公开，而决策记录的价值恰恰在于
> 写「哪个判据我还怀疑」「哪个缺口没修」）。看到旧文档指向 `notes/` 就当它说的是
> `_local-notes/`，**不要新建 `notes/`** —— 那会立刻变成第二份真相。


`_local-notes/` 是这个仓库的**决策记录**：某个函数为什么这么写、某个接口为什么故意**不**提供某个方法、
某个阈值为什么是这个数。它们不是文档，是**已经付过学费的教训** ——
25 个 commit 里 9 个是 fix，其中 8 个落在同一件事上。

改到哪一块，先看那块有没有 note。**别顺手改判据**：阈值、正则、边界条件的现有行为
背后通常有一次事故，`git log` 里查得到。

## 改了行为或接口，同一个改动里留一份 note

只有**纯机械、纯局部**的编辑免除。写法见 [`_local-notes/README.md`](_local-notes/README.md)，三段：
`## 背景` / `## 决定`（现在时）/ `## 打败了什么`。

跨项目、方法论、工具选型的决定**不进这里**，进 `~/workos`。
分界线一句话：**换个人接手这个仓库，他需不需要这条？**

## 硬边界

- **`data/` 是生产状态**（真实消息、游标、投递台账、会话状态）。测试**绝不允许**读写它 ——
  用假实现（`tmux_probe.FakeScreen`、`dtcc.FakeRoutes`）或直接喂内存字典。
  证明方式是审计钩子 `python3 tests/_audit_run.py`，不是跑前跑后比 mtime
  （后台采集进程每 8 秒就在写，比不出来）。
- **不要动 launchd**（`./run.sh install/uninstall`）—— 三个采集进程正在跑，停了就开始漏消息。
- **不碰 `_local-*`**：本机私有，含真实人名和坐标。
- `git commit` / `git push` **一律用户说了才做**。
- **公开仓库。** 任何 tracked 文件（**包括测试 fixture 和 example 配置**）里不许出现
  真通道 id、真 sender_id、真人名、真群名、公司邮箱。2026-08-27 因为「注释里抄一个真
  cid 说明形状」泄漏过一次，只能靠删库重建关掉 —— **force-push 关不掉**，
  GitHub 不 GC，实测 36/40 个旧 commit 仍可按全 40 位 SHA fetch。
  造 fixture 前先量真数据的形状，别抄真值。

## MCP 面（agent 怎么读这个系统）

`fleet_mcp.py` 是编队状态的**只读** MCP 面，三个工具：`fleet_status` / `my_queue` /
`awaiting`。接法、输出契约、以及**为什么写操作不做成工具**见 `FLEET-MCP.md`。

一句话边界：**投递 / send-keys / 发消息不做成工具**，走 desk 审批。
`--allowedTools` 实测不是边界（agent 用 `ToolSearch` 绕到 `Monitor`，
那个工具入参里有 `command`），边界拦不住的东西就不能做成工具。
`tests/test_fleet_mcp.py::test_一期不许有写工具` 是这个决定的闸。

## 判据和 IO 分开

这个仓库的 bug 集中在一类：**判据长在外部状态上，所以测不了，所以只能靠跑一天真流量验收。**
已经治过三层，模式一致，新代码照抄：

- 判据做成**只吃数据的纯函数**（`parse_*` / `match_*` / `select_*` / `build_*`），不读盘、不问 tmux、不看时钟；
- IO 藏在一个**可替换的接口**后面，真假两个实现（`ScreenSource` / `RouteSource`）；
- 「现在」当参数传进去（`at`），别在判据里调 `now()` —— 否则测边界只能真等。

## 跑测试

```sh
python3 -m unittest discover -s tests      # 全套
python3 tests/_audit_run.py                # 同一套 + 证明没碰 data/
```
