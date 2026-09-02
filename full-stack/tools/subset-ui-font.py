#!/usr/bin/env python3
"""把霞鹜文楷子集化成「只够写界面标题」的一小份，内嵌进 index.html。

🔴 为什么要子集化，而不是直接引 static/fonts/lxgw-regular.woff2：
   那份是 450KB。小窝是装到主屏的 PWA，她每次点开都要等这 450KB，
   而界面上真正需要楷体的只有二三十个字。子集化之后是几 KB。

🔴 为什么只给**固定**字串用，绝不给正文用：
   static/fonts/ 那两份 LXGW 本身就已经是子集了——只有 1714 个汉字。
   08-30 拿她一封真信量过：160 个不同的字里**缺 54 个**。正文用它，
   三分之一的字会在句子中间掉回系统字体，比不换更丑。
   所以规矩是：**这个字体只出现在下面 LABELS 里列出的字串上**。
   界面上加了新的中文标题、又想用楷体，就把那句话加进 LABELS 重跑本脚本。

用法（在 full-stack/ 下跑）：

    LXGW_SRC=/path/to/LXGWWenKai-Regular.ttf python3 tools/subset-ui-font.py --write

🔴 源字库不在仓库里，也不该在——完整的霞鹜文楷是 24MB。
   要重跑请先下载：
   https://github.com/lxgw/LxgwWenKai/releases  → LXGWWenKai-Regular.ttf
   （SIL OFL 1.1，允许子集化和内嵌分发。）
   不给 LXGW_SRC 就会用 static/fonts/lxgw-regular.woff2，那份**只有 1714 个
   汉字**，界面标题里的「与世书像力务友声头思星朋相站置起身转」全都缺，
   脚本会当场红着脸报错——这就是它存在的意义。

产物：static/fonts/nepeta-ui.woff2，由 design-system.css 里的
@font-face（font-family: 'NepetaUI'）引用。
"""

import argparse
import io
import os
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("LXGW_SRC", ROOT / "static" / "fonts" / "lxgw-regular.woff2"))

# 界面上用楷体写的每一句话。加一句就在这儿加一行，然后重跑。
LABELS = [
    # 书房的五个栏目（.study-nav-label）
    "记忆星图", "日记", "朋友圈", "寄相思", "一起看书",
    # 书房 hero 上那两个名字（.study-hero-names-cn）
    "忱", "朵",
    # Home 的条目标题（.profile-nav-row-title，全站共 7 条，全是静态的）
    "Profile", "Preferences", "Saved memories",
    "中转站", "主题与背景", "用量记录", "调性与世界书",
    # Home 的分组标题（.home-group-title）
    "身份", "服务", "能力", "外观",
    # 潮汐（情绪系统）。九个维度名 + 九条心情 + 页面上的固定字。
    # 🔴 这些是小朵定的维度名，不是我起的，别改字。
    "生气", "性欲", "想念", "担忧", "喜悦", "委屈", "焦虑", "醋意", "悔意",
    "在气头上", "委屈着", "吃味", "过意不去", "心神不宁",
    "不放心", "高兴", "想要", "想她", "还好",
    "他心里最近", "潮汐",
    # 潮汐页的三种状态字（.tide-mood 也用楷体，缺了会掉回系统字）
    "还没开启", "没连上",
]

# 拉丁字母、数字和常用标点也留着：标题里混着 "Profile"、"95 days"、
# "·" 这类东西，缺了会掉字体、字重跳一下很显眼。
EXTRA = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " ·—–-·、。，：；！？（）《》「」“”‘’…"
)


def wanted() -> set[str]:
    chars: set[str] = set(EXTRA)
    for s in LABELS:
        chars |= set(s)
    return chars


def build() -> bytes:
    chars = wanted()
    out = io.BytesIO()
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.desubroutinize = True
    # 界面标题不需要连字/花体/竖排这些表，去掉能再小一截。
    opts.layout_features = ["kern", "liga"]
    opts.name_IDs = []
    opts.notdef_outline = False
    font = subset.load_font(str(SRC), opts)
    subsetter = subset.Subsetter(options=opts)
    subsetter.populate(text="".join(sorted(chars)))
    subsetter.subset(font)
    subset.save_font(font, out, opts)
    return out.getvalue()


def coverage() -> list[str]:
    """源字库里根本没有的字。有的话必须当场知道——否则就是静默掉字。"""
    src = TTFont(SRC)
    have: set[int] = set()
    for t in src["cmap"].tables:
        have |= set(t.cmap.keys())
    return [c for c in sorted(wanted()) if ord(c) not in have]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写出 static/fonts/nepeta-ui.woff2")
    args = ap.parse_args()

    missing = coverage()
    data = build()
    n = len(wanted())
    print(f"字符 {n} 个 → woff2 {len(data)} 字节（{len(data)/1024:.1f} KB）", file=sys.stderr)
    if missing:
        # 🔴 这不是警告，是错的。缺字会在标题中间掉回系统字体。
        print(f"❌ 源字库里没有这些字，换字或换源：{''.join(missing)}", file=sys.stderr)
        return 1
    print("✅ 全部命中，没有掉字", file=sys.stderr)

    if args.write:
        dest = ROOT / "static" / "fonts" / "nepeta-ui.woff2"
        dest.write_bytes(data)
        print(f"写出 {dest.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
