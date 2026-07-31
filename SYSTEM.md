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
# 1. 代码
git clone <本仓库>
git clone <claude-tmux-sessions>           # TODO：补仓库地址。通用会话层，装它的 hook + picker

# 2. dws（钉钉官方公开的命令行）
npm i -g dingtalk-workspace-cli
dws auth login                             # 扫码授权，管理员需在 open-dev 开「CLI 访问管理」

# 3. 本机配置（不进版本库）
cp config.example.json config.json         # 填自己的 open_dingtalk_id / user_id / 关注群 / 路由表

# 4. hook：让每个 Claude 会话收尾时报心跳 + 探队列
#    把 settings.example.json 的 hooks 段合并进 ~/.claude/settings.json
#    （claude-tmux-sessions 的 install.sh 管会话状态那半）

# 5. 常驻
./run.sh install 300                       # 采集器上 launchd
# push-loop 不开机自启，remote on 时才起

# 6. 整夜干活（可选）：caffeinate 别让机器睡（插电才生效，合盖仍会睡）
```

细节：采集器 [README.md](README.md)、遥控 [DTCC.md](DTCC.md)、
hook 结构 [settings.example.json](settings.example.json)、
picker 键位在 claude-tmux-sessions 的 install.sh（默认 `prefix+g`）。

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
