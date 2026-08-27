# dtwatch —— 钉钉消息哨兵

> 反方向的那半边（用手机钉钉遥控这台电脑上的 Claude Code）在 **[DTCC.md](DTCC.md)**，
> 共用同一个自聊天窗口，互不干扰。


有人私聊我、或在群里点名到我的时候，把消息抓下来、分个轻重、攒成队列，
再由 Claude 读队列去补上下文、拉文档、判断要不要做事、该不该提醒。

## 为什么是这个结构

钉钉这边只给了两种拿消息的路，能力不一样，所以两条都用：

| 路子 | 覆盖 | 实时性 | 靠不靠谱 |
|---|---|---|---|
| `dws event consume user_im_message_receive_at` 长连接 | 只有「群里 @ 我」 | 秒级 | 进程一挂就断，不补历史 |
| `dws chat message list --group <cid>` 轮询 | 私聊 + 群，全都行 | 取决于间隔 | 带游标，重启不丢 |

轮询是**唯一真相**，长连接只是加一个「不在关注列表里的群突然 @ 我」的补漏。
（顺带记一笔：`dws chat +at-me` 这个现成的接口用不了，账号没有「消息搜索权益」，
所以 @ 我只能靠事件流 + 正文匹配名字这两条路，不要再去试它。）

分成「采集器」和「Claude」两层是故意的：

- **采集器**（`dtwatch.py`）只跑死规则 —— 谁发的、在哪发的、有没有关键词。
  规则错了顶多是多一条或少一条，不会瞎判断。它不发消息、不做决定。
- **Claude** 读队列，做真正需要理解的事：这句话到底要我干嘛、该拉哪份文档、
  是不是有 deadline、要不要现在提醒。结论用 `mark` 写回去。

## 目录

```
dtwatch.py        采集 + 打标 + 队列，全部命令都在这
dtcc.py           手机遥控那半（播报 / 收指令 / Stop hook）
fleet.py          会话侧：心跳、唤醒、事件日志、任务台账
board_html.py     网页版 board（渲染静态 HTML，数据层复用 fleet.py）
picker_items.py   给 tmux picker 供「附加条目」（待办混进会话列表）
run.sh            守护脚本，拉起/停掉后台进程
config.json       关注哪些群、谁是关键人、什么词算重要、免打扰时段（不进版本库）
config.example.json    上面那个的结构模板
settings.example.json  Claude Code 的 Stop / Notification hook 配置模板
TRIAGE.md         Claude 每轮巡检照着做的流程
data/
  registry.json   会话 ID → 名字 / 是不是单聊（缓存，省接口调用）
  state.json      每个会话的时间游标 + 上次采集/提醒时间
  inbox.ndjson    归一化后的消息流，一行一条，只追加
  triage.json     每条消息的处理结论（done / ignored / snoozed）
  announced.json  已经推送给 Claude 的消息 ID，防重复播报
  at_events.ndjson  @我 实时事件原始流
  tasks.md        Claude 认定「要我做事」的条目
  docs/           为了看懂上下文而下载的钉钉文档 / 附件
  run.log / poll.log / at.log
```

## 用法

```bash
./run.sh install 300    # 挂 launchd：开机自启 + 挂了自动拉起（推荐，已装）
./run.sh uninstall      # 卸掉 launchd
./run.sh status         # 托管方式 + 进程在不在 + 队列积压情况
./run.sh start 300      # 手工起（临时用，重启电脑就没了；跟 launchd 二选一）
./run.sh stop
./run.sh tail           # 跟一下采集日志

python3 dtwatch.py pending --level normal      # 看待处理（默认合并连发消息）
python3 dtwatch.py pending --level high        # 只看最紧的
python3 dtwatch.py show <msgid> --context 30   # 看某条 + 前后文
python3 dtwatch.py mark <msgid> --status done --note "已回"
python3 dtwatch.py remind "..."                # 往自己钉钉单聊发提醒
python3 dtwatch.py reclassify                  # 改完 config.json 后重打历史标签
```

## 打标规则

| level | 什么情况 |
|---|---|
| `high` | 真人私聊我；或群里点到我名字（低优先级群除外） |
| `normal` | 机器人私聊我（IM 自带的 AI 助手这类）；关注群里命中关键词；私聊里的卡片消息 |
| `low` | 其余全部 —— 全员群日报刷屏、CI/CD 监控推送、系统卡片；**我自己贴过表情的** |

**贴表情等于处理完了。** `message list` 每条消息都带 `emotionReplyList`，
里面有谁贴了什么表情。我常用「贴个 OK」代替回一句话，所以每轮采集完会回扫一遍
还开着的条目（`sweep_acks`），只要我自己贴过表情就直接结掉。
不解析这个字段的话，我已经点头的事在队列里永远显示「没人接」——
一条约时间的消息就这么被反复催了一小时。

`pending` 默认只给 high + normal。日报卡片正文是一坨 JSON 加 `dingtalk://` 深链，
里面必然带「日报」「上线」这些词，专门有一条规则把它压成 `low`，不然队列会被冲垮。

想调整就改 `config.json` 的 `groups_always_watch` / `priority_keywords` /
`priority_senders` / `low_priority_conversations`，改完跑一次 `reclassify`。

## 按项目把消息派给对应的 Claude 会话

tmux 里每个项目开一个 Claude。某位同事说的都是项目A 的事，那些消息应该**直接落到
项目A 那个会话手里接着干**，而不是全堆到哨兵会话里由它转述一遍。

`config.json` 的 `route` 就是这张表 —— 群名 或 发送人名 → 目标 session id：

```json
"route": {
  "同事A":        {"session": "<session-id>", "label": "项目A"},
  "需求-项目A":   {"session": "<session-id>", "label": "项目A"},
  "项目A":        {"session": "<session-id>", "label": "项目A"}
}
```

**优先按群名配，别按发送人配。** `route_of` 先查会话名再查发送人名，
发送人那一层是「这个人只谈这一个项目」才成立的捷径 —— 同事A 符合，同事B 不符合
（她同时在催项目B），按她的名字路由会把项目B 的消息也派到项目A 会话去。

配上之后：

- `dtwatch.py pending` **默认只显示没派出去的**，末尾会提一句「另有已派给别的会话的：
  项目A 2 条」，免得哨兵以为队列空了。`--session <sid>` 看某个会话的，
  `--all-routes` 全看。
- `dtcc.py hook-stop` 在那个会话每次收尾时问一句 `dtwatch.py for-session <sid>`，
  有活就注入回去让它接着干。`data/injected.json` 记账，同一条只投一次。
- 派出去的条目在哨兵这边打 `route:项目A` 标。

拿 session id：在那个会话里看 `/status`，或者 `data/cc/cc.log` 里的 `sid=xxxx` 短标签
对应 `~/.claude/projects/*/` 下的文件名。

**边界**：投递发生在**那个会话「一轮活干完」的时候**，不是推送。它要是完全闲着
（没人给它派活、也没在跑），消息就排着，等你下次在那个 pane 里说话时一起进去。

## 手机指令的即时回执 + 叫醒（push-loop）

原来只有 **pull**：`hook-stop` 在某个会话收尾时才去问「有我的指令吗」。
**所有会话都闲着的时候压根没人去问** —— 实测有过一次手机上发的话十个小时
没人接，就是这个原因（不是 hook 坏了，是没人来拉）。

所以加了一条 **push**：`dtcc.py push-loop` 常驻（launchd `<label-prefix>.push`，
8 秒一扫），一有新指令就：

1. **在他那条指令上贴个「OK」表情**当收到回执。这是**唯一**允许的回执方式：
   - **表情 = 可以**（他自己平时就用「贴个 OK」代替回一句话）。
   - 🚫 **待办条目 = 禁止。绝不许用 `dws todo` 之类做回执或提醒** ——
     用户明确反感被加进 IM 自带的待办清单（原话「别给我待办清单」）。
     这条没有例外，也别为了"更醒目"去试。IM 自带待办本身也不准，
     任务流转有它自己的系统，这套东西不该往那儿写。
   - 另发一条**文字**只留给**异常**（目标 pane 已经关了、叫不醒），
     那不是回执而是报告问题 —— 正常路径一个字都不多发。
2. 算出这条归哪个会话：引用了某条播报 → 认回那条播报的主人；裸指令 → 最后说话的会话；
   都认不出 → 不动，留给 pull 先到先得。
3. 目标会话**闲着** → 立刻 `tmux send-keys` 把指令打进它的 pane；
   **在跑** → 什么都不做，留给它自己收尾时拿（**不打断，不加 `--force`**）。

**push 和 pull 不会把同一条指令做两遍**：共用 `claim()` 那把 `O_CREAT|O_EXCL` 锁。
关键细节是 **叫醒失败要 `unclaim()` 退锁** —— 否则这条指令 push 没送到、
pull 又被锁挡着，两头都拿不到，直接消失。游标同理：没被 push 消费的指令，
`advance_cursor` 会把游标停在它前面，pull 那边照样拉得到。

```bash
python3 dtcc.py push-loop --once --no-wake   # 只看路由判断对不对，不真叫、不真发
```

## fleet —— 会话之间互相看得见、能被叫醒

`dtwatch` 管「消息怎么进来、派给谁」，`fleet.py` 管另一半：**会话侧**。

```bash
python3 board_html.py --open               # 生成网页版 board 并打开浏览器
python3 board_html.py --loop 10            # 每 10 秒重生成一次（常驻，浏览器自己刷新）
python3 fleet.py board                     # 同上，转发到 board_html.py
watch -n5 python3 fleet.py board           # 挂着当常驻监控屏
python3 fleet.py list                      # 谁在忙、谁闲着、谁的 pane 已经没了
python3 fleet.py wake <sid|项目名> --task "..."   # 把任务打进那个 pane，叫它接着干
python3 fleet.py wake 项目A --task "..." --dry-run # 先看会打到哪，不真发
python3 fleet.py log --tail 30              # 事件日志：谁什么时候干了什么、产出在哪
python3 fleet.py task add --text "..." [--target 项目A] [--reason "为什么还没派"]
python3 fleet.py task dispatch <id前缀> [--target X] [--wake]   # 派出去，--wake 顺手叫醒
python3 fleet.py task done <id前缀> [--where 产出位置]           # 交活
python3 fleet.py beat --note "刚干完什么"    # 手工报一次心跳（平时不用，hook 会自动报）
```

**项目短名配在 `config.json` 的 `fleet.projects`**（cwd 前缀 → 短名，最长前缀优先）。
不配的话会退回目录名 —— 那就会出现「这套东西叫 dtwatch，署名却写着 agent-fleet」
这种膈应事。新开一个项目会话，在这张表里加一行就有名字了。

**会话状态的真相是 `~/.claude/tmux-claude-status.json`**（见下面「外部依赖」一节）。
fleet **不再自己维护一份心跳** —— 两套会话追踪器必然打架，谁对谁错说不清。

`data/fleet.json` 退化成**薄 sidecar**，按 `session_id` 关联，只补那个文件没有的三样：
`project`（项目短名）、`note`（最后干完的一句话）、`last_wake`。
`dtcc.py hook-stop` 每次收尾调 `fleet.beat()` 写的就是这三样；失败只写日志，绝不挡住收尾。

⚠️ **只读那个状态文件，绝不改 claude-tmux-sessions 仓库** —— 它是通用可分享的，
IM/工作相关的东西不许进去。

### 外部依赖：`~/.claude/tmux-claude-status.json`

**这个文件不由本仓库产生。** 它由一个独立的通用工具 **claude-tmux-sessions** 维护 ——
那套东西给每个 tmux pane 里的 Claude Code 会话装一个 hook，会话状态一变就把
`{pane, session, window, window_name, pane_index, cwd, status, updated_at, session_id}`
写进这个 JSON。本仓库**只读它**。

> TODO：补上 claude-tmux-sessions 的仓库地址和安装说明。

**没有这个文件会怎样**：`fleet.py` 的 `sessions()` 拿不到任何实时会话，于是

| 命令 | 表现 |
|---|---|
| `fleet list` | 列不出会话，或只剩 sidecar 里记得的历史条目（标 `remembered`） |
| `fleet board` / `board_html.py` | 「谁在忙/闲」「卡住」这两块全空，路由表里的会话一律显示「查不到」 |
| `fleet wake <项目>` | 解析不到目标 pane，直接失败 —— 叫不醒任何会话 |
| `picker_items.py` | `[!] 卡住的会话` / `[Q] 待投递` 两类条目出不来 |

消息采集、打标、队列、`pending`、`mark` **不受影响** —— 那半不依赖会话状态。
`fleet.py` 还有一层兜底：读 `~/.claude/projects/*/` 下 transcript 的 mtime 判断
「这个会话刚刚还在写盘」，所以状态文件缺失时仍能判断活没活，但**拿不到 pane**，
因此依然叫不醒（猜 pane 会把活派错地方，宁可失败）。

**任务台账 `data/tasks.ndjson`** 补的是「**闲着的会话不会自己去拉名下积压**」这个洞 ——
投递只在目标会话收尾时触发，它一闲下来就没人推，于是队列里有活、会话却静默
（board 上出现过「某项目 19 条未处理、会话还静默 1 小时」）。
一条 = 一件活的一次状态变更，`{id, text, target, status, reason, ts}`，
**只追加**，同一个 id 取最后一条为准 —— 几个会话同时写也不会互相覆盖。

`board` 第 ⑤ 节就是给这个的，**「卡住」的判据是：某会话静默 且 它名下队列未处理数 > 0**，
直接标 `🔴 该 wake 它`。另外收尾 hook 写的事件带 `kind=turn-done`、`task done` 带
`kind=task-done`，board 末尾「最近交活」读的就是它们 —— 主会话看一眼就知道谁交了活，
不用去轮询每个 pane。

### board 是网页版（2026-07-31 起，终端版已退役）

`board_html.py` 渲染一个静态 HTML 到 **`data/board/index.html`**，浏览器开一个标签页常驻，
页面自己每 10 秒重载。**不起 web server、不引框架、不装依赖** —— 一个文件而已，
挂了不影响任何东西，重跑一次就有。

数据层完全复用 `fleet.py`（`sessions()` / `inbox_stats()` / `tasks_load()` /
`dispatch_flow()`），所以网页和别的命令看到的永远是同一份数，不会一边说 4 条一边说 3 条。

**为什么不用终端了**：等宽字符画的表格，中英文混排永远差一格；长文本要么截半截要么撑爆行。
换成 HTML 之后这些限制都没了，还能：心跳的最后一句**完整显示**、@你的消息**完整正文**、
**图片直接内嵌**（`dws chat message download-media` 下到 `data/board/img/`，按 mediaId 哈希
命名所以不会重复下）、调度流水给到 30 条。深浅色跟随系统，窄窗口是表格自己横向滚、页面不滚。

它**只负责看，不做交互** —— 点了能 wake 那种是 picker 的活。

（下面这段讲的是终端版的设计原则，逻辑同样适用于网页版的信息取舍）
`board` 的设计原则是**让人扫一眼就知道「有没有事要我管」，不是读报表**：
第一行只有结论（`✅ 无事` 或 `⚠️ N 件要你处理`），**没事的区块整块不印**；
会话一览只留「名字 · 跑/闲 · 多久没动」，note 一律不显示（截半截比不显示更糟）；
列宽按显示宽度算（中文和 emoji 都占两格），80 列能看；颜色只用在结论行和「卡住/超时」。

「要你处理」按优先级排：**@他本人的消息**（最前 —— 任何会话都无权自判闭环）→
**卡住**（会话静默且名下有未投递）→ **积压**（没配路由又在堆）→ **未派 / 超时**（台账）。

**「调度流水」是给他看这套东西怎么运作的**，不只是结果 ——
`时间 · 动作 · 从谁→到谁 · 内容`，动作是固定短词（路由/接单/让出/投递/唤醒/交活/急推/窗口），
**方括号里是判据**（`quote` = 他引用了那条播报所以派给它的主人、`default→main`、
`belongs-to-<sid>`）。数据全部来自现成日志（`data/cc/cc.log` 和 `events.ndjson`），没额外埋点。
⚠️ 目前**「回执」（贴 OK 表情）在流水里看不到** —— `react()` 只在失败时写日志，
成功的没记；要补得改 `dtcc.py`。

（下面这段是旧版四块表格的说明，board 已经重做，保留供参考）
`board` 的前四块：① route 表每条规则命中多少、派给谁、那个会话活着没；
② 没有任何路由匹配、全靠哨兵接的会话（按量排，这是"下一个该分出去的项目"）；
③ 归某个会话但还没投递进去的条数；④ 谁活谁静默。宽度自适应，中文按显示宽度对齐。

**为什么需要唤醒**：Claude Code 没有「往正在运行的会话推一条消息」的接口。
`for-session` 那条投递只在目标会话**一轮活干完**时触发，所以完全空闲的会话会一直排着不动。
要主动叫起来只有 `tmux send-keys` 这一条路 —— 代价是会打断正在输出的会话，
所以 `wake` 默认拒绝对 `state=busy` 的会话动手（真要插队加 `--force`）。

一次性、边界清楚的活（查个字段、跑个脚本）不要用 wake，直接 `claude -p` 起个无头进程更干净；
wake 留给**需要那个会话已有上下文**的活。

**`data/events.ndjson` 是多会话之间「记忆互通」的唯一真相**：只追加，一行一条
（`who` / `when` / `project` / `what` / `where`）。各会话自己按 project 过滤、
自己压缩成自己的文档，**不要让所有人读写同一份状态文件** —— 那样必然互相覆盖。

## hook 配置（整套遥控和投递的入口）

上面说的「投递」「心跳」「收尾时问一句有没有我的活」全都挂在 Claude Code 的
**Stop hook** 上，授权提醒挂在 **Notification hook** 上。没配这两个 hook，
采集和队列照常跑，但**没有任何东西会自己进到会话里** —— 只能手敲 `pending`。

结构见 **[`settings.example.json`](settings.example.json)**，把里面的 `hooks` 段
合并进 `~/.claude/settings.json`，`<REPO>` 换成本仓库的绝对路径（别用 `~`）。

两个要点：

- `Stop` 的 `timeout` **必须显式写**，而且要比 `config.json` 的 `cc.remote_wait_seconds`
  大一点。hook 默认只给 60 秒，遥控窗口开着时这个 hook 会挂着等最长
  `remote_wait_seconds` 秒，不写就会被掐断。
- 改了 `cc.remote_wait_seconds` 记得同步改这里的 `timeout`，两边是一对。

## 两个提醒通道

- **PushNotification** —— 桌面/手机弹窗，Claude 会话开着的时候用这个，不留痕。
- **`dtwatch.py remind`** —— 发到自己的钉钉单聊，会话关了也看得见，适合真的会忘的事。
  受 `config.json` 的免打扰时段（默认 22:30–08:30）和最小间隔（45 分钟）约束，
  `--force` 可以绕过。

## 已知边界

- 采集器已经交给 **launchd 托管**（`<label-prefix>.at` / `.poll`，前缀在 `run.sh` 顶部，
  默认 `com.workos.dtwatch`；plist 在 `~/Library/LaunchAgents/`）：开机自启，进程挂了自动拉回来。
  `./run.sh install [间隔秒]` 装，`./run.sh uninstall` 卸。
  装了 launchd 之后就别再用 `run.sh start`，会跑两份、抢同一个游标文件。
- **Claude 这侧的定时巡检（cron）只活在当前这个 Claude 会话里**，会话一关就没了，
  而且最多 7 天自动过期。采集器不受影响，照常攒队列，下次开会话时 `pending` 全在。
- **一个事件流只挂一个组织**（当前 profile 那个）。人同时在多个组织里的话，
  要覆盖第二个组织得再起第二个 bus。轮询这边不受影响，会话列表是跨组织的。
- **token 过期不用扫码，加 `--profile` 就会自动续。** access token 半天就过期
  （`dws profile list` 显示 `status: expired`），但 refresh token 有 30 天。
  报 `forbidden.accessDenied`「不支持跨组织访问数据」时，在原命令上补一次性 profile 即可：

  ```bash
  # 形如 `组织id:用户id`，用 `dws profile list` 查自己的，别写死在文档里
  --profile "<corp-id>:<user-id>"
  ```

  ⚠️ **这一串是凭据，不要提交进版本库、不要贴进任何会外传的文档。**
  `--profile` 不改全局 current，不影响采集器。**消息轮询是跨组织的；钉盘（drive）、
  文档（doc）、事件流（event consume）按组织隔离**，这三类才需要指定。
  （更正过一次：原来这里写「要先 `dws auth login`」，是错的。）
- 轮询走「最近活跃前缀扫描」：会话列表本身按活跃度排序，默认扫前 22 个，
  扫到底还有新消息就自动往下延伸，上限 60 个。一轮大约 20–30 秒。
