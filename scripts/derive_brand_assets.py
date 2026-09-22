#!/usr/bin/env python3
"""从官方 logo 机械派生品牌资产（只做裁切/缩放，不重绘、不改比例）。

输入：logo.png（极算门官方 logo，白底方图：上方波形标记 + JISUMEN + 极算门）
输出：assets/brand/jisumen-mark.png（仅波形标记，自动裁掉白边，供顶栏使用）

用法：python3 scripts/derive_brand_assets.py
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageChops

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "logo.png")
OUT_DIR = os.path.join(REPO, "assets", "brand")
OUT_MARK = os.path.join(OUT_DIR, "jisumen-mark.png")


def crop_mark(src: str) -> Image.Image:
    """取官方 logo 上方波形标记区域：先在垂直方向截取上 45%，再自动裁白边。"""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    top = im.crop((0, 0, w, int(h * 0.45)))
    bg = Image.new("RGB", top.size, (255, 255, 255))
    diff = ImageChops.difference(top, bg)
    bbox = diff.getbbox()
    if not bbox:
        raise SystemExit("未能在 logo.png 中找到非白色内容，放弃派生")
    return top.crop(bbox)


def main() -> int:
    if not os.path.exists(SRC):
        print(f"缺少源文件：{SRC}", file=sys.stderr)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    mark = crop_mark(SRC)
    mark.save(OUT_MARK)
    print(f"已生成 {OUT_MARK}  ({mark.width}x{mark.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
