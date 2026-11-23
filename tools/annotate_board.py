#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 render_gerber.py 出的顶面图加分区块标注。

分工：
    render_gerber.py  只负责「把 Gerber 如实画出来」，不带任何主观信息
    annotate_board.py 只负责「在真实渲染图上叠说明」，不改变底图

这样的好处是底图永远可以单独拿去核对 —— 标注层坏了不影响对底图的信任。

坐标来源：pcb-alcohol/placement.json（30 个器件的封装中心与本体尺寸）。
分区框是人工归组，写在 BLOCKS 里，每一项都对应 place.py 里的器件位号，
不是凭空画的。

用法：
    python tools/annotate_board.py
    python tools/annotate_board.py --ppi 30 --pad 1.0
"""

from __future__ import annotations

import argparse
import glob
import json
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def load_font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# 功能分区：(序号, 标题, 副标题, 覆盖位号列表)
# 覆盖位号只用来算包围框，所以框的位置完全由 placement.json 决定。
# ---------------------------------------------------------------------------
BLOCKS = [
    ("1", "Type-C 5V 输入", "J1 + C1 去耦", ["J1", "C1"]),
    ("2", "酒精传感器", "MQ-3 加热+分压", ["MQ1", "R1"]),
    ("3", "三档基准分压", "R5~R10 三组", ["R5", "R6", "R7", "R8", "R9", "R10"]),
    ("4", "三路电压比较", "2×LM393（四比较器只用三路）", ["U1", "U2", "C2", "C3"]),
    ("5", "蜂鸣器驱动", "S8050 + 基极限流", ["Q1", "R14", "R15"]),
    ("6", "蜂鸣器", "BZ1", ["BZ1"]),
    ("7", "三档指示", "D1 低 / D2 中 / D3 高", ["D1", "D2", "D3", "R2", "R3", "R4"]),
    ("8", "对外接口", "XH2.54-5P：5V/GND/三档", ["J2"]),
]

# 标注配色：跟绿板反差最大的一组，同时保证印刷体上也能看清
C_BOX = (255, 214, 64)
C_BOX_SOFT = (255, 214, 64, 46)
C_TEXT = (28, 24, 8)
C_BADGE = (255, 152, 0)
C_BADGE_TXT = (255, 255, 255)
C_PANEL = (24, 26, 30)
C_PANEL_TXT = (232, 232, 228)
C_PANEL_DIM = (150, 152, 148)


def board_bbox(gerber_dir: str):
    """从板框文件取 AABB。跟 render_gerber.py 用的是同一份输入，保证对齐。"""
    import sys
    sys.path.insert(0, HERE)
    from render_gerber import GerberLayer, find_file, LAYER_ALIASES

    p = find_file(gerber_dir, LAYER_ALIASES["outline"])
    if not p:
        raise SystemExit("找不到板框层 GKO")
    bb = GerberLayer(p).bbox()
    if not bb:
        raise SystemExit("板框层没有图元")
    return p, bb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gerber", default=os.path.join(ROOT, "pcb-alcohol", "gerber"))
    ap.add_argument("--shots", default=os.path.join(ROOT, "pcb-alcohol", "shots"))
    ap.add_argument("--placement", default=os.path.join(ROOT, "pcb-alcohol", "placement.json"))
    ap.add_argument("--ppi", type=float, default=30.0)
    ap.add_argument("--pad", type=float, default=1.0)
    ap.add_argument("--src", default="01-top.png")
    ap.add_argument("--dst", default="05-annotated.png")
    args = ap.parse_args()

    src = os.path.join(args.shots, args.src)
    if not os.path.exists(src):
        raise SystemExit(f"底图不存在：{src}（先跑 render_gerber.py）")
    base = Image.open(src).convert("RGB")

    _, bbox = board_bbox(args.gerber)
    x0, y0, x1, y1 = bbox
    ppm = args.ppi
    pad = args.pad

    def T(x: float, y: float) -> tuple[float, float]:
        """mm → 底图像素（Y 向上转 Y 向下）。"""
        return ((x - (x0 - pad)) * ppm, ((y1 + pad) - y) * ppm)

    place = json.load(open(args.placement, encoding="utf-8"))

    W, H = base.size
    rows = (len(BLOCKS) + 2) // 3
    legend_h = 46 + rows * 52 + 46

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    f_badge = load_font(20)
    f_title = load_font(17)
    f_sub = load_font(14)

    for i, (num, title, sub, refs) in enumerate(BLOCKS):
        # 用引用到的器件的封装框求并集，再外扩 12px
        pts = []
        for r in refs:
            if r not in place:
                continue
            it = place[r]
            cx, cy, w, h = it["x"], it["y"], it["w"], it["h"]
            pts.append((T(cx - w / 2, cy + h / 2), T(cx + w / 2, cy - h / 2)))
        if not pts:
            continue
        bx0 = min(p[0][0] for p in pts) - 12
        by0 = min(p[0][1] for p in pts) - 12
        bx1 = max(p[1][0] for p in pts) + 12
        by1 = max(p[1][1] for p in pts) + 12
        bx0 = max(2, bx0); by0 = max(2, by0)
        bx1 = min(W - 2, bx1); by1 = min(H - 2, by1)

        d.rounded_rectangle([bx0, by0, bx1, by1], radius=8,
                            outline=C_BOX, width=3, fill=C_BOX_SOFT)

        # 序号徽章放在框左上角外侧，尽量不压元件
        bs = 26
        cx_b = bx0 + bs / 2
        cy_b = by0 + bs / 2
        d.ellipse([cx_b - bs / 2, cy_b - bs / 2, cx_b + bs / 2, cy_b + bs / 2],
                  fill=C_BADGE)
        tw = d.textlength(num, font=f_badge)
        d.text((cx_b - tw / 2, cy_b - 12), num, font=f_badge, fill=C_BADGE_TXT)

    board = Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")
    canvas = Image.new("RGB", (W, H + legend_h), C_PANEL)
    canvas.paste(board, (0, 0))

    # ---- 底部图例 ----
    d2 = ImageDraw.Draw(canvas)
    y = H + 16
    d2.text((36, y), "功能分区", font=load_font(20), fill=C_PANEL_TXT)
    y += 30
    col_w = W // 3
    for i, (num, title, sub, refs) in enumerate(BLOCKS):
        cx = 36 + (i % 3) * col_w
        cy = y + (i // 3) * 52
        d2.ellipse([cx, cy, cx + 22, cy + 22], fill=C_BADGE)
        tw = d2.textlength(num, font=load_font(15))
        d2.text((cx + 11 - tw / 2, cy + 3), num, font=load_font(15), fill=C_BADGE_TXT)
        d2.text((cx + 32, cy - 1), title, font=f_title, fill=C_PANEL_TXT)
        d2.text((cx + 32, cy + 22), sub, font=f_sub, fill=C_PANEL_DIM)

    note = ("底图由 tools/render_gerber.py 从 gerber/ 目录的 RS-274X 与 Excellon 文件直接光栅化，"
            "未打开任何 EDA 软件；分区框位置取自 placement.json 的封装坐标。")
    d2.text((36, H + legend_h - 40), note, font=load_font(13), fill=C_PANEL_DIM)

    out = os.path.join(args.shots, args.dst)
    canvas.save(out, optimize=True)
    print(f"{out}  {canvas.size[0]}×{canvas.size[1]}  {os.path.getsize(out)//1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
