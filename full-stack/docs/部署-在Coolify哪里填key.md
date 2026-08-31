# 在 Coolify 哪里填这些 key

给小朵的。看这一份就够，不用去碰服务器上的文件。

## 先说结论：**不用找 `.env` 文件**

`env.example` 是给别人自建时看的模板。你的小窝跑在 Coolify 里，
**Coolify 自己就是那个 `.env`**——你在它界面上填的每一条环境变量，
起容器的时候会原样注入进去。仓库里那个 `.env` 你从来就没有过，也不需要有。

🔴 而且**千万别**真在服务器上建一个 `.env` 提交进仓库——chatnest 是公开的。

## 路径

Coolify → 你的项目 → **chatnest 这个应用** → 左边（有的版本在顶部）
那一栏里的 **Environment Variables / 环境变量**。

进去之后有两种填法：

- **一条一条加**：`Add` → 左边填名字（`ELEVENLABS_API_KEY`），右边填值 → 保存。
- **整段粘贴**（快得多）：那一页上有个切换叫 **Developer view**（有的版本叫
  Bulk / 批量编辑）。打开之后是一个大文本框，可以直接把下面整段贴进去，
  一次填完。

## 直接贴这段，然后把等号后面补上

```
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
HERVOICE_ENABLED=1
GROQ_API_KEY=
LLM_API_KEY=
```

五条。`HERVOICE_ENABLED=1` 已经填好了，剩下四条你去下面四个地方拿。

### 1. `ELEVENLABS_API_KEY` —— 我说话要用的

elevenlabs.io 登录 → 右上角头像 → **API Keys** → 新建一个 → 复制。
（有的版本在 Profile / Settings 里，找 "API Key" 就对了。）

### 2. `ELEVENLABS_VOICE_ID` —— 你捏的那个声音的编号

elevenlabs.io → **Voices** → 点开你捏的那个 → 详情里有一串
`Voice ID`（二十来位的字母数字），复制它。**不是**声音的名字。

> 如果你捏声音的时候选的不是 multilingual v2，还要加一条
> `ELEVENLABS_MODEL_ID=`，填对应的 model id。不确定就先不填——
> 填错的话报错里会直接写是哪一项不对，不会闷着不出声。

### 3. `GROQ_API_KEY` —— 听你说话的转写

console.groq.com 注册（免费额度够用）→ **API Keys** → Create → 复制。

### 4. `LLM_API_KEY` —— 判断你说话语气的那个模型

默认走 DeepSeek（便宜）：platform.deepseek.com → **API keys** → 创建 → 复制。

> 想换别家也行，任何 OpenAI 兼容接口都可以，再加两条
> `LLM_BASE_URL=` 和 `LLM_MODEL=` 指过去。

## 填完之后

Coolify 里**改环境变量本身就会触发一次重新部署**。但这次不一样：

🔴 **这次必须是 Deploy / Redeploy，不能是 Restart。**
Dockerfile 改了（加了 anno 的 npm 依赖、pymupdf、ebooklib、ffmpeg），
Restart 只是把老镜像重新跑一遍，新装的东西一样都不会有——
「一起看书」点开会是 502，麦克风按下去没反应。

所以：**Deploy** 那个按钮，让它重新 build。第一次会慢一些（要装 ffmpeg 和
Node 依赖），后面就快了。

## 一条都不填会怎样

不会崩。这是设计好的：

| 没填 | 结果 |
|---|---|
| `ELEVENLABS_*` | 语音条整个不显示，聊天一切照旧，也不会教我「用声音说」 |
| `HERVOICE_ENABLED` / `GROQ_API_KEY` / `LLM_API_KEY` | 麦克风按下去提示「没开启」，打字照旧 |

所以你可以先只填 ElevenLabs 那两条，听见我说话之后再回来填另外两条。
不用一次填齐。

## 别做的事

- **不要把 key 贴给我。** 我不需要看见它，看见了也只是多一份泄漏面。
  填完你说一句「填好了」就行。
- **不要提交进仓库。** 仓库是公开的。
- key 万一贴错了地方（比如发到聊天里、截图里），去对应平台**吊销重发**一个，
  比改哪儿都快。
