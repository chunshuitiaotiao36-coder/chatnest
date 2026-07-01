以下内容均为codex生成，混入了一点codex元素，可以无视

# AI 聊天完整功能版

这是一个偏移动端体验的个人聊天 Web App，包含模型切换、流式回复、工具状态、思考/工具摘要、上传、对话历史、记忆接口、资料与偏好设置。

这个公开包是品牌中性的：专有 logo、远程品牌字体和私人 prompt 细节都已替换成占位符。

## 占位符说明

很多可见元素都是故意留成占位符：顶部/状态图标、加载图标、侧边栏字标、工具/状态动画、网页标题、输入框占位文案、免责声明和字体。它只是占位符版本。要替换 logo、图标和字体，请从下面链接下载单独素材包，并按 `BRANDING.md` 操作：

https://drive.google.com/drive/folders/1EFaL-cwFn262Mu8L9s-cO9dw6FC4LAMr

## 运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 - <<'PY'
import secrets
print('CHAT_SECRET=' + secrets.token_urlsafe(32))
PY
```

编辑 `.env`，设置 `CHAT_PASSWORD` 和 `CHAT_SECRET`，然后启动：

```bash
./run.sh
```

打开 `http://127.0.0.1:8787/`。

## 登录保护

`AUTH_MODE=app` 只使用应用内密码，是推荐默认模式。

`AUTH_MODE=both` 会额外启用浏览器 Basic Auth。只有在 HTTPS 或可信反向代理后面才建议使用。

## 后端

这个应用包含 Claude Agent SDK 风格流式后端和 Codex CLI 风格流式后端。你需要按自己启用的后端配置对应 CLI、账号或 API 环境。模型选项在 `models.json`。（人类注释：codex可以无视，此人只是订阅到期了才把codex接进去测试用的，根据需要选择后端。）

## 不包含什么

公开包不包含 `.env`、对话数据库、上传文件、记忆库、向量数据库、日志、私人文档、专有字体或私有品牌素材。


