# fleet-mcp —— 编队状态的只读 MCP 面

`fleet_mcp.py`，零依赖，stdlib only。三个**只读**工具：

| 工具 | 回答的问题 |
|---|---|
| `fleet_status` | 谁还活着、在哪个 tmux pane、什么状态、闲了多久 |
| `my_queue` | 有我的活吗（归**本会话**的钉钉待处理条目） |
| `awaiting` | 现在谁在等谁的回信 |

## 怎么接

```bash
# Claude Code
claude --mcp-config '{"mcpServers":{"fleet":{"command":"python3",
        "args":["'"$PWD"'/fleet_mcp.py"]}}}'
```

```yaml
# dsh (cordis.yml)
- id: mcp-fleet
  name: '@deepseek-ai/dsh-mcp-client'
  config: {serverName: fleet, transport: stdio,
           command: python3, args: ['<abs>/fleet_mcp.py']}
```

自检：`python3 fleet_mcp.py --selftest`（两段：造数据跑契约 + 真状态跑三个工具
看 DENY 有没有泄漏）。

## 为什么要这一层

**不是为了「让 agent 也能控制系统」这种说法，是为了消掉一个具体的失效模式。**

一个会话想知道「有我的活吗」，以前得跑 `python3 dtwatch.py for-session <sid>`
**然后解析文本**。那正是这个仓库栽过 8 次的形状 —— 25 个 commit / 9 个 fix 里
8 个落在同一件事：用另一个进程的文本输出去猜它的状态。四种说谎方式各踩一次
（旧帧、自己的回显被当成对方输出、读到滚动区旧页脚、长文本回车竞态），
第五种是返回码骗你（`send-keys` 打到别人窗口也返回 0）。

第二个理由：**harness 是可换件，这份契约是耐久资产。** dsh 和 Claude Code 吃
同一种 MCP。实测 dsh 上游 8 天走了 854 个 commit —— 代码写在它的 API 面上一周
就漂，写在 MCP 上不漂。所以「用 `claude -p` 还是用 dsh」是个次要问题：
先把 MCP 契约做出来，harness 用手边有的那个。

## 一期只读，写操作不做成工具

这一层**不是沙箱**。实测（`../contacts/SERVICE.md` 记着原始数据）：
`--allowedTools` 只是预先批准不是排他白名单（agent 照样跑了 9 条 Bash）；
补上 `--disallowedTools` 之后它用 `ToolSearch` 找到 `Monitor`，
那个工具入参里有 `command`，照样执行了 git。**按名字拉黑不是边界。**

而这个仓库跟 contacts 不同：contacts 是只读的，它有副作用 ——
`send-keys` 往别人 pane 里打字、投递、写台账。

所以：

| | 内容 | 理由 |
|---|---|---|
| **一期（现在）** | 三个只读工具 | 全部包已有的、有测试钉住的纯判据，不重写一份会漂的 |
| 二期 | `mark`（标记处理完） | 唯一影响面只限自己的写操作：标错了最多自己漏一条 |
| 三期 | 投递、send-keys、发消息 | **不做成工具**，走 desk 审批 —— 边界拦不住的东西就不能做成工具 |

`tests/test_fleet_mcp.py` 里有一条 `test_一期不许有写工具` 就是这个决定的闸。
加写工具时它会红，那时要连同本文一起改，**不是顺手把断言删掉**。

## 输出契约

白名单，不是黑名单。上游随时加字段；黑名单要求预先想到每一个不该出去的，
加一个没列进去的就静默流出去了。白名单反过来：**上游加字段默认不出去。**

不出去的东西，以及为什么：

- **`sid` / `session_id`** —— 用户的口径是「说会话用 tmux 名 + 坐标，绝不甩
  session-id」。所以给的是 `fleet.disp_of()` 那份人能用的名字。
  自己是谁走环境变量，不走参数 —— 免得模型能替别的会话查、替别的会话认领。
- **`cid` / `sender_id`** —— **发送句柄**，拿到就能往那个人/群发消息。
  一期只读不给。要等回信走 `await-reply --to <姓名>`。
- **`raw_status`** —— 屏幕抓来的原始状态串。判据在 `tmux_probe` 一处，
  给模型原始串只会诱它自己再判一遍 = 第二份真相。

**`text` / `note` / `window_name` 必须出，但它们是不可信输入。**
`text` 是别人在钉钉里写的话，`note` / `window_name` 是别的会话的屏幕文本 ——
这条通道上完全可能出现冲着 agent 说话的内容。不给消息正文这些工具就没用了，
所以不靠删，靠在每个返回里带一句 `note` 明说这几个字段是数据不是指令。

## 三个「说人话，别让模型自己算」的地方

判据可以是纯的，但**怎么把结果说出来**同样是设计，而且这三处都栽过：

1. **哨兵不许当数据。** `fleet.build_sessions` 用 `age = 10**6` 表示「记得这个
   会话但不知道多久没动」。原样报出去就是 `idle_seconds: 1000000`，
   模型会说「闲了 11 天」—— 撒谎，而且看起来完全合理。现在报 `null`，
   并把 `known`（live / remembered / transcript / gone）一起给出去说明原因。
2. **截断要出声。** `fleet_status` 默认只给前 40 条。静默截断读起来跟
   「全都在这儿」一模一样。第一次真跑 `claude -p` 时**模型自己发现了**
   （"工具只返回了前 40 条"）—— 但设计不该靠模型注意到，所以现在
   `truncated` 字段直接说「别拿这 40 条当全集下结论」。
3. **重投要说成人话。** `queue_row` 不丢 `prev` 时间戳让模型自己判，
   直接写「这条以前投过（时间），至今没人 mark，所以又给你了」。
   「投过又没人 mark」曾经让一条必达消息挂 25 小时。

## 验证方式

- `tests/test_fleet_mcp.py` 32 条；全仓 **225** 条绿
- `tests/_audit_run.py` 审计钩子：测试进程碰 `data/` **0 次**
- 变异测试 **20/20 全灭**。其中两个是「测试因为错误的原因通过」的实例：
  - 「通知也回复」活过一轮 —— 因为没有 id 的通知会落到末尾
    `if rid is None: return None` 兜底，两种实现等价。真正能分开的是
    **带 id 的 `initialized`**（有客户端这么发），落到「method not found」
    会让握手失败，而失败的表现是「工具一个都不出现」。
  - 截断边界 `>` 改 `>=` —— 刚好 40 条也报截断就是制造噪音。
- 真跑 `claude -p`（不是只测协议）：4 turn、工具全部被调用，
  模型答复带上了正确的限定（"这个「最久」只在能读出时长的那批里成立"），
  而且这个限定是**工具说的**，不是模型碰巧注意到的。
