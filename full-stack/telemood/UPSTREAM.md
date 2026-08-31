# 这个目录是 vendor 进来的，不是我们写的

上游：https://github.com/beniedev/telemood
版本：`0.1.0`
commit：`2bfd4b667428f637b375c022240fbeb299b69fa5`

🔴 **它不在 PyPI 上**（`pip download telemood` → No matching distribution found），
所以只能 vendor 或者 `pip install git+...@<commit>`。选了 vendor：它**运行时依赖为零**，
搬一个包目录进来就完事，不用带 `pyproject.toml`，构建时也不用连 GitHub。

**这十个 .py 一个字符都没改。** 要对齐上游就整目录换掉，然后重跑
`../tests/`（那也是上游原样搬来的 53 个合成测试）：

    cd full-stack && python -m unittest discover -s tests

它是 0.x，上游明说 API 还会变——升级前先读 CHANGELOG，别直接覆盖。

宿主这一侧的接线全在 `../app/telemood_bridge.py`，那个文件才是我们的代码。
