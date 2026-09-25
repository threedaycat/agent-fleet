# 编队层 —— 官方 Agent Teams 怎么起、怎么管、有哪些坑

> **2026-09-25 合入说明**：这份是另一条分支（8/1–8/2，官方 Agent Teams 接入实验）留下的**实测笔记**，只合了文档、没合代码。文中提到的 `agentteams.py` / `winnames.py` / board 编队视图**不在当前主线上**；窗口名串台的问题已由 dotfiles 的 tmux 配置（`automatic-rename-format` 两级 + 截断）解决。§3 的实测坑与代码无关，仍然有效 —— 以后真要接官方 Agent Teams，先读 §3。

> 一句话：fleet 从此管**两层**。原来那层是「一支舰队里有哪些船」（跨项目、跨会话、
> 长命、能被钉钉叫醒）；这层是「一条船上有哪些人」（一个会话内部的班组，跟会话同生共死）。
> 两层的真相存在两个地方，**互不知道对方存在** —— 把它们接起来就是本文和 board 的全部工作。
>
> 总纲看 [SYSTEM.md](SYSTEM.md)，消息采集看 [README.md](README.md)，
> picker 侧的实现方案看 [PICKER-FLEET-PLAN.md](PICKER-FLEET-PLAN.md)。

**本文所有「实测」结论的取样环境：claude 2.1.220 / tmux 3.7b / macOS，2026-08-01。**
Agent Teams 还是 research preview，字段和行为都可能变 —— 所以每条结论都写了**怎么复验**，
不要当永久真理用。

---

## 0. 两层的边界（先把这张表看懂，后面才有意义）

| | **舰队层**（fleet 原有） | **编队层**（官方 Agent Teams） |
|---|---|---|
| 单位 | 一个独立 Claude 会话 | 一个会话内的 teammate |
| 真相在哪 | `~/.claude/tmux-claude-status.json` | `~/.claude/teams/{team}/config.json` |
| 生命周期 | 长命，静默几天照样在册 | 跟 lead 会话同生共死 |
| 怎么派活 | `fleet.py wake`（tmux send-keys） | `SendMessage`（进程内信箱） |
| 谁能触发 | 钉钉 / 手机 / 定时 / 你 | **只有 lead** |
| 任务表 | `fleet.py task` 台账 | 官方共享任务表（带依赖图） |
| 规模 | 数十个会话 × 数个项目 | 一个会话里数个 teammate |

**关键认知：官方是「每个 session 一个隐式团队」。** 没有任何官方 API 横跨多个会话。
所以「管理数十个编队」= 数十个 lead 会话各带一个编队 —— **那一层只有 fleet 有**，
这也是 fleet 在官方做了 Agent Teams 之后仍然不可替代的原因。

对官方数据，fleet 的姿势**只有只读**（`agentteams.py` 连写的函数都不提供）。
官方文档明说 `config.json` 存的是运行时状态，手改会在下次状态更新时被覆盖。

---

## 1. 怎么起一个编队

### 1.1 开关（一次性）

```bash
# ~/.claude/settings.json
{
  "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" },
  "teammateMode": "auto"
}
```

`./install.sh` 的第 2 步会问你要不要开，也可以手动加。

- **它默认关闭**，是实验性功能。**token 消耗按 teammate 数量线性叠加** —— 额度紧就别开。
- `teammateMode`：`auto` / `tmux` / `iterm2` / `in-process`。写 `auto` 不写死 `tmux`，
  是为了让没装 tmux 的机器优雅退化成 in-process，而不是报错。
- 装了 tmux 且在 tmux 里 → teammate 会**分屏到当前 window**（这个选择带来 §3.2 那个坑）。

### 1.2 起人（每次）

**没有 setup 步骤。** 旧版那两个 `TeamCreate` / `TeamDelete` 工具已经删了，
开关一开，每个 session 自带一个隐式团队，直接起人就行：

> 「起 doctor 和 desk 两个 teammate」

主会话（lead）用 Agent 工具带 `name` 参数生成，**名字就是它的地址**。
名字要和 `config.json` 的 `fleet.roles` 对上，board 才认得出角色。

- 追加指令：`SendMessage` 发给名字，teammate 保留自己的上下文继续做，不是重开一个。
- 看进度：`Shift+Down` 在 teammate 面板间循环切换；tmux 里也能直接切 pane。
- **teammate 不能再起 teammate**（嵌套被官方挡掉了）。
- teammate 继承 lead 的权限模式和 effort 等级。

### 1.3 角色约定

`config.json` 的 `fleet.roles`：成员名 → `{kind, job}`。`kind` 只有两个值：

- `alert` —— 产出的是「要你看一眼」，不是代码（doctor / desk）
- `work` —— 真在改东西的（writer）

本机现有三个角色：

| 名字 | kind | 职责 |
|---|---|---|
| `doctor` | alert | fleet 自检：launchd 进程活没活 · 会话卡住 · 队列积压 |
| `desk` | alert | 待决事项出口。它正开着选择框时不许打断，排队等它空（`desk_push.sh`） |
| `writer` | work | 单写手：同一个文件同一时刻只有它能改 |

**这张表是编辑判断，不是官方数据。** 官方 `config.json` 里唯一的硬信息是
`agentType == "team-lead"`，其余角色区分都得靠这张表。

### 1.4 把角色变成硬约束（推荐，未落地）

`~/.claude/agents/<name>.md` 的 frontmatter 能钉住 `model` / `tools` / `description`。
给每个角色建一个，有两个好处：

1. **角色约束从口头承诺变成机制**。doctor 的定义里不给 `Edit`/`Write`，
   「只读报警岗」就不再靠它自觉 —— 它**结构上改不了代码**。
2. **顺手解决 §3.3 的显示问题**：spawn 时 `subagent_type: doctor`，
   pane title 立刻变成 `✳ doctor` 而不是 `✳ general-purpose`，picker 一个字都不用改。

代价：定死 `tools` 之后角色越界会**直接失败**而不是被提示词劝阻。想让 doctor
顺手修个小 bug，得先改它的定义文件。

---

## 2. 官方给了什么 —— 这些 fleet 不要再造

| 官方的 | fleet 里对应的 | 怎么处 |
|---|---|---|
| **共享任务表** `TaskCreate/List/Update/Get`，带 `owner` + `blocks/blockedBy` 依赖图 | `fleet.py` 的 `cmd_task` 台账 | **两个都留**，各管各的。理由见 §2.1 —— 这条一开始写的是「收敛掉」，是错的 |
| **teammate 生成 + tmux 分屏** | 无（手工开会话） | 直接用 |
| **`SendMessage`** 点对点信箱 | `fleet.py:106 tmux_send` + Stop hook 投递 | 编队内用官方的：它有信箱，对方忙不丢消息；`send-keys` 会打断正在输出的会话 |
| **`TeammateIdle` / `TaskCompleted` hook** | `data/events.ndjson` 靠 Stop hook 写 | 白拿两个新事件源，接进同一条 ndjson |
| **落盘状态** `~/.claude/teams/*/config.json` | — | `agentteams.py` 只读解析，**只读** |

官方**不给**、只能 fleet 自己扛的：跨会话跨项目、唤醒完全 idle 的独立会话、
外部触发（钉钉/手机）、持久心跳与事件日志、舰队级视图。

### 2.1 为什么两套任务表都要留（一条被推翻的结论）

这份文档最初写的是「`fleet.py` 的台账收敛到官方任务表，官方版有依赖图」。
**这条是错的**，留在这里当记录，免得以后有人再走一遍。

看着像同一样东西，实际做的是两件事：

| | `fleet.py task` | 官方任务表 |
|---|---|---|
| 派给谁 | `target` = 全机器上的**某个项目/会话** | `owner` = **本会话内**的某个 teammate |
| 能不能叫醒 | `dispatch --wake` → `tmux_send` **敲醒另一个 window 里 idle 的会话** | 没有对应物 |
| 完成之后 | append-only ndjson，**留痕** | **文件从磁盘删掉**（§3.5），不留痕 |
| 活多久 | 持久，跨会话跨重启 | 跟 lead 会话同生共死 |

收敛过去会丢掉三样 fleet 赖以存在的东西：**跨项目派活、唤醒 idle 会话、历史**。
换来的只有一个依赖图 —— 那还不如直接给 fleet 的台账加 `blocks`/`blockedBy`，
代价小得多，而且不用把持久数据搬进一个「完成即删」的存储里。

**正确的分工**：编队内部的活用官方任务表（teammate 能自己认领、有依赖图）；
跨项目、要唤醒、要留痕的活用 fleet 台账。两边都不去读写对方 ——
board 和 picker 各自渲染，谁也不假装自己是另一个的镜像。

唯一该消除的重复是**显示层**：一条 `in_progress` 的官方任务已经出现在它执行者那行了，
provider 就别再单独吐一行（见 PICKER-FLEET-PLAN §6）。

---

## 3. 实测坑（全部踩过，每条附复验方法）

### 3.1 join key 是 `tmuxPaneId`，不是 session id

成员项的**全部**真实字段：

```
agentId / name / agentType / model / color / joinedAt /
tmuxPaneId / cwd / backendType / isActive / prompt
```

**成员级没有任何 session id 字段。** 早期代码试 `sessionId` / `session_id` / `sid`
三个键，一个都不存在，于是 `member_state()` 恒返回空、board 上每个成员都是「状态未知」。

唯一的连接点是 `tmuxPaneId`（形如 `%70`）↔ `fleet.sessions()` 值里的 `pane`
（注意那个 dict 按 sid 索引，pane 藏在值里，得遍历值）。

两个必须处理的例外：

- **lead 的 `tmuxPaneId` 是字面量 `"leader"`**，不是 pane。只认 `%` 开头的。
- **顶层 `leadSessionId` 是过期的。** team 目录跨会话复用，lead 换了会话它不刷新
  （实测它还指着一个早停了的会话，连 lead 成员项里的 `cwd` 都是旧的）。
  **别拿它兜底认 lead。** 后果是 lead 自己关联不上、显示「状态未知」——
  这是诚实的失败，不要硬造。

复验：`python3 -c "import json,glob,os;[print(json.dumps(json.load(open(p)),ensure_ascii=False,indent=1)) for p in glob.glob(os.path.expanduser('~/.claude/teams/*/config.json'))]"`

### 3.2 teammate pane 会进状态文件，但**要等它第一次 Stop hook**

`claude-tmux-sessions` 那四个用户级 hook（`UserPromptSubmit` / `Stop` /
`Notification` / `SessionEnd`）对 teammate 子会话**照常触发**，所以 teammate 的 pane
最终会出现在 `~/.claude/tmux-claude-status.json` 里。

但**刚起的那几分钟查不到** —— 要等它跑完第一轮。
**显示「状态未知」即可，不要当成掉线报错。**

### 3.3 `window_name` 会被同 window 的多个 claude pane 互相污染；`pane_title` 不会

teammate 分屏进**同一个 tmux window**，每个 Claude 都往 window name 写自己那段，
拼成一条串；而且每个 pane 记的是自己刷新那一刻的快照，**内容互不一致**：

```
window_name（三个 pane 共享一份，拼成 4 段）
  ⠂ 如何使用 agent-team✳ general-purpose✳ general-purpose⠂ general-purpose

pane_title（各自独立，干净）
  %61  ⠂ 如何使用 agent-team      ← lead，内容是会话摘要
  %70  ✳ general-purpose          ← desk
  %69  ✳ general-purpose          ← doctor

对照组（单 pane 的 window，两者逐字节一致）
  %50  title=[✳ 检查WorkOS是否需要更新]  win=[✳ 检查WorkOS是否需要更新]
```

**所以显示名一律取 `pane_title`，别用 `window_name`。**

**但 `window_name` 本身是可以修好的，而且该由 fleet 来修** —— 见
`winnames.py` / `./run.sh windows`。补一条实测根因，因为它不在 Claude Code 那边：

拼接来自 **tmux 自己的 `automatic-rename-format`**。如果那个格式里用了 `#{P:...}`
（遍历窗口里每一个 pane 再首尾相接），一个窗口一个 Claude 时它很好用，编队分屏之后
就开始串台。顺带排除掉一条：本机 `allow-rename` 全局是 `off`，**所以跟终端标题
转义无关**，别往那个方向查。

改全局格式能止血，但那会把单 Claude 窗口上本来好用的行为一起改掉，而且它
**永远拿不到队员的名字** —— 名字只存在于 `teams/*/config.json`。fleet 是唯一同时
看得见两边的一层，所以名字在这里算完、用 `tmux rename-window` 写回去：

```
⠐ 如何使用 agent-team⠐ general-purpose⠐ general-purpose
⚓ 如何使用 agent-team · doctor+writer
```

两个实现要点：**`rename-window` 一条命令同时完成改名和钉住**（tmux 会自动关掉那个
窗口的 `automatic-rename`），不用去动窗口选项、也就不用记得回头恢复；名字一律带
`⚓ ` 前缀当所有权标记，**不带前缀的窗口一律不碰**，那些可能是人手起的。

**但 `pane_title` 认不出 teammate 是谁。** Claude Code 起 teammate pane 时确实调了
`select-pane -T <teammate 名>`，但那个 pane 里跑起来的 Claude 随后会用终端标题转义
**把它覆盖掉**，最终留下的是 `agentType`。三个用 `general-purpose` 起的 teammate
长得一模一样。

**活下来的只有边框色** —— `doctor` 是蓝、`desk` 是绿，跟 `config.json` 的 `color`
字段一致（Claude Code 同时设了 per-pane 的 `pane-border-format` 和边框色）。
这是 tmux 层唯一没被覆盖掉的身份信号，但它不是文本，进不了列表的名字列。

teammate 的名字在别处也都没有：`~/.claude/sessions/` 里没有（见 §3.8）、环境变量里
没有、tmux 用户选项里也没有。**唯一可靠来源就是 `teams/*/config.json` 的 `name`
按 `tmuxPaneId` join** —— 别再找第二条路。想让 `agentType` 本身变成角色名，走 §1.4。

复验：`tmux list-panes -a -F '#{pane_id}|title=[#{pane_title}]|win=[#{window_name}]'`

### 3.4 teammate **不能自报坐标**

teammate 在自己会话里跑 `tmux display-message -p '#{pane_id}'`，拿到的是
**Bash 工具那个 shell 所在的 pane**，不是 agent 自己的 pane。实测 desk 因此
把自己报成了 `%68`（实际 `%70`，而 `%68` 是个空 zsh）。

**坐标只认 `config.json` 的 `tmuxPaneId`。** 这条对任何 teammate 都成立。

后果比听起来严重，因为**失败是静默的**：往空 shell 送事项，`desk_push.sh` 的
`desk_busy()` grep 不到任何选择框特征行 → 判定 idle → 直接 inject →
文本当命令回车执行掉 → 队列不留痕、退出码 0、事项凭空消失。

### 3.5 完成的任务**文件会从磁盘上删掉**

实测：`#1`/`#2`/`#3` 一标完成，`1.json`/`2.json`/`3.json` 立刻消失，
目录里只剩未完成的。两个后果：

1. **`completed` 计数结构性恒为 0** —— 已完成的根本不在返回值里。
   board 上别给它留格子，那会是一个永远显示 0 的坑。
2. **判「还挡着没」不能只看 `status == "completed"`**（那个集合恒空）。
   按旧写法，一条 `blockedBy` 指向已完成任务的条目会被**永远算成「挡住」**。
   正确判据：blockedBy 里的 id **在磁盘上还找得到、且没标完成**才算真挡着；
   找不到 = 完成后被删了 = 已解开。（见 `agentteams.py:task_stats`）

   **错的那一侧是假阳性，不是假阴性** —— 这个方向容易记反，所以写死在这里。
   实测：任务 2 `blockedBy: [1]`，`#1` 已完成但文件还在 → 正确显示 `待领`；
   把 `1.json` 删掉再渲染 → 任务 2 立刻翻成 `挡住 … 等 #1`，
   **开始声称自己在等一个已经做完的任务**。而删除正是官方的常态行为，
   所以按旧写法的界面会随着任务推进**越来越多地虚报阻塞**，
   并且指向一个查无此物的编号 —— 看到的人会去找一个不存在的任务。

   这条踩过两次：`agentteams.py` 早就修对了，同一个概念在
   `claude-tmux-sessions/bin/session-digest.py` 里被独立实现了一遍，
   用的是「还在磁盘上的 completed 记录」，方向正好相反。
   **同一个概念在两处各写一遍，本身就是一次免费的交叉验证 ——
   前提是有人真去对照。** 改动任何一侧之前，先看另一侧怎么写的。

   ⚠️ **正确口径有一个代价，是取舍不是缺陷，写在这里免得被当成 bug 修回去。**
   新口径是「只被**我看得见、且未完成**的任务挡住」。而「看不见的 blocker」有两种：

   1. 已完成后被删 —— **常态**，正是要修的；
   2. 悬空引用 —— `blockedBy` 指向一个磁盘上从来不存在的 id。

   **两者在磁盘上长得一模一样，无法区分**，所以修掉 1 必然连 2 一起静音。
   常态优先是对的选择，但**从此悬空引用不会再被察觉**。

   实测确认它没有矫枉过正：`blockedBy` 指向一条**存在且未完成**的任务时，
   照样报「挡住」—— 没有退化成「永远不报 blocked」。

### 3.6 `owner` 是真的有

早期基于老格式样本的悲观结论（「任务条目不一定有 owner，所以『他在做哪条』
大概率填不上」）**已作废**。2.1.220 的 `TaskUpdate` 确实往任务文件里写 `owner`，
teammate 认领的瞬间就能读到。所以「成员行显示他此刻在做哪条任务」这个设计值得做。

复验：`ls ~/.claude/tasks/*/ && python3 -c "..."` 看 `owner` 字段。

### 3.7 `~/.claude/tasks/` 里有残骸

`tasks/` 会给**任何**用过任务表的会话留目录（本机有 6 个 7 月的完整 UUID 目录，
早于 2.1.178 那次改版；新的是 `session-` + session_id 前 8 位）。

**判断「有没有活动编队」只认 `teams/`，不认 `tasks/`** —— 拿后者当判据会假报。
目录名一律 `glob`，不按名字拼路径。

---

### 3.8 手动会话名不在 hook 载荷里，但在 `~/.claude/sessions/` 里

`/rename`（以及 `--name` / `claude --bg -cn <name>`）能给会话起一个人手选的名字。
显示名的降级链想拿它当第一级，于是有个问题：**它落在哪儿？**

**结论：hook 载荷里没有。** 反查 2.1.220 二进制确认过——hook 输入是一个公共构造器
拼出来的，字段逐字是 `{session_id, transcript_path, cwd, prompt_id, permission_mode,
agent_id, agent_type, effort}`，没有 `session_name`。statusline 的载荷是**同一个构造器
再拼几样**，`session_name` 只在那一处、而且是「有名字才拼进去」的条件字段。
所以 statusline 拿得到、hook 拿不到，**是设计如此，不是版本问题**。

**它在 `~/.claude/sessions/<pid>.json`** —— 官方自己维护的会话索引，一个进程一个文件：

```
{pid, sessionId, cwd, startedAt, procStart, version, entrypoint, kind,
 name, nameSource, status, updatedAt, statusUpdatedAt}
```

`nameSource` 能区分**人起的名**和**系统凑的名**：手动 `/rename` 过的**缺这个字段**，
自动生成的是 `"derived"`（形如 `workos-8f` / `minimal-agent-56`）。正好是降级链
第一级要的判据 —— 只认人起的，不认系统凑的。

它带 `sessionId`，跟 `tmux-claude-status.json` 用同一个 key 就能对上，**不用改 hook、
不用碰 statusline，多读一个目录就行**。

⚠️ 代价：文件按 **pid** 命名，进程崩了不保证清理（本机就有 7 月的残留）。
所以要按 `sessionId` 反查、拿 `status`/`updatedAt` 兜一层，别按 pid 认。

**判据要写成「`nameSource` 字段缺失」，不是「`!= "derived"`」** —— 端到端复现过
（一次性会话、私有 tmux server、中性 cwd）：`/rename` 不是给 `nameSource` 写别的值，
是**把这个 key 整个删掉**。

```
rename 前：  "name": "wd-36",        "nameSource": "derived"
rename 后：  "name": "dr-probe-name"      ← nameSource 不见了
```

顺带三条同一次测出来的：记录在**会话启动时**就建好（带 derived 名），不是 rename 时才有；
`/rename` **原地改已有文件**，`pid`/`sessionId`/`startedAt` 全不变，所以「同 sessionId
两份文件」只可能来自两个 pid 各写一份，rename 这条路走不到；会话退出后记录会自动消失。

还有一条**安全性**理由，比上面的口味问题更硬：**derived 名可以是登录名。**
它拿 cwd 生成，cwd 是 `$HOME` 时得到的就是「登录名 + 两位后缀」（本机真有这么一条）。
**拒掉 derived 顺手把这个泄漏也堵了**，不需要第二道闸 —— 想把规则放宽成
`!= "derived"` 的人，得先接受这个代价。

### 3.9 `pid` 是一座通往 tmux pane 的桥（**缺一环，但后半段是通的**）

`sessions/` 里没有任何字段直接写 pane。但文件按 `pid` 命名，而 pid 能反查 pane：

```
sessions/<pid>.json  →  ps -o ppid=  →  tmux #{pane_pid}  →  %NN
```

两条真实数据上都通了（`9158 → ppid 8975 → %61`、`90551 → ppid 84496 → %7`，后者与
`tmux-claude-status.json` 一致）。**这是一条只用官方数据的 session→pane 映射。**

⚠️ **但它今天救不了「哪个 pane 是 lead」**（§3.4），因为链条断在第一环：编队 config 的
`leadSessionId` 在 `sessions/` 里**找不到任何 `sessionId` 与之相等**。最像 lead 的那条
记录（cwd 恰好对上 lead roster 的 cwd）`sessionId` 也对不上。**为什么对不上没查出来**
（resume 换 id？两套 id 空间？）—— 这一条是未解的，别当成已知。

所以现在仍然用排除法（「这个 session 里不是 teammate 的那些 pane」）。
记在这里是因为**后半段已经验证可用**：哪天 `leadSessionId` 能对上 `sessions/` 的
`sessionId`，lead 就能被**正面识别**，而不是靠排除。

**顺带，完整的 hook 公共字段表**（`tmux_status_update.py` 现在只取了 `session_id`，
其余全丢了）：

| 字段 | 说明 |
|---|---|
| `session_id` / `transcript_path` / `cwd` | 必有 |
| `prompt_id` | 同一轮用户提问到下一轮之间所有事件共用的 UUID |
| `permission_mode` | |
| `agent_id` | **只在 subagent 里出现**，官方明说用它（不是 `agent_type`）判「这是不是子 agent」 |
| `agent_type` | `"general-purpose"` 这类类型名 |
| `effort` | 当轮推理档位，同时也以 `CLAUDE_EFFORT` 环境变量暴露 |

Stop 事件另有 `stop_hook_active` 和 **`last_assistant_message`**（官方注释说它就是
为了免去解析 transcript）；SubagentStop 另有 `agent_id / agent_transcript_path /
agent_type / last_assistant_message`。

**`last_assistant_message` 对 fleet 直接有用**：`fleet.py:362 last_assistant_text()`
现在为了拿「最后一句」在自己翻 transcript 文件，而 Stop hook 的载荷里本来就带着它。

`agent_id` / `agent_type` 能不能在 tmux 后端的 teammate 会话里真的拿到，**没有验证**
（要装 hook 才看得到载荷，那要改全局配置）。别当成已知。

---

## 4. 怎么看（舰队面板）

```bash
python3 agentteams.py                    # 编队现状：谁在场、任务分布
python3 fleet.py list                    # 舰队现状：哪些会话在忙/闲/静默
open data/board/index.html               # 网页版 board，两层都画
```

**board 的口径**（`board_html.py`）：

- 成员状态按 `tmuxPaneId` ↔ `pane` 关联（§3.1），对不上就显示「状态未知」，
  **绝不拿别人的状态冒充**。
- 没有活动团队时**整块不渲染**，而不是画一个空表格 —— 空区块比没有区块更吵。
- `fleet.roles` 里没配的成员按 `work` 显示，不隐藏 —— 新起一个 teammate 不该
  因为忘了配表就在 board 上消失。反过来配了但人没起，也不显示。

picker 侧（`prefix+g` 那个列表）把编队画进会话列表的方案见
[PICKER-FLEET-PLAN.md](PICKER-FLEET-PLAN.md)。

---

## 5. 什么时候**不**该起编队

- **活很小。** research preview 的定位就是 token-intensive，token 按人数线性叠加。
- **要并行改同一批文件。** 让每个 teammate 负责不同模块，或用独立 git worktree。
  本仓库的规矩是**单写手**（见 SYSTEM.md §4）：同一个文件同一时刻只有一个人能改。
- **活需要跨会话/跨项目。** 那是舰队层的事，用 `fleet.py wake`，不是编队层。
- **要长命。** teammate 跟 lead 会话同生共死；要留下来的活派给独立会话。
