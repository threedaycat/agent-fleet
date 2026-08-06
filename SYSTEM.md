# 这套系统是什么 · 总纲

> 一句话：把钉钉里进来的事，**自动落到正确的项目会话手里、干完能自己收尾、
> 彼此看得见、只在真需要你判断时才打扰你**。你要开的不是「一百个 Claude」，
> 是一个能替你调度它们的操作系统。
>
> 这份是**总纲 + 在新机器上跑起来的步骤**。分层细节各有专文：
> 消息采集看 [README.md](README.md)、手机遥控看 [DTCC.md](DTCC.md)、
> 巡检流程看 [TRIAGE.md](TRIAGE.md)、hook 配置看 [settings.example.json](settings.example.json)。

---

## 0. 一条铁律：机器搬运，Claude 思考

整套东西只有两层，别混：

| 层 | 谁 | 干嘛 | 带不带脑子 |
|---|---|---|---|
| **机器层** | launchd 托管的 Python 脚本 | 采集消息、打标、路由、投递、唤醒 | ❌ 死规则，不调模型 |
| **智能层** | tmux 里的 Claude Code 会话 | 收到活才理解、才干、才回 | ✅ 只有这层 |

**没有任何一个 Claude 在「监听」你的钉钉。** 监听和路由全是死脑筋的脚本。
Claude 只在最末端出现——活被派到某个项目会话手里，那个 Claude 才开始理解。

---

## 1. 两个工具，单向依赖

```
claude-tmux-sessions   通用、与消息来源无关     ← 「有哪些会话、什么状态、跳过去」的唯一真相
        ↑ 读                                    prefix+g 打开 fzf picker
dingtalk-watch （本仓库）  钉钉适配那半          ← 加「消息怎么路由、队列、唤醒、事件」
```

- **claude-tmux-sessions**（外部依赖，不在本仓库）：每个 Claude pane 的状态
  靠 hook 写进 `~/.claude/tmux-claude-status.json`（pane / session / window / cwd /
  status / session_id）。它是「会话存活/状态」的真相，本仓库**只读它**、不再自己维护一套。
  没有它会怎样、缺哪些功能，见 [README.md](README.md) 的「外部依赖」一节。
  > TODO：补上 claude-tmux-sessions 的仓库地址和安装说明。
- **dingtalk-watch**（本仓库）：只加钉钉那半。**红线：具体消息内容、联系人、群名一律
  不写进 claude-tmux-sessions**——那个是通用的，要保持跟任何一家公司无关。

---

## 2. 三个常驻进程（机器层）

`./run.sh install` 装成 launchd，开机自启、挂了自动拉回。

| 进程 | 频率 | 干嘛 | 什么时候跑 |
|---|---|---|---|
| `run.sh at-stream` | 推送长连接，闲时不动 | 接「群里 @ 我」实时事件 | 一直 |
| `run.sh poll-loop 300` | 每 300 秒 | 增量扫所有会话（只拉新消息），打标、排队 | 一直，很轻 |
| `dtcc.py push-loop` | 约 6–8 秒 | 盯自聊天，手机指令即刻回执 + 唤醒目标会话 | **只在 `remote on` 时**（出门才开，桌前不跑） |

- **轮询是唯一真相，长连接只是补漏**（不在关注列表的群突然 @ 我）。
- 钉钉**不推送你自己发的消息**，所以自聊天指令只能轮询——但只轮自聊天一个会话，且
  绑遥控窗口，桌前完全不跑。
- 别用长 `sleep` 判周期：macOS 睡眠会把 sleep 冻住（实测 `sleep 300` 睡醒跑到 37 分钟）。
  循环一律按墙钟比对上次执行时间。机器整夜干活要 `caffeinate`（见第 7 节）。

---

## 3. 一条消息怎么走到 Claude · 两个入口

**入口 A · 别人发给你的**
```
钉钉群/私聊 → 采集器(poll/at) → 打标+队列(inbox.ndjson)
           → 路由 route_of() 查 config.json 静态表 → 投递(Stop hook) → 项目会话 Claude
```

**入口 B · 你手机/mac 发给自己的指令**
```
你的自聊天 → dtcc collect()（> 前缀 或 引用【CC】播报 = 指令）
          → 认收件人 entitled()/owner_of_quote() → 投递(Stop hook) → 目标会话 Claude
```

两个入口共用**同一个投递口**：每个会话的 Stop hook → `dtcc.py hook-stop`，
会话一收尾就问「有我的活吗」，有就注入。`injected.json` 记账，同一条只投一次。

**关键限制**：投递只在会话「收尾那一下」触发。**完全 idle 的会话不会自己醒、也收不到
路由消息**——得主动唤醒（见第 5 节）。

---

## 4. 角色与工作流

- **你只跟主会话沟通**（总控那个）。要谁干什么告诉主会话，由它去唤醒/派活/
  把结果压缩回来。你不用自己管一堆会话。
- **文件所有权 = 单写手**：同一个 `.py` 文件同一时刻只能一个会话改，不然冲突。
  配置/路由/策略（`config.json`）主会话自己改；代码逻辑派给固定那一个写手会话。
- **跟人汇报用名字+坐标**（`项目A-run (项目A:2.1)`），不甩 session-id。
  内部路由/代码该用 session-id 就用；定位 pane 用稳定的 **pane-id（`%NN`，改名/挪窗口都不变）**，
  别用会变的 `session:window` 名。
- **规模慢慢加**：先 3~10 个跑稳，不是一上来 100 个。后台会话绝大多数只做只读活
  （L0：翻记录、看代码、跑测试、查资料），不产生待批；待批只从前台那几个会话出。

---

## 5. 心跳 / 唤醒 / 监控（`fleet.py`）

- **心跳**：每个会话 Stop hook 自动 `fleet.beat()`，记 project / 状态 / 坐标 / 最后一句。
- **唤醒**：`fleet.py wake <项目名|pane> --task "..."` → `tmux send-keys` 把活打进那个 pane。
  busy 会话默认拒（会打断输出），`--force` 插队。idle 会话靠这个才能被叫起来。
- **事件日志**：`data/events.ndjson`，一行一条（who/when/what/where），只追加。
  **会话间「记忆互通」的唯一真相**——各自按 project 过滤、各自压缩，不共写一份状态文件。
- **监控屏**：`fleet.py board` 一屏看四样（路由/待接管/队列/心跳），挂 `watch` 常驻。

---

## 6. 常用命令

```bash
# 监控
python3 fleet.py list                       # 谁在忙/闲/静默
python3 fleet.py board                       # 路由 + 队列 + 待接管 + 心跳（挂 watch 当监控屏）
while :; do clear; python3 fleet.py board; sleep 5; done

# 调度
python3 fleet.py wake <项目名> --task "..."   # 叫醒一个会话派活
python3 dtwatch.py pending --level normal     # 看没派出去的待处理
python3 dtwatch.py mark <msgid> --status done # 标记处理完

# 手机遥控（出门用）
python3 dtcc.py remote on 90                  # 开遥控窗口（push-loop 随之启动）
python3 dtcc.py remote off
python3 dtcc.py say "..."                     # 主动往手机播报一条（自动署名【CC·项目/会话】）

# 采集器托管
./run.sh install 300 / uninstall / status / tail
```

---

## 7. 在新机器上跑起来

```bash
git clone <本仓库> && cd dingtalk-watch
./fleet_up.py setup
```

`setup` 是一条**交互式流水线**：八步，每步先自检，已完成的自动打勾跳过，
没完成的停下来问你一句。任何时候 Ctrl-C 都能退，下次跑接着来。

有几步注定**不能一键**，流水线不假装能——它停下来把该办的事讲清楚，等你办完按回车：

| 步骤 | 为什么不能自动 |
|---|---|
| `dws auth login` | 钉钉授权是扫码换 token，且管理员要先在 open-dev 开「CLI 访问管理」 |
| `config.json` | 里面是你自己的 open_dingtalk_id / 关注群 / 路由表，换个人全不一样 |
| hooks 合并 | 要**并进** `~/.claude/settings.json`，不是覆盖——那文件里还有你自己的配置 |

其余五步（依赖检查、launchd 装常驻、生成 `_local-fleet.yaml`、角色文件核对、
把会话拉起来）流水线自己干。

### 拓扑与角色（`fleet_up.py`）

机器层装好了，还差**智能层**：开哪些 window、每个 pane 里坐着谁。这两样以前一个字
都没落盘——角色只活在会话历史里，换台机器得到的是一堆失忆的 Claude。现在它们是文件：

| 文件 | 是什么 | 进版本库 |
|---|---|---|
| `fleet.example.yaml` | workOS 本体的拓扑模板，无真实名字 | ✅ |
| `_local-fleet.yaml` | 本机真实拓扑 | ❌ `_local-*` |
| `roles/<name>.md` | 角色定义，通用版 | ✅ |
| `_local-roles/<name>.md` | 角色定义，本机版，**优先于** `roles/` | ❌ `_local-*` |

同 `TRIAGE.md` / `_local-TRIAGE.md` 的规矩，没有新概念。

```bash
./fleet_up.py up            # 照配置建 tmux（已存在的 session 跳过，不覆盖）
./fleet_up.py up --dry-run  # 只打印要跑的 tmux 命令
./fleet_up.py check         # 配置 vs 现实，对账
./fleet_up.py doctor        # 只读自检
./fleet_up.py capture -s OS # 给已有 session 逆向存档
```

**范围红线：这份配置只描述 workOS 本体**——主会话、desk、值班、采集器运维、遥控，
这些不管你手上是什么项目都要有的基础设施。具体项目的会话随项目生灭，不属于这里。
所以 `capture` 强制要求点名 session，不给「一把梭全抓」的默认行为：全抓出来的是
你今天的工作现场，不是这套系统本身。

角色是靠 `claude "$(cat roles/xxx.md)"` 喂进去的——把提示词当**首条消息**交给 claude
自己，而不是先起 claude 再 `send-keys`，后者要靠猜「启动好了没」。

> 踩过的坑：detached 建 session 不给 `-x/-y`，tmux 按 80x24 算，`main-vertical` 的
> 主 pane 默认就要 80 列，**其余 pane 被压成 1 列宽**（实测 78x24 / 1x12 / 1x11）。
> 命令照常送达执行，只是显示成一列一个字，看上去像「没送进去」；attach 上来也不会
> 自动重排。`build_window()` 现在按当前 client 尺寸建，没 client 就用 204x60。
> 另外定位 pane 一律用 pane-id（`%NN`），不用 `session:window.index`——window 名会被
> hook 加状态图标、随时会指错地方（§4 也是这么定的）。

细节：采集器 [README.md](README.md)、遥控 [DTCC.md](DTCC.md)、
hook 结构 [settings.example.json](settings.example.json)、
picker 键位在 claude-tmux-sessions 的 install.sh（默认 `prefix+g`）。

### 定时与常驻（`fleet_up.py services`）

以前这些是一条条手工 `launchctl` 装进去的，仓库零记录——换机器不会自动有，
改过什么也无从查起。现在同样是声明：`services.example.yaml` / `_local-services.yaml`。

```bash
./fleet_up.py services status      # 装没装 + plist 内容跟声明对不对得上
./fleet_up.py services install     # 全装
./fleet_up.py services install console
./fleet_up.py services uninstall <名字>
```

`status` 不只看「装没装」，还比对 plist 内容与声明是否一致——**装了但内容对不上，
比压根没装更难查**。

两个字段值得记住：

- `owner:` —— 这条由别的东西安装（比如采集器三件套是 `./run.sh install` 装的），
  本层只登记、不接管。**重复安装会变成两份进程轮同一份 token、写同一份
  `state.json`**，表现是「采集变慢、疑似停了」，根因却是装了两遍。
- `env:` 不写会自动补 `PATH`/`HOME`。launchd 给的 PATH 是最小集，
  不补的话 homebrew 装的东西一律 `command not found`，而且是**静默失败**。

> 踩过的坑：plist 里写相对路径无声失效。launchd 的工作目录是 `/`，
> `./data/x.out` 被解析成 `/data/x.out`，写不进去、进程照常起、日志一个字没有，
> 看上去像「定时任务根本没跑」。现在 `cwd`/`log`/`cmd[0]` 一律转绝对路径
> （相对路径按仓库目录解析）。

### 控制台（`console.py`）

```bash
python3 console.py --open          # 生成并打开
python3 console.py --loop 10       # 常驻重生成（已登记为 console 服务，默认就在跑）
```

`data/console/index.html`，静态文件，浏览器开个标签页常驻即可。六个区块：
会话与角色、服务、拓扑对账、自检、采集器、事件流。**它把「声明」和「现实」
摆在一起，不一致的地方直接标出来**——以前定位一个问题要人肉跑四五条命令再自己对账。

跟 board 的分工别混：`board_html.py` 面向**消息**（我该处理什么事、谁在等我），
`console.py` 面向**系统**（这套东西自己跑得对不对）。

---

## 8. 抽象成与消息来源无关的编排框架（方向）

这套东西的核心是通用的多 Agent 工作编排，不是钉钉专用。
`dws` 是钉钉**官方公开** CLI；真正不能外传的是**具体的消息内容和联系人**，
而那些本来就在版本库外（`.gitignore` 挡掉 `data/` 和 `config.json`）。

**关键设计：把「消息来源」抽象成一个接口，dws 只是其中一个适配器。**
钉钉 / 飞书 / Slack / 邮件……都只是「一个消息源」，实现同一个接口即可插拔。
这一步同时解决通用性和隐私：框架层零公司信息，来源适配器可换。

```
消息源接口 (MessageSource)
  ├── dws 适配器      （钉钉，示例实现）
  ├── feishu 适配器    （待写）
  └── slack 适配器     （待写）
        ↓ 归一化后进
采集 → 路由 → 会话 → hook → 心跳/唤醒/board   ← 这整套跟来源无关，就是可分享的核心
```

**可复用的三块**：① 编排框架（采集/路由/投递/心跳/唤醒/board）；
② 消息源接口 + dws 适配器示例；③ claude-tmux-sessions（通用会话层，最干净，可先单独发）。

**打包前的清扫清单**（都是去掉「某个人的具体工作数据」，不是去掉工具）：

- [ ] 抽出 `MessageSource` 接口，把 dws 相关代码收进 `sources/dws/`，主流程不 import 具体来源。
- [ ] 代码注释 / README 里的真人名、真项目名换占位。
- [ ] `config.example.json` 只留结构，不留任何真实群名 / 人名 / id。
- [ ] 个人化的巡检口径留在 `_local-TRIAGE.md`（已 gitignore），仓库里那份保持通用。
- [ ] 保留 `.gitignore` 对 `data/` 和 `config.json` 的屏蔽（个人数据永不进包）。
- [ ] 提交前跑一遍自查：真人名 / 公司名 / 群名 / profile 凭据串 / 20 位以上 id，必须零命中。

**这是个真重构（抽接口 + 清洗），不是一晚上的事**——不急，先把自用版跑顺，
想发的时候按这张清单走。
