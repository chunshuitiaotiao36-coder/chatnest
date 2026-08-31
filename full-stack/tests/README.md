# 上游 telemood 的合成测试，原样搬来的

跟 `../telemood/` 同一个 commit（`2bfd4b6…`，见 `../telemood/UPSTREAM.md`）。
只用合成数据，不碰网络、不碰 Telegram、不碰 /data。

    cd full-stack && python -m unittest discover -s tests

53 个应该全过。**换 vendor 副本之后必须重跑这一条**——它是「搬进来的这份没坏」
的唯一凭据。

宿主接线那一侧的测试在 `../test_telemood_bridge.py`。
