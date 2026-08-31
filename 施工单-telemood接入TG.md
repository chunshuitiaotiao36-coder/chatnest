# 施工单：给 Telegram 那条线接上 Telemood

写给接手的 CLI。**这是加功能，不是修 bug**，跟上一张导航栏的单子性质不同：
没有需要先测出来的未知量，路径是清楚的。但**她那边有三处硬拦路石写死在代码里**，
决定了四种能力现在哪些能用、哪些必须先改宿主。第 2 节全部写明了，别绕过它们。

上游：https://github.com/beniedev/telemood
版本 `0.1.0`，commit `2bfd4b667428f637b375c022240fbeb299b69fa5`

---

## 0. 它是什么（我已经读完全部源码并跑过测试）

一个**小 Python 库**，让模型能在一次 Telegram 回复里按顺序组合四种东西：

| 能力 | 是什么 |
|---|---|
| bubble | 文字气泡，长文按段落/句子/空白保守拆分 |
| reaction | 对她那条消息发普通 emoji 表情回应 |
| sticker | 把她发来的贴纸收藏进目录，之后能挑一张发回去 |
| choices | 一次性选项按钮（走 Telegram callback_data） |

关键性质，跟接入方式直接相关：

- **它不是第二个 bot，也不跑第二个 update loop。** 它复用宿主已有的
  Telegram 客户端，只负责「解析模型的计划 → 绑定可信上下文 → 按序执行 → 收回执」。
- **Python `>=3.11`，运行时依赖为零。** 她镜像是 `python:3.12-slim`，满足。
- **模型拿不到 chat/user/thread/token/file_id。** 这些一律由宿主绑定，模型只能
  给一个 `telemood.plan.v1` 的 JSON。贴纸也只给不透明的 catalog ID。
- 已实测：仓库里 `python -m unittest discover -s tests` **53 个测试全过**。

**上游作者是按「让 agent 来接」设计的**：README 里直接写了给 agent 的提示词，
`SETUP.zh-CN.md` 要求先出一份只读能力报告再动手，仓库里还带了一个便携 skill
（`skills/telemood/SKILL.md`）。**照它的协议走，别自创流程。**

---

## 1. 🔴 它不在 PyPI 上

```
pip download telemood → No matching distribution found
```

`pip install telemood` 会直接失败，这是第一步就会撞上的墙。两条路：

- **推荐：vendor 进仓库**，跟 `full-stack/anno/` 一个待遇。零运行时依赖让这件事
  很干净——把上游的 `telemood/` 包目录整个搬到 `full-stack/telemood/` 即可，
  `pyproject.toml` 都不用带。搬完记下上游 commit，将来好对齐。
- 或者 Dockerfile 里 `pip install git+https://github.com/beniedev/telemood@2bfd4b6...`
  ——**必须钉 commit**，它是 0.x，API 明说了还会变。这条依赖构建时能连 GitHub。

---

## 2. 🔴 她这边的三处硬拦路石（决定哪些能力现在能用）

全都在 `full-stack/app/telegram.py` 里，我逐行确认过。

### 2.1 `allowed_updates` 写死成 `["message"]`

`_get_updates()`（约 227 行）：

```python
{"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
```

注释写着「编辑过的消息、频道消息、回调按钮第一批一律不处理，让服务端就别发过来」。
后果：

- `message_reaction` / `message_reaction_count` **服务端根本不会发过来**
  → 入站 reaction 用不了。
- `callback_query` **也不会发过来** → **choices 按钮点了没反应**：
  按钮能发出去，但她点一下什么都不会发生。这是最容易做出来一个「看起来能用、
  其实是死的」功能的地方。

### 2.2 `_handle_update()` 把贴纸直接丢掉

约 632 行：

```python
if not text:
    # 语音、贴纸、非图片文件这一批都不处理
    return
```

她发一张贴纸过来，现在是**静默丢弃**。而「用户自制贴纸闭环」的第一步正是
收下这张贴纸、存进 catalog。不改这里，sticker 那条链没有起点。

### 2.3 `_allowed()` 只认 `upd["message"]["chat"]["id"]`

约 350 行。`callback_query` 和 `message_reaction` 这两种 update **顶层没有
`message` 字段**（callback 的消息在 `callback_query.message` 里），所以
`_allowed()` 会返回 False，直接丢掉。

> 🔴 **这是 fail-closed，方向是对的，别用 `return True` 去「修」它。**
> 那个 bot 的名字陌生人能搜到，白名单是她唯一的门。正确改法是**按 update 类型
> 各自取出 chat_id 再比对白名单**，四种类型分别取：
> `message.chat.id` / `callback_query.message.chat.id` /
> `message_reaction.chat.id` / `message_reaction_count.chat.id`。
> 取不到就丢。

### 2.4 由此得出的开通顺序

**照这个顺序做，每一期都能独立验收、独立回滚：**

| 期 | 能力 | 前置 | 宿主要改什么 |
|---|---|---|---|
| 一 | bubble | 无 | 只改回复路径，不碰 update 订阅 |
| 二 | sticker | 2.2 | `_handle_update` 收下贴纸 |
| 三 | reaction | 2.1 + 2.3 | 加订阅 + 白名单按类型取 chat_id；**可能还要 bot 管理员权限** |
| 四 | choices | 2.1 + 2.3 | 同上，外加 callback 消费与过期清理 |

**第一期就能看见效果**（他一次回复能分成几个气泡，节奏是他自己排的），
风险最低。不要一上来四个一起做。

---

## 3. 接入形态（这条别选错）

她的 Telegram 客户端是**手写的 httpx 裸 HTTP**，不是任何 SDK：

- `_api(method, payload)` 约 208 行，打 `https://api.telegram.org/bot{TOKEN}/{method}`
- `_send_message(chat_id, text)` 约 242 行，**故意不传 `parse_mode`**
  （注释：MarkdownV2 漏转义一个字符整条消息 400）
- 整条线是 **async**

所以：

- 用 **`AsyncInjectedTelegramAdapter` + `AsyncInteractionKernel`**，
  四个 facade 方法都是 `async def`。
- 🔴 **不要在 adapter 里 `asyncio.run()` 或造 event-loop bridge**，
  SETUP 第 5 节明说了。她那条线本来就在 event loop 里跑。
- 四个 `host_*` callable 各自把 `_api(...)` 的返回映射成 `InjectedResult`：
  明确接受 → `VERIFIED`，明确拒绝 → `FAILED`，返回形状不对 → `UNKNOWN`，
  超时或副作用不确定 → `UNCERTAIN`。
  🔴 **不能因为 `_api` 没抛异常就报 VERIFIED**——上游反复强调这一点，
  而她的 `_api` 恰好是「失败返回 None」而不是抛异常，最容易误判成成功。

---

## 4. 跟她现有实现会打架的两处

### 4.1 `_split_for_tg()` 和 telemood 的 bubble 展开重复

她自己有一套分段（约 360 行），telemood 绑定阶段也会按「段落、句子、空白、硬切」
展开长 bubble。**两套叠加会切得很碎。** 二选一，建议保留 telemood 那套
（它保证展开后仍与 reaction/sticker/choices 保持相对顺序），把 `_split_for_tg`
从 telemood 这条路径上摘掉——但**别删函数**，非 telemood 的回退路径还要用。

### 4.2 TG 这条线是刻意省钱的，加计划格式会涨 token

`claude.py` 里 `TG_MAX_TURNS` 默认 3，注释写得很清楚：「TG 是闲聊…
🔴 不靠 prompt 写『少翻一点』去约束——模型未必听，花钱的事要硬限制」。
而 telemood 要求模型输出 `telemood.plan.v1` 的 JSON，格式说明要进
`full-stack/telegram_prompt.md`（那是 TG 专用的精简人设）。

**要做的事**：把格式说明写到**尽可能短**，并且放进**稳定前缀**（system prompt）
而不是每轮的用户消息——理由见 `claude.py` 里 `PIANO_DJ_PROMPT` 上面那段注释，
稳定前缀能命中缓存，放用户侧则每轮都要重新付。

**还要留一条退路**：模型没有按格式输出、或者输出解析失败时，
**必须回落成「当成纯文本发出去」**，不能让她收不到消息。
telemood 的解析是 fail-closed 的（未知版本/字段/动作类型一律拒绝），
所以这条回退不是可选项。

---

## 5. 状态落盘（Docker，这条踩过一次）

telemood 有两个 SQLite：`SQLiteStickerCatalog` 和 `SQLiteCallbackStore`。

🔴 **必须落在 `/data`（持久卷）**，不能用 SETUP 里示例的 `state/*.sqlite3`
相对路径——那会落在 `/app`，**容器一重建，她收藏的贴纸和未过期的按钮全没**。

接 anno 那次的教训：路径相关的东西一律走环境变量 + 持久卷，例如
`TELEMOOD_STATE_DIR=/data/telemood`，并在 `docker-entrypoint.sh` 里
`mkdir -p`。**建目录必须在启动时做，不能写在 Dockerfile 里**——构建期在
`/data` 下建的东西会被运行时挂上来的卷整个盖掉，这个坑刚踩过。

---

## 6. 不要做的事

- **不要新建第二个 Telegram 客户端或 update loop。** 她只有一条线，
  token 和轮询归她自己持有，这是 telemood 的核心约定。
- **不要读、复制、落盘 bot token。**
- **不要把 `catalog.list(...)` 的原始行交给模型**——里面有可复用的 provider
  `file_id`。给模型的是 `list_sticker_model_views(...)` 或 `StickerModelEvent`。
- **不要用 `return True` 修 `_allowed()`。** 见 2.3。
- **不要把私人贴纸包名、素材、mood 写进任何提交进公开仓库的地方。**
  chatnest 是公开仓库。
- **不要一次把四种能力全开。** 见 2.4 的分期。

---

## 7. 验收（每一期各自过）

**第一期 bubble**
1. 她在 TG 说一句话，收到的回复能分成几个气泡，顺序正确，没有被切碎成十几条。
2. 模型故意输出一段不合格式的文本时，**她仍然能收到消息**（回退到纯文本）。
3. `check_adapter(adapter, mode="async").ok` 为真。

**第二期 sticker**
4. 她发一张自制包里的贴纸，bot 不再静默丢弃；catalog 里能查到它。
5. 模型挑那个 catalog ID 发回来，她收到的是同一张贴纸。
6. 换个 bot namespace 的 ID 会被拒（fail closed）。

**第三期 reaction**
7. 他能给她那条消息点上表情。
8. 她给他的消息点表情，宿主能收到 `message_reaction`（**要先加订阅，
   而且可能需要管理员权限——不成的话按 SETUP 第 7 节报告能力降级，不要静默跳过**）。

**第四期 choices**
9. 按钮发得出去，**点了有反应**（这是 2.1 那条的真正验收点）。
10. 同一个按钮点第二次无效（one-shot）。
11. 过了 TTL 再点无效，即使界面上按钮还在。

**通用**
12. `python -m unittest discover -s tests` 在 vendor 进来的副本上仍然全过。
13. **真机验**。云端跑通不算数——导航栏那个 bug 就是栽在「云端说没问题、
    真机是坏的」上面。

---

## 8. 文件索引

**上游**（先读 `SETUP.zh-CN.md`，它是权威接入指南；`SETUP.md` 是同一份的英文，
读一份就行，别两份都读）

| 文件 | 干什么 |
|---|---|
| `SETUP.zh-CN.md` | 权威接入指南，9 节 |
| `telemood/contracts.py` | 计划格式、绑定、类型（855 行，最核心） |
| `telemood/adapters.py` | `InjectedTelegramAdapter` / async 版 |
| `telemood/async_kernel.py` | `AsyncInteractionKernel` ← 她要用这个 |
| `telemood/stickers.py` | catalog + 入站规范化 |
| `telemood/callbacks.py` | callback store、TTL、one-shot |
| `telemood/conformance.py` | `check_adapter` 静态检查 |
| `skills/telemood/SKILL.md` | 上游自带的便携 skill |

**宿主**（`full-stack/`）

| 位置 | 为什么相关 |
|---|---|
| `app/telegram.py:208` `_api()` | 唯一的发送出口，adapter 包它 |
| `app/telegram.py:227` `_get_updates()` | `allowed_updates` 写死处，见 2.1 |
| `app/telegram.py:242` `_send_message()` | bubble 落地处 |
| `app/telegram.py:350` `_allowed()` | 白名单，见 2.3 |
| `app/telegram.py:360` `_split_for_tg()` | 跟 telemood 分段重复，见 4.1 |
| `app/telegram.py:632` `_handle_update()` | 入站分流，贴纸在这被丢，见 2.2 |
| `app/telegram.py:445` `_stream_reply()` | 回复流，计划 JSON 要在这之后解析 |
| `telegram_prompt.md` | TG 专用精简人设，格式说明写这儿 |
| `app/claude.py` `TG_MAX_TURNS` | 成本硬限制，见 4.2 |
| `Dockerfile` / `docker-entrypoint.sh` | vendor 与状态目录，见 1、5 |

---

## 9. 开工前先做这一件事

按 `SETUP.zh-CN.md` 第 1 节输出那份**只读能力报告**，把
`Reaction subscriptions`、`Usable now`、`Degraded or missing`、
`Files to modify` 填出来发给她，**再动第一行代码**。

上面第 2 节我已经把答案挖出来大半了，但**你要自己核实一遍**——
她的 `telegram.py` 每天都在变，行号会漂。
