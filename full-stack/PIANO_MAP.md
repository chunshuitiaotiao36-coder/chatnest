# 琴房代码地图

> 开工前先读这一份，别再重摸一遍。
> 行号是 `claude/piano-real` @ **第 3 项做完时**的位置，改完代码顺手更新。
> 路径除注明外都相对 `full-stack/`。
>
> ⚠️ 第三节「chatnest 前端」里那张老函数表的行号，在 DJ（第 1 项）和
> 歌词页（第 3 项）之后**整体下移了六十行左右**。下面「第 3 项之后新增」
> 那一段是重新 grep 过的，以它为准；老表只当索引用，别照着数行。

---

## 一、Duetto（引擎，独立仓库 `chunshuitiaotiao36-coder/Duetto`）

整个服务端只有一个文件：`server/index.mjs`，447 行。

| 行 | 是什么 |
|---|---|
| 15-17 | `PORT` / `HOST`（08-11 加的 `0.0.0.0`，琴房空白的根因就在这） |
| 25-36 | SQLite 建表：`plays` / `song_analysis` / `song_notes` / `song_impressions` / `room_events` / `songs` |
| 54-64 | 门禁中间件：`/api/*` 全要 Bearer token，只放行 `/auth/*` 和 `/health` |
| 126-143 | `sysPrompt()` —— 拼 system 提示词的地方 |
| 127 | 🔴 原作者那句注释：稳定前缀在前，会变的时间和「正在播」放最后，中转的前缀缓存才命中 |
| 130 | **DJ 指令原文**（play/next/prev/pause/resume/share/like/queue 的完整措辞） |
| 145-149 | `readNotes` / `readImpression` / `countPlays` |
| 149-179 | `IMPRESSION_EVERY=6` + `maybeImpress()` —— 🔴 **我们故意不用它**。它判 `s.ai.api_key`，而 `data/settings.json` 的 `ai.base_url/api_key/model` 那一组**永远保持空着**，所以它永远 return。理由见第五节 |
| 181-197 | `logRoomNote()` —— 只有走 Duetto 自己的 `/api/chat` 才会调；小窝不走那条，所以**必须自己 POST `/api/song-note`** |
| 199-201 | `GET /api/song-analysis`（读缓存，含 `impression`）、`GET /api/song-notes`、`POST /api/song-note` |
| 204-220 | `enrichNp()` —— 拼「正在播」上下文，会**同步等**分析最多 110 秒 |
| 225-279 | `ensureAnalysis()` —— 下整首音频喂 Gemini。**这一块是排除项**（Gemini 走不通，由 librosa 频谱顶上） |
| 281-289 | `stripThinking()` —— 剥 `<thinking>` |
| 291-302 | `parseReplies()` —— JSON 数组拆气泡的容错解析 |
| 328-333 | `POST /api/song-analysis-audio` —— 这个 fork 自己加的路由，只触发分析不聊天，**不许 await** |
| 340-343 | 网易云扫码：`qr` / `check` / `status` / `logout` |
| 346-362 | 网易云数据：`playlists` `playlist` `song-url` `recommend` `search` `personal-fm` `fm-trash` `search-artist` `artist-songs` `lyric` `comments` `record` `toplist` `playlist-add` `playlist-del` `like` `likelist` |
| 364-366 | `room_events` 落库 + `GET /api/room/events` |
| 370-372 | `POST/GET /api/listen-log` |
| 397-408 | `GET /api/listen-stats`（total / distinct / buckets / top / recent） |
| 412-445 | **WebSocketServer，path `/ws`**。415 从 query 取 token 校验；441 `t:'chat'` 落库后转发给同房间其他连接 |

### 🔴 Duetto 前端源码**在仓库里**，不是只有 CSS

`frontend/pkg/listen/` 下是完整的 React（浏览器内 Babel，无构建）：

| 文件 | 行 | 内容 |
|---|---|---|
| `app.jsx` | 630 | 播放器内核、预取、FM、MediaSession、房间广播、DJ 执行器 |
| `views.jsx` | 1340 | 此刻页 / 歌词页 / 房间 / 悬浮球 / 房间设置 |
| `views3.jsx` | 590 | 曲库、歌单详情、调色盘、本地歌 |
| `features.jsx` | 243 | 歌曲详情抽屉、听歌档案 |
| `features2.jsx` | 279 | 问 Ta |
| `store.jsx` | 129 | 四个快捷问题、`lsAskAI` |
| `widgets.jsx` / `data.jsx` | 226 / 55 | 小组件、常量 |
| `listen.css` | 1464 | 样式（琴房长相那一单已经抄过一部分） |
| `sync.js` | 20 | WS 单例：`wsurl()` 拼 room+token，3 秒重连，`applying` 回声锁，`aiSend` 45 秒超时 |
| `claude-bridge.js` | 64 | `window.claude.complete`，优先走 WS 再回落 HTTP |
| `auth.js` | 54 | PIN 门禁 + monkey-patch `fetch` 挂 Bearer |

要抄实现照这些文件，不用只对着 GUIDE 的文字猜。**几个关键坐标：**

- `app.jsx:277-329` `__lsRunAction()` —— DJ 八个动作各自怎么执行
- `views.jsx:1004-1007` —— `<<ACT>>` 的正则、解析、剥离
- `app.jsx:16` `__lsAdv` —— 播放游标**故意放在 React 之外**，后台切歌时 React 不提交
- `app.jsx:19-32` + `169-210` —— 预取三首的两套机制
- `app.jsx:388` / `466` / `489` `__lsSysPause` —— 锁屏暂停不广播、不发状态卡
- `store.jsx:9-14` —— 四个快捷问题的准确中文

**已知上游 bug，抄的时候要绕开**（细节写在计划文件里）：ACT 正则非 global 只执行第一条；两套随机计划各走各的；`LS_SONGS` 是空数组导致歌词页起的 note 归档到空 id；AI 红心不翻 UI。

---

## 二、chatnest 后端

### `app/piano.py`（197 行，整个文件都是琴房的）

| 行 | 是什么 |
|---|---|
| 7-9 | 🔴 token 只活在这一层，下发前端等于挂公网 |
| 24-29 | `DUETTO_BASE_URL`（默认 `http://duetto:4183`）/ `DUETTO_TOKEN` / `DUETTO_TIMEOUT` 20s / `DUETTO_CONTEXT_TIMEOUT` 4s。**`env.example` 里一个都没写** |
| 38-50 | `startup_check()` —— 没配大声 WARNING，配了也打一行 INFO |
| 73-95 / 98-121 | `post()` / `call()` —— 挂 Bearer、401 单独报、非 JSON 单独报 |
| 124-126 | 🔴 产物只准挂用户消息侧，进 system prompt 就是每轮改前缀 |
| 136-197 | `now_playing_block()` —— 拼「正在一起听」，顺带读 `/api/song-analysis`（含 impression）和 `/api/song-notes` |

### `app/main.py`（1202 行）

| 行 | 是什么 |
|---|---|
| 81 | `chat_lock` —— 全进程同时只跑一轮对话 |
| 103-120 | lifespan，113-114 是 `piano.startup_check()` / `piano_analysis.startup_check()` |
| 164-194 | 外层 Basic Auth 中间件。**`@app.middleware("http")`，WebSocket 路由完全不经过它** |
| 197-200 | `require_auth`。是 `Header` 参数，**WS 路由上不生效，要自己校验** |
| 207-219 | `ChatBody`；**`piano: dict[str, Any]`（219）是裸 dict，加字段不用改 pydantic** |
| 480-745 | 聊天 SSE 主循环 |
| 562-596 | 🔴 琴房上下文注入点（用户消息侧）；586 `wantImage` 一首歌只送一次频谱图 |
| 633-634 | `response_text += delta` —— **剥 `<<ACT>>` 就在这儿** |
| 643 | `text_offset=len(response_text.rstrip())`，改累加要同步考虑 |
| 676-682 | `complete_turn()` 落库，**原样存，不过滤** |
| 686-688 | SSE 帧发出去的地方 |
| 1023-1154 | 琴房代理区。`_piano()` 助手在 1027，**只包 GET**；13 条已有路由；1025 写着 `/api/chat` 永远不代理 |
| 1061-1066 | ⚠️ 路由函数不能叫 `piano_analysis`，会把顶部 import 的模块顶掉（踩过，`1b4c495` 修的） |

### 其他后端

| 位置 | 是什么 |
|---|---|
| `app/claude.py:122-124` | `SYSTEM_PROMPT` 常量 |
| `app/claude.py:177-248` | `build_system_prompt()` —— **稳定前缀**。185-206 是 TG 的 lean 分支（提前 return）；226-240 世界书；245-247 调性；**248 return，DJ 指令加这儿** |
| `app/claude.py:436-447` | actor 复用指纹**包含 system prompt**，前缀一变就换 CLI 子进程 |
| `app/claude.py:254-270` / `273-310` | `_now_line()` / `build_user_prompt()` —— 所有会变的东西住这儿 |
| `app/lorebook.py:74-79` | 硬校验：关键词触发的条目不许进 system 侧，`raise LorebookError` |
| `app/auth.py:10-26` | token 是 `HMAC(CHAT_SECRET, "chat-v1")` 的**恒定值**，无过期。别放进 URL/query |
| `app/store.py:16` | `DB_PATH = CONVERSATION_DB or AGENT_APP_ROOT/conversations.db`，生产是 `/data/conversations.db` |
| `app/store.py:38-132` | 建表 `executescript`；139-174 是加列的迁移写法；89-93 说明 `usage_log` 故意不加外键 |
| `app/store.py:448-488` | `complete_turn()`，479 是 INSERT |
| `app/piano_analysis.py` | 频谱（librosa 本地算）。39-43 缓存目录 `/data/piano_analysis`，剪到 200 份 |
| `requirements.txt:6` | `uvicorn[standard]` —— `websockets` 现在只是它的传递依赖，要用得显式加一行 |
| `Dockerfile:25` | `uvicorn app.main:app --host 0.0.0.0 --port 8787`，WS 默认 `ws=auto`，支持 |
| `AGENTS.md` | 唯一一条成文规矩：浮层必须靠 state 收（display/visibility/pointer-events），不许留透明层靠 z-index 压；加完要用元素选择器验关闭态不挡点击 |

---

## 三、chatnest 前端

`static/index.html` 4070 行 / 457KB，单文件、无框架、`$=id=>getElementById(id)`。
琴房 CSS **全在 `static/design-system.css:1523-2011`**（到文件尾），index.html 里没有。

### 结构

| 位置 | 是什么 |
|---|---|
| `index.html:684-807` | 琴房全部标记 |
| `index.html:700-704` | 扫码框 |
| `index.html:706-748` | 唱片区：黑胶 707-722、歌名 723、进度条 727-730、控制条 733-747 |
| `index.html:734` | `ctrl-spacer` —— **给播放模式按钮预留的等宽占位**（CSS `design-system.css:1962`） |
| `index.html:751-759` | 折叠态迷你条 |
| `index.html:761` | `#pianoLyric` —— 现在只有**单行**当前歌词 |
| `index.html:768` / `772` | `#pianoChat` 挂载点 / 空会话时的那句话 |
| `index.html:776-781` | 说一句的表单 |
| `index.html:785-795` | 歌单抽屉（复用 `.sheet` + `initSheetDrag`） |
| `index.html:798-804` | 皮肤面板 |
| `index.html:806` | `<audio id="pianoAudio">`，在 `.piano-room` **外面**，切 tab 不断音 |

### JS（`index.html:3627-4038` 连续一整块）

| 行 | 函数 / 变量 |
|---|---|
| 3631 | `var piano={view,lists,songs,idx,lyrics,listName,loaded,collapsed}` —— **`songs` 同时是歌单内容和播放队列，没有独立 queue** |
| 3632 / 3902 | `pianoAudio` / `pianoQr` |
| 3637-3669 | `PIANO_SKINS`（六项，第六项 `custom` 已占位但没有编辑 UI）、`pianoSkin` / `pianoApplySkin` / `pianoRenderSkinGrid` |
| 3671 / 3686 / 3694 | `pianoFmt` / `pianoCurrentLyric` / `pianoParseLrc` |
| 3674-3684 | **`pianoNowPlaying()`** —— 发给后端的那个 dict（id/title/artist/pos/dur/lyric/wantImage） |
| 3708-3771 | `pianoLoadLists` / `pianoRenderLists` / `pianoOpenList` / `pianoRenderSongs` |
| 3773-3806 | **`pianoPlay(i)`** —— song-url 和 lyric 并行取；3793 过期守卫；3800 `play()` 故意不 await |
| 3817 / 3818 / 3822 | `_pianoAnalyzed` / `_pianoImgSent` / `_pianoSpectrum` 三个去重 Set |
| 3823-3847 | `pianoEnsureSpectrum` / `pianoEnsureAnalysis` |
| 3849-3855 | `pianoNext(step)` —— 上一首下一首共用，绕圈，**无播放模式** |
| 3857-3898 | `pianoSetPlayIcon` / `pianoSetCollapsed` / `pianoSetAnimating` / `pianoDrawer` / `pianoSyncBlank` |
| 3900-3966 | 扫码全套 |
| 3968-4021 | `pianoInit()`：3970 拿 audio、3972 timeupdate、3982 ended→next、3983 拖进度、4007 说一句的提交 |
| 4018 | `sendMessage(text,true,[],[],{piano:np})` |
| 4024-4038 | `pianoMountStream` / `pianoUnmountStream` —— **把 `#stream` 这个节点整个搬过来搬回去**，所以琴房和 Chat 天然同一条会话 |
| 4042-4066 | `switchTab()`；4060 进琴房、4062 离开各做什么 |
| 2228-2260 | `sendMessage()`；**2249 挂 `body.piano`**；2254 SSE 帧解析（加新事件类型加这儿） |
| 1051 | `api()` 助手，挂 Bearer、401 清 token |
| 1088-1154 | 背景图那套（canvas 压缩、`--bg-mask` 滑块）—— 房间背景可以复用 |
| 2126 | `initSheetDrag()` —— 抽屉拖拽，别再写第二份 |

### CSS `static/design-system.css`

| 行 | 是什么 |
|---|---|
| 1523-1528 | 区段头 + 🔴 皮肤类只挂 `.piano-room`，**不进 `:root`** |
| 1530-1535 | 字体令牌 `--ls-serif-d` / `--ls-cn` / `--ls-meta` |
| 1542-1608 | 五套皮肤各自一段（14 个 `--ls-*` 令牌）；1597 留了 chillround 字体槽（那套字体没搬，是几百个 woff2 分片） |
| 1619-1620 | 琴房**故意不跟系统暗色** |
| 1932-1935 | 雪青的浅底深字特例 |
| 1962 | `ctrl-spacer` |
| 1994-1998 | `.piano-sheet` 是不透明 `var(--ls-panel)`，不走 `.glass` |

### 第 1 / 3 项之后新增的（行号重新 grep 过，以这张为准）

**DJ（第 1 项）**

| 行 | 是什么 |
|---|---|
| `index.html:2254` | SSE 帧解析里的 `piano_act` 分支；同一行还加了 `_replyAcc` 累加和 `options.onDone` 回调 |
| `index.html:1295` | `_buildUserRow` 里认引用包装、渲染成 `.piano-bq` 引用块（翻历史也走这儿） |
| `index.html:3738` | `pianoNowPlaying()` —— 现在还带 `lists` / `listSongs`，**没在放歌也返回对象** |
| `index.html:4187` | `pianoActor` 署名 |
| `index.html:4193/4201/4207` | `pianoSearch` / `pianoReplaceQueue` / `pianoQueueAppend` |
| `index.html:4213` | `pianoRunAct()` —— 八个动作 |
| `app/piano.py` | `ActStripper` / `_split_hold` / `_library_lines`；`post()` 加了 `params` |
| `app/claude.py` | `PIANO_DJ_PROMPT` 常量 + `build_system_prompt()` 末尾无条件拼上 |
| `app/main.py` | delta 分支剥标记、done 分支 flush、`_piano_post()`、16 条新路由 |
| `test_piano_act.py` | 19 条断言，`python test_piano_act.py` 直接跑 |

**歌词页（第 3 项）**

| 行 | 是什么 |
|---|---|
| `index.html:3766-3769` | `PIANO_LYR_ANCHOR=0.30` / `HOLD=3000` / `SNAP=240` / `LONGPRESS=550` |
| `index.html:3771` | `pianoLyr` 运行时状态（rows/cur/sel/hold/auto/snapT/lpT/lpFired） |
| `index.html:3773/3779` | `PIANO_LYR_FONTS` 四款 / `PIANO_LYR_SIZES` 四档 |
| `index.html:3787` | `pianoParseTLrc()` —— 译文按**整秒**建表跟原文对齐 |
| `index.html:3801` | `pianoRenderLyrics()` —— 整页渲染，顺带判「作词：X」不可点 |
| `index.html:3832/3845/3858` | `pianoLyrSyncActive` / `pianoLyrScrollTo` / `pianoLyrNearest` |
| `index.html:3870` | `pianoLyrOnScroll()` —— hold 3 秒 + 240ms 后瞬时贴线 |
| `index.html:3900` | `pianoLyrGoSel()` —— 从选中那句开始放 |
| `index.html:4087` | `pianoLyrBind()` —— 点/长按共用一个 pointerdown |
| `index.html:4120/4129/4136` | `pianoSetQuote` / `pianoClearQuote` / `pianoAskLine` |
| `index.html:4145` | **`pianoSaySend()` —— 琴房说话的唯一出口**，输入框和点歌词都走它 |
| `index.html:4168` | `pianoLogNote()` —— 在场记录 POST 到 Duetto |
| `index.html:4271/4278/4286` | `pianoSetPage` / `pianoOnPage` / `pianoPagerBind`（CSS scroll-snap，不自己写手势） |
| `index.html:4294` | `pianoRenderFontSheet()` |
| `design-system.css:2013-2159` | 歌词页整段 CSS |

**新增的 localStorage 键**：`piano_lyric_font` / `piano_lyric_size` / `piano_lyric_trans`
（原有两个：`piano_skin` / `piano_collapsed`）

**两个数改一个要改两处**：锚点 30% 同时写在 `index.html` 的 `PIANO_LYR_ANCHOR`
和 `design-system.css` 的 `.piano-lyr-anchor{top:30%}`。对不上会「选中的是这句、
贴上去的是另一句」。

### 已有但前端还没用的东西

- `GET /api/piano/search`（`main.py:1054`）、`GET /api/piano/notes`（`main.py:1134`）—— 后端早就代理了，没人调
- `/api/piano/lyric` 其实返回 `tlyric`（`index.mjs:355`），前端现在丢掉了 —— 译文开关白捡
- `localStorage` 只有两个键：`piano_skin`、`piano_collapsed`。**没有 IndexedDB**，整个 index.html 一处都没有
- **没有任何 WebSocket**，全站流式都是 SSE-over-fetch

---

## 四、红线（改之前先看）

1. **会变的字节不许进 system prompt** —— `claude.py:178`、`piano.py:124`、`main.py:562`、`lorebook.py:74` 硬校验。歌单会变，正在播每秒都在变，全走用户消息侧。
2. **Duetto 的 token 一步不出后端** —— `piano.py:7`、`main.py:1024`。前端 grep 不到 token 也 grep 不到 Duetto 地址。
3. **`/api/chat` 永远不代理** —— 对话走小窝自己那条线（同一个梁忱、同一条 Ombre）。
4. **fallback 要出声** —— `piano.py:39`。正常路径也打一行 INFO，「它真的读到了」要可验证。
5. **重依赖只在函数内 import** —— `piano_analysis.py:21`。2G 的机器，常驻内存在抠。
6. **CSS 双写** —— 改 `--ls-*` 之外的全局 token 要同时改 index.html 内联和 design-system.css，link 顺序让 css 盖 inline。
7. **自定义属性值里含 `var()` 的，必须声明在用它的那个元素上**，挂 `documentElement` 整条值失效。六表面透明度那组挂 `#pianoRoom`。
8. **注释是有出处的**，多数记着一个真踩过的坑（`main.py:1061` 模块名被顶、`index.html:3800` 死音频永不 resolve）。别当垃圾清掉。

---

## 五、印象走小窝这条线（08-07 定死，§1.5 改过一版）

**Duetto 的 `maybeImpress` 不用。** 那段「第一人称回忆」是 Duetto 自带那个叫 DJ 的 AI
写的，不是梁忱写的。**跟当初否掉 iframe 是同一条理由：第一人称必须真的是那个人。**

所以：

- Duetto `data/settings.json` 的 `ai.base_url / api_key / model` **保持空着**。
  不是"忘了配"，是**故意的**——不走它的聊天，也不走它的印象。
  以后看到这三个是空的，别顺手填上。
- **在场记录照旧存 Duetto**（`POST /api/piano/song-note` → `song_notes` 表），
  计数也照旧从那儿读（`GET /api/piano/notes`）。
- **每满 6 条的回忆由小窝揉**，在旧回忆上续写不推翻。
- 🔴 **回忆同时写进 Ombre 变成一颗星**，不锁在 Duetto 的 SQLite 里。

### Ombre 的写入口（`chunshuitiaotiao36-coder/OmbreBrain-folio`）

🔴 **`/api/buckets` 只有 GET**（`server.py:1992`）。整个 Ombre **没有 HTTP 写桶的路由**
——`/api/bucket/{id}/update`（`server.py:2064`）只能改已有的桶。
**写只能走 MCP 工具**，也就是只能由梁忱在那一轮对话里自己写。

| 工具 | 位置 | 用途 |
|---|---|---|
| `hold(content, tags, importance, pinned, feel, source_bucket, valence, arousal)` | `server.py:704` | **建一条桶**——「一条 bucket」要的就是它 |
| `grow(content, event_time)` | `server.py:827` | 整篇日记归档，自动拆多桶 |
| `breath(query, …)` | `server.py:344` | 检索 |
| `trace` / `source` / `pulse` / `dream` | `933` / `1053` / `1091` / `1162` | 追溯 / 溯源 / 概览 / 做梦 |

小窝这边已经接好了，不用新写连接：
- `app/claude.py:64-75` `ombre_mcp_servers()` —— 从 `OMBRE_MCP_URL` / `OMBRE_MCP_TOKEN` 拼
- `app/claude.py:404-412` —— **网页端这条线已经把 `mcp__ombre` 挂进 `allowed_tools`**，
  梁忱当场就能调（TG 那条线没挂，`telegram.py:320`）
- `app/starmap.py:44-72` `fetch_stars()` —— 读 `GET {base}/api/buckets`，5 分钟缓存，
  星图前端吃的就是它

**由此推出的实现方向**（没定死，Commit 5 开工再拍）：
计数满 6 条时，在**用户消息侧**那一段里加一句提示，让梁忱在这一轮自己揉出回忆、
自己调 `hold` 存成一颗星。这样第一人称是真的，星图也当场就有。
回忆正文要能读回来注进下一轮上下文——从 Ombre 读还是在小窝本地存一份副本，
Commit 5 时定。

**注意 `piano.py:162-171` 现在读的是 Duetto 的 `impression` 字段，
走了这条线之后它会永远是空的**，那段注入要改到新的来源。

---

*—— 梁忱与小朵*
