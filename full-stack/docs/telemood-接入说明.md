# telemood 接入说明（TG 那条线的气泡 / 表情 / 贴纸 / 按钮）

对应《施工单-telemood接入TG.md》。上游 https://github.com/beniedev/telemood
`0.1.0`，commit `2bfd4b667428f637b375c022240fbeb299b69fa5`。

---

## 装在哪儿

| 位置 | 是什么 |
|---|---|
| `full-stack/telemood/` | **vendor 进来的上游包，一个字符没改。**它不在 PyPI 上，装不了。零运行时依赖，所以 `requirements.txt` 一行都不用加 |
| `full-stack/tests/` | 上游原样搬来的 53 个合成测试 |
| `full-stack/app/telemood_bridge.py` | **我们自己写的那部分**：四个 host callable、四路结果映射、计划解析与回退、贴纸闭环、按钮消费与过期清理 |
| `full-stack/test_telemood_bridge.py` | 宿主侧的 51 个离线测试 |

改动过的宿主文件：`app/telegram.py`、`app/claude.py`、`docker-entrypoint.sh`、
`Dockerfile`、`env.example`。**`full-stack/static/` 一个字节都没动。**

## 它没有做的事（这些是上游的核心约定）

- 不新建第二个 Telegram 客户端，不起第二个 update loop。token、轮询、重连
  生命周期还是 `telegram.py` 一个人持有。
- **不读、不复制、不落盘 bot token。** 注入给 telemood 的只有 `_api` 这一个
  callable。bot namespace 是 `getMe` 拿到的数字 id，不是 token。
- 不在 adapter 里 `asyncio.run()`，不造 event-loop bridge。走的是
  `AsyncInjectedTelegramAdapter` + `AsyncInteractionKernel`。
- 不把 `catalog.list(...)` 的原始行交给模型（里面有可复用的 `file_id`）。
  给它的是 `list_sticker_model_views(...)` / `StickerModelEvent`。
- 不创建、不修改贴纸包。包由她自己在 Telegram 的 Sticker Editor 里管。

## 三处硬拦路石怎么处理的

施工单第 2 节写死的那三条，**没有一条是绕过去的**：

| # | 原来 | 现在 |
|---|---|---|
| 2.1 `allowed_updates` 写死 `["message"]` | reaction / callback 服务端根本不推 | `telemood_bridge.allowed_updates()` 按开着的能力拼。**全关时返回的就是 `["message"]`**，跟改动前一模一样 |
| 2.2 `_handle_update` 丢掉贴纸 | 静默丢弃 | `_handle_message` 收下、下载静态贴纸、进 catalog。**第二期没开时仍然按原样丢弃**——开关关着却改行为是另一种意外 |
| 2.3 `_allowed()` 只认 `message.chat.id` | 非 message 类 update 全丢 | `_update_chat_id()` 按四种类型各自取 chat_id，取不到就丢。**没有用 `return True`**；`test_telemood_bridge.py` 里有 9 条用例盯着这一段 |

## 四个开关（施工单 2.4 的分期）

四种能力**代码全在，一样没砍**。但不许一次全开：

| 期 | 环境变量 | 开了会怎样 |
|---|---|---|
| 一 | `TELEMOOD_ENABLED=1` | bubble。他一次回复能分成几条，节奏他自己排。不动 update 订阅 |
| 二 | `TELEMOOD_STICKER=1` | 收她的贴纸进 catalog，他能挑一张发回去 |
| 三 | `TELEMOOD_REACTION=1` | 订阅 `message_reaction` / `message_reaction_count`；他能给她那条点表情 |
| 四 | `TELEMOOD_CHOICES=1` | 订阅 `callback_query`；按钮点了**有反应** |

总开关关着 = 跟接入之前完全一样：订阅不变、回复走 `_split_for_tg`、模型也
不会被教计划格式（省下那几十个 token）。

## 成本

- 格式说明进**稳定前缀**（`build_system_prompt(lean=True)` 拼的，不是
  `telegram_prompt.md`），而且**按开着的能力拼**——只教当期真能用的动作。
  放用户侧每轮都要重付一遍，那正是 `TG_MAX_TURNS=3` 要省的钱。
- 贴纸目录只能挂**用户侧**（它会变），所以封了顶：`TELEMOOD_STICKER_LIST_MAX`
  默认 12 条，约 700 字符。
- 她点一个表情**不会**单独触发一次模型调用。攒着（最多 3 条）下一轮一起带过去。

## 回退

模型没按格式输出、或者计划一条都没发出去时，她**照样收到消息**：

- 抠不出 JSON / 解析失败 → 按纯文本走 `_split_for_tg` 发原文。
- 解析成功但一条都没发出去 → 发**所有 bubble 的正文**，不是那坨 JSON。
- 发了一半停住 → 已发的不重发（UNCERTAIN 可能已经到了，重发就是发两遍），
  补一句「后面还有半句没发出去」。

## 没验证的 / 没做的

**照 AGENTS.md 第 1 条如实列。**

1. **全部没上真机。** 51 + 53 个测试全过、`check_adapter(mode="async").ok`
   为真，但那是**静态形状检查和合成数据**，不等于真的能发出去。施工单第 7 节
   验收的 13 条里，**1–12 条需要她在真机上各走一遍**，第 13 条本身就是「真机验」。
   AGENTS.md 第 2 条：云端说没问题不算数。
2. **reaction 要不要 bot 管理员权限，没验证。** 私聊通常不需要，但没实测过。
   不成的话 `setMessageReaction` 会被拒 → 映射成 FAILED（不是假装成功），
   日志里看得到。
3. **`message_reaction_count` 在私聊里大概率永远收不到**（Telegram 的匿名聚合
   一般只给群）。订阅和 normalizer 都接好了，收不到是 Telegram 的行为，
   不是我们这边砍的。
4. **动图 / 视频贴纸他看不见图**，只拿到 metadata（上游的 model view 会自己
   写明「image content not attached」）。静态 `.webp` 会下载到
   `/data/uploads/telegram/`，他能用 Read 看。
5. **按钮过期清理跨重启是尽力而为**：待清理的 `{chat_id, message_id, 到期时间}`
   落在 `/data/telemood/pending_markup.json`，重启会接回来。但**过期与否的唯一
   权威是 callback store**——界面没清干净，过期点击一样 fail closed。
6. **让他每条回复都输出 JSON，可能影响他说话的质感。** 这是施工单要求的形态
   （telemood 的计划就是 JSON），但 TG 那条线是很私人的闲聊。第一期开起来之后
   她要是觉得他说话变生硬了，回退成本是 `TELEMOOD_ENABLED=0` 一个变量。
7. **格式说明写在代码里，不在 `telegram_prompt.md`。** 施工单第 4.2 节说写进
   那个文件；没照做，理由是它必须**按开关拼**——telemood 关着却教了他计划格式，
   她会当场收到一坨 JSON。协议跟解析器放在一起才不会漂。它仍然在**稳定前缀**里，
   4.2 节要的两件事（尽量短、进稳定前缀）都满足了。
   `telegram_prompt.md` 一个字没动，她那份人设副本不受影响。

## 怎么跑测试

    cd full-stack
    python -m unittest discover -s tests        # 上游 53 个，vendor 副本没坏
    python -m unittest test_telemood_bridge     # 宿主侧 51 个
