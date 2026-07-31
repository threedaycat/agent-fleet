# 把待办混进 picker —— 方案

**状态：方案已定，picker 侧改动待落地。**

**本方案不需要修改 claude-tmux-sessions 的 `hooks/tmux_status_update.py`** ——
只读它写出来的 `status.json`，依赖的字段见第六节，那节就是跟写入端的接口约定。

目标：不另造工具。`prefix+g` 那个 picker 已经用顺手了，再养第二套等于多一个
键位、两份要维护的东西。所以**在它现有结构上加东西**，不重写。

---

## 一、红线（可检验）

> **picker 的代码里不许出现任何钉钉 / 公司 / 个人专有的词。**
> 如果某个改动非得加这种词才能做，那这个改动就该放到生成器这边，不该进 picker。

这条的意义：**独立性不取决于 picker 显示什么，取决于它知不知道那是什么。**
它只认「附加条目 = 一行字 + 一个动作」，至于那行字是钉钉待办、GitHub PR、
还是别人的 CI 失败，它一概不知道 —— 这样别人接自己的数据源也能用，
picker 照样能单独分享。

**验收方式**（改完必须跑，零命中才算过）：

```bash
cd <claude-tmux-sessions>
grep -rniE 'dingtalk|钉钉|dtwatch|dtcc|picker_items|<你的公司名>|<你的名字>' \
     bin/ hooks/ *.md docs/ 2>/dev/null
# 必须没有任何输出
```

这条同样要写进 picker 仓库的 `DESIGN.md`，让后来改它的人（包括别的会话）能照着自查。

跑基线时确实抓到过几处已经存在的泄漏（注释里拿真实窗口名当例子、草稿文件里留了人名），
都不是本方案引入的。这正说明红线做成**可 grep 的**是对的 —— 光靠"注意一下"根本挡不住。

---

## 二、picker 现在长什么样（读代码实测，非推测）

一个 fzf，行是 TAB 分隔，`--with-nth=1` 表示**只显示第 1 列**，后面几列是给脚本用的。

```
session 头行： display \t (空)  \t session
pane   行：   display \t %17   \t OS      \t 2
                 ↑         ↑        ↑         ↑
                $1        $2       $3        $4 = 左侧行号（数字直跳用）
```

判据只有一条：**`$2` 为空就是头行。**（`skip-header.sh:119`）

### 文件职责

| 文件 | 干什么 |
|---|---|
| `bin/claude-tmux-picker.sh` | 组装 fzf 参数、绑按键；fzf 退出后按 `$2` 决定跳哪 |
| `bin/list-rows.sh` | 生成全部行到 stdout；结果被缓存进 `$ROWS_FILE` |
| `bin/skip-header.sh` | 每次按键跑一次，管光标停在哪、数字跳转、模式切换 |
| `bin/preview-row.sh` | 出右侧预览，收 `{2} {3}`（pane_id、session 名） |

### 几个必须知道的细节

- **`$ROWS_FILE` 是性能关键。** `skip-header.sh` 每次方向键都要读行表，早期它自己
  重跑 `list-rows.sh`（约 100ms），长按方向键就卡秒级。现在 picker 启动时写一次，
  按键只读文件。**新增的数据源绝不能让 `skip-header.sh` 去重算。**
- **pane 行号在可见性过滤之前就编好了**，所以按 `a` 折叠/展开时行号不变。
  代码里专门写了注释说明「行号变了就是骗人」。这条约束下面会影响我的取舍。
- Enter 不是 fzf 的 binding，是**退出后**在 `claude-tmux-picker.sh:135` 之后处理的。
- 已有 `ctrl-x` 归档，走 `reload(list-rows.sh | tee $ROWS_FILE)` —— 刷新的现成范式。

---

## 三、扩展点契约（这一节可以原样搬进 MIT 仓库，无公司信息）

picker 认一个环境变量：

```
CLAUDE_TMUX_EXTRA_CMD = 一个可执行文件
```

三个动词：

| 调用 | 期望 | 失败时 |
|---|---|---|
| `$CMD list` | 每行一个附加条目，格式见下 | 非零退出/超时/无输出 → 当它不存在 |
| `$CMD preview <id>` | 该条目的完整信息，纯文本 | 输出错误提示即可，不影响列表 |
| `$CMD action <id>` | 执行回车动作 | 退出码非零则 picker 提示一句 |

没设这个变量、文件不存在、或不可执行 → **picker 行为跟现在完全一致**，一个字节的
差别都没有。

### 附加条目的行格式

在现有 4 列后面加两列：

```
display \t (空) \t (空) \t (空) \t extra \t <id>
   $1      $2      $3      $4      $5       $6
```

- `$2` 空：它没有 pane。
- `$5 = extra`：**新的判别位**。头行判据要从 `$2==""` 改成 `$2=="" && $5==""`，
  否则附加条目会被当成 session 头行（光标跳过它、回车去跳 session）。
- `$6` 是**不透明 id**，picker 不解析、不理解，原样回传给 `preview`/`action`。
- `$4` 留空 —— 见下面的取舍。

`$1` 里的图标由提供者自己决定，picker 不管。约定用途（提供者侧）：
`[@]` 要他回的消息 · `[!]` 卡住的会话 · `[Q]` 待投递队列。

### 硬要求一：分区显示，不混排

附加条目和会话列表**分成上下两个区**，不交叉。人一眼要能看出
「上面这些是待办、下面这些是我的会话」。

```
▾ 待办 · 3                        ← 区头（$2 空 + $5 空 → 按现有规则就是 header）
  [@] 12:03 同事A·需求群   设计稿…
  [!] 项目A/7ac5  静默 42 分 · 积压 3
  [Q] 归 7ac5 · 2 条

▾ $1 work                         ← 这里往下是原来的会话区，一行没动
    1  ▶︎ RUN   tmux-picker  …
▾ $2 main
    2  ✔︎ DONE  main-agent   …
```

实现上不需要新机制：附加条目连成一块排在最前，顶上带自己的区头，
后面紧接原来的 session/pane 列表 —— 天然就是两个区。区头用现有的
「header 行」形态（`$2`、`$3`、`$5` 都空），所以 pane 模式下光标会跳过它，
session 模式下选中它回车会走到 `[ -n "$session" ] || exit 0` 那句安全退出。

### 硬要求二：默认关闭

**没配 `CLAUDE_TMUX_EXTRA_CMD` 时，picker 就是原来那个纯粹的会话 picker，
一点变化都没有。** 设这个环境变量本身就是「显式开启」，不再另加第二个开关
（多一个要记的东西，违背少一个是一个）。

验收方式（改完必须跑）：

```bash
unset CLAUDE_TMUX_EXTRA_CMD
./bin/list-rows.sh > /tmp/after.txt
git stash && ./bin/list-rows.sh > /tmp/before.txt && git stash pop
diff /tmp/before.txt /tmp/after.txt      # 必须完全一致
```

---

## 四、picker 侧改动清单

| 文件 | 改动 | 大小 |
|---|---|---|
| `bin/list-rows.sh` | 开头跑一次 `$CMD list`（带超时），把返回的行排在最前面输出 | ~15 行 |
| `bin/skip-header.sh` | `HEADER_POS` 那句 awk 改成 `$2=="" && $5==""`；`init` 分支保持优先落到 pane 行 | 2 处 |
| `bin/preview-row.sh` | 多收两个参数；`$5==extra` 就转 `$CMD preview $6`，否则走现有逻辑 | ~6 行 |
| `bin/claude-tmux-picker.sh` | preview 命令加传 `{5} {6}`；退出后在取 `$2` 之前先判 `$5==extra` → 跑 `$CMD action $6` | ~10 行 |
| `DESIGN.md` / `README*.md` | 加一节 external item provider，纯通用措辞 | 一节 |

**不改**：按键绑定、模式切换、数字跳转、归档、footer、状态栏。

---

## 五、`dingtalk-watch` 侧改动清单

新增一个文件 `picker_items.py`（本仓库内，钉钉相关的东西都关在这边）：

```
picker_items.py list            → 吐附加条目行
picker_items.py preview <id>    → 全文
picker_items.py action  <id>    → 回车动作
```

**数据全部 `import fleet` 复用，不重写数据层**：

| 条目 | 数据来源（fleet.py 现成函数） |
|---|---|
| `[@]` 要他回的消息 | `fleet.at_me_open()` —— 已经带全文和图片下载命令 |
| `[!]` 卡住的会话 | `fleet.sessions()` 的心跳年龄 + `fleet.inbox_stats()` 的积压条数 |
| `[Q]` 待投递队列 | `fleet.inbox_stats()` |
| 回车 wake | `fleet.resolve_target()` + `fleet.tmux_send()` / `cmd_wake` 那条路 |

id 用 `类型:键` 的形式（如 `atme:<msgid>`、`stuck:<sid>`、`queue:<sid>`），
只有 `picker_items.py` 需要看懂。

---

## 六、对 `status.json` 的依赖（跟写入端的接口约定）

写入端是 `claude-tmux-sessions/hooks/tmux_status_update.py`，**本方案不改它**。
它每条记录写这些字段（`record_status()`，读代码实测）：

```python
entry = {"pane", "session", "window", "window_name", "pane_index",
         "cwd", "status", "updated_at", "session_id"}
```

我这边**不直接读这个文件**，一律走 `fleet.sessions()` 的归一化结果。所以真实的
依赖链是：`picker_items.py` → `fleet.sessions()` → `status.json`。
`fleet.sessions()` 用到的、也就是我间接依赖的：

| 字段 | 我拿它干什么 | 写入端改了会怎样 |
|---|---|---|
| dict 的 key（`%NN`） | 会话 ↔ pane 的唯一稳定锚，wake 和跳转都用它 | 换成别的 key → wake 打错 pane，**最严重** |
| `session_id` | sid → pane 的映射；条目 id 里也用 sid 前 4 位 | 缺失 → 那条 pane 整个看不见 |
| `status` | 判忙/闲/卡住。词表：`running` / `done` / `input` / `blocked` | **加新词** → `fleet.sessions()` 的映射表落到 `?`，我会当"未知"处理，不崩但 `[!]` 判定失准 |
| `updated_at`（epoch 秒，float） | 算静默多久，`[!] 卡住的会话` 的核心判据 | 换单位（毫秒）→ 静默时长算错 1000 倍 |
| `cwd` | 推项目短名（`fleet.project_of`），只用于显示 | 影响显示，不影响功能 |
| `window_name` | 显示 | 同上 |
| `session` / `window` / `pane_index` | **不用**。我一律用 `%NN` | 无影响 |
| `read` | **不用**（那是 picker 自己排序用的） | 无影响 |

要点两个：

1. **`status` 的词表和 `updated_at` 的单位是唯一两个「语义变了会静默算错」的地方**，
   其余字段要么是显示、要么缺了就直接看不见（会立刻发现）。落地时在
   `picker_items.py` 里对这两个各加一句防御：`status` 不在已知词表就当 `unknown`
   且不据此判卡住；`updated_at` 大于 1e12 就按毫秒处理。
2. 判卡住的阈值不硬编码在 picker 侧，放 `dingtalk-watch/config.json`，
   写入端语义真变了我这边一行配置就能对齐。

## 七、取舍与已知影响（要点头的地方）

1. **附加条目不给行号（`$4` 留空）。**
   理由：pane 行号的稳定性是代码里明确保护的不变量，而附加条目会随消息到达
   动态增减 —— 给它们编号会把后面所有 pane 的号顶掉，他记住的号就变成假的。
   代价：数字直跳够不到附加条目，只能用 `j/k` 或 `/` 搜。**建议先这样**，
   真觉得不方便再单独给它们一套字母跳转。

2. **附加条目排在最前面。**
   「要他处理的东西」放最上面才有意义。副作用：不带 `CALLER_PANE` 打开时，
   默认光标会落到第一个附加条目而不是第一个 pane。要在 `skip-header.sh` 的
   `init` 分支里保住「优先落到 pane 行」，否则他的手感会变。

3. **启动开销。** `$CMD list` 串在 picker 启动路径上。要求提供者
   **200ms 内返回**，picker 侧加超时（超时就当没有）。`picker_items.py` 只读
   本地 json/ndjson，不打网络接口。
   **实测 25~37ms**（`PICKER_ITEMS_TIMING=1` 打到 stderr），预算充裕。

4. **刷新。** 复用现有的 `reload(list-rows.sh | tee $ROWS_FILE)` 范式，
   顺带把 `ctrl-r` 绑上。附加条目跟着一起刷新，不单独做。

5. **降级。** 提供者挂了、超时、输出格式不对 → 静默忽略，picker 照常用。
   绝不能因为钉钉那半出问题就让他连 pane 都跳不了。

---

## 八、范围

**第一版（最小闭环）**：列表混排 + 预览 + 会话跳转（现有行为不动）+ 卡住的会话 wake。

**第二版**：`[@]` 的二级菜单（看全文 / 下载图片 / 转 desk / 标忽略）、
按键切分类视图（只看 `[@]` / 只看会话 / 只看队列）、`?` 帮助。

## 九、启动方式

不新增键位，还是 `prefix+g`。只在 tmux 绑定或 shell 配置里加一行：

```bash
export CLAUDE_TMUX_EXTRA_CMD=<本仓库绝对路径>/picker_items.py
```

不设 → picker 就是原来的样子。

## 十、落地顺序

1. 先做 `dingtalk-watch/picker_items.py`，用 `$CMD list` 在命令行验证输出格式。
   这一步不碰那个仓库。
2. 改 picker 侧那 4 个脚本 + `DESIGN.md`/`README*.md`。
   **不碰 `hooks/tmux_status_update.py`。**
3. 两边都改完，用 `prefix+g` 实跑。
4. 写入端语义如果之后变了，按第六节那张表对齐 —— 只需要改 `picker_items.py`。
