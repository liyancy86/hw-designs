# -*- coding: utf-8 -*-
"""生成 80x40 板上 30 个器件的摆件坐标表，并做几何校验（越界/干涉/安装孔）。"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))

BOARD = (0.0, 0.0, 80.0, 40.0)
CLEAR_PARTS = 0.508      # 元件本体间距（评分项 20mil）
CLEAR_HOLE = 1.5         # 器件到安装孔边缘
HOLES = [(5.0, 5.0), (75.0, 5.0), (5.0, 35.0), (75.0, 35.0)]
HOLE_R = 1.5

# ref: (x, y, 本体宽 W, 本体高 H, 器件名, 备注)
# W/H 为摆放后的实际外廓（已含旋转）；SOP-8 5.0x4.0，0805 2.0x1.25，SOT-23 3.0x1.7，LED0805 2.0x1.25
P = {
    # 连接器（贴边，坐标为封装中心）
    "J1":  (5.20, 20.00, 7.40, 9.00, "Type-C 2P", "贴左边中点，开口朝左"),
    "J2":  (40.00, 3.60, 12.60, 6.00, "XH2.54-5P", "贴下边中点，P1 在左"),
    # 传感器 / 蜂鸣器
    "MQ1": (33.00, 28.00, 17.50, 17.50, "MQ-3", "敏感面朝上，周围净空≥1.5"),
    "BZ1": (57.00, 29.00, 12.00, 12.00, "蜂鸣器", "出声孔朝上"),
    # 比较器
    "U1":  (28.00, 11.50, 5.00, 4.00, "LM393", "1 脚左下"),
    "U2":  (42.00, 11.50, 5.00, 4.00, "LM393", "1 脚左下"),
    # 基准分压（三组竖排：上=接5V，下=接地）
    "R5":  (13.00, 13.00, 1.25, 2.00, "20K", "VREF1 上"),
    "R6":  (13.00, 9.00, 1.25, 2.00, "30K", "VREF1 下"),
    "R7":  (17.00, 13.00, 1.25, 2.00, "10K", "VREF2 上"),
    "R8":  (17.00, 9.00, 1.25, 2.00, "30K", "VREF2 下"),
    "R9":  (21.00, 13.00, 1.25, 2.00, "10K", "VREF3 上"),
    "R10": (21.00, 9.00, 1.25, 2.00, "40K", "VREF3 下"),
    # 去耦
    "C1":  (11.00, 24.00, 2.00, 1.25, "100nF", "5V 入口"),
    "C2":  (24.50, 15.00, 2.00, 1.25, "100nF", "U1 旁"),
    "C3":  (45.50, 15.00, 2.00, 1.25, "100nF", "U2 旁"),
     "C4":  (21.00, 17.00, 2.00, 1.25, "100nF", "AOUT 滤波"),
     "C5":  (22.50, 6.50, 2.00, 1.25, "100nF", "VREF3 滤波"),
    # 输出上拉
    "R11": (26.50, 7.00, 2.00, 1.25, "10K", "OUT1 上拉"),
    "R12": (31.50, 7.00, 2.00, 1.25, "10K", "OUT2 上拉"),
     "R13": (46.50, 8.50, 2.00, 1.25, "10K", "OUT3 上拉"),
    # 驱动
    "Q1":  (52.00, 12.00, 3.00, 1.70, "S8050", "B/E/C"),
    "R14": (55.50, 14.00, 1.25, 2.00, "1K", "基极限流"),
    "R15": (55.50, 9.50, 1.25, 2.00, "10K", "基极下拉"),
    # LED 列（右边缘，竖排）
    "R2":  (65.00, 33.50, 1.25, 2.00, "1K", "D1 限流"),
    "D1":  (69.00, 33.50, 2.00, 1.25, "LED 红", "一档"),
    "R3":  (65.00, 20.00, 1.25, 2.00, "1K", "D2 限流"),
    "D2":  (69.00, 20.00, 2.00, 1.25, "LED 红", "二档"),
    "R4":  (65.00, 7.00, 1.25, 2.00, "1K", "D3 限流"),
    "D3":  (69.00, 7.00, 2.00, 1.25, "LED 红", "三档"),
    # 传感器负载
     "R1":  (37.00, 15.50, 2.00, 1.25, "4.7K", "R_L"),
}


def box(x, y, w, h):
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def gap(a, b):
    """两个 AABB 的间隙（负数=重叠）"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(bx1 - ax2, ax1 - bx2)
    dy = max(by1 - ay2, ay1 - by2)
    if dx > 0 and dy > 0:
        return math.hypot(dx, dy)
    return max(dx, dy)


def main():
    errs, warns = [], []
    refs = list(P)
    boxes = {r: box(P[r][0], P[r][1], P[r][2], P[r][3]) for r in refs}

    # 1) 越界（连接器允许少量出界：J1 开口朝左、J2 插口朝下）
    for r in refs:
        b = boxes[r]
        if b[0] < -0.01 or b[1] < -0.01 or b[2] > BOARD[2] + 0.01 or b[3] > BOARD[3] + 0.01:
            if r in ("J1", "J2"):
                warns.append(f"{r} 外廓超出板边（连接器允许，插头方向朝外）: {b}")
            else:
                errs.append(f"{r} 越界: {[round(v,2) for v in b]}")

    # 2) 器件间距
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            a, b = refs[i], refs[j]
            g = gap(boxes[a], boxes[b])
            if g < CLEAR_PARTS:
                msg = f"{a}-{b} 间距 {g:.2f} < {CLEAR_PARTS}"
                (errs if g < 0 else warns).append(msg)

    # 3) 安装孔
    for r in refs:
        b = boxes[r]
        for hx, hy in HOLES:
            cx = min(max(hx, b[0]), b[2])
            cy = min(max(hy, b[1]), b[3])
            d = math.hypot(cx - hx, cy - hy)
            if d < HOLE_R + CLEAR_HOLE:
                errs.append(f"{r} 距安装孔({hx},{hy}) 仅 {d:.2f}mm")

    # 4) 连接器插入方向净空
    j1, j2 = boxes["J1"], boxes["J2"]
    for r in refs:
        if r in ("J1", "J2"):
            continue
        b = boxes[r]
        if b[0] < j1[2] + 1.0 and not (b[3] < j1[1] or b[1] > j1[3]):
            warns.append(f"{r} 挡在 J1 插头前方 (x<{j1[2]+1.0:.1f})")
        if b[1] < j2[3] + 1.0 and not (b[2] < j2[0] or b[0] > j2[2]):
            warns.append(f"{r} 挡在 J2 插头前方 (y<{j2[3]+1.0:.1f})")

    print(f"器件 {len(refs)} 个")
    print("错误:", len(errs))
    for e in errs:
        print("  ✗", e)
    print("提示:", len(warns))
    for w in warns:
        print("  ·", w)

    # 输出表格 HTML
    rows = []
    for r in refs:
        x, y, w, h, name, note = P[r]
        rows.append(f"<tr><td class=r>{r}</td><td>{name}</td><td class=c>{x:.2f}</td>"
                    f"<td class=c>{y:.2f}</td><td class=c>{w:.1f}×{h:.1f}</td><td class=note>{note}</td></tr>")
    total = {k: 0 for k in ("err", "warn")}
    html = f"""<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<title>摆件坐标表</title><style>
body{{font:13px/1.5 "Microsoft YaHei",sans-serif;margin:16px 20px;color:#1a1a1a}}
h1{{font-size:17px;margin:0 0 4px}} .sub{{color:#666;font-size:12px;margin-bottom:10px}}
table{{border-collapse:collapse;width:100%;max-width:820px}}
th,td{{border:1px solid #ccc;padding:3px 7px}} th{{background:#f2f2f2;font-size:11.5px;text-align:left}}
td.r{{font-weight:700;width:46px}} td.c{{text-align:right;width:70px;font-variant-numeric:tabular-nums}}
td.note{{color:#555;font-size:11.5px}}
.ok{{border-left:3px solid #2e7d32;background:#f1f8f1;padding:6px 10px;margin:8px 0;font-size:12px}}
.bad{{border-left:3px solid #b3261e;background:#fdf3f2;padding:6px 10px;margin:8px 0;font-size:12px}}
</style></head><body>
<h1>摆件坐标表 · 80 × 40 mm（原点=板左下角）</h1>
<div class=sub>单位 mm，坐标为<b>封装中心</b>。在属性面板直接输入 X/Y 比拖拽准。几何校验：{len(errs)} 个错误 / {len(warns)} 个提示。</div>
{''.join(f'<div class="bad">✗ {e}</div>' for e in errs)}
{''.join(f'<div class=ok>· {w}</div>' for w in warns)}
<table><tr><th>位号</th><th>器件</th><th>X</th><th>Y</th><th>外廓 W×H</th><th>说明</th></tr>{''.join(rows)}</table>
<div class=sub style="margin-top:10px">安装孔：(5,5) (75,5) (5,35) (75,35)，Ø3.00，用过孔放置。板框 80.00×40.00 闭合。</div>
</body></html>"""
    out = os.path.join(HERE, "摆件坐标表.html")
    open(out, "w", encoding="utf-8", newline="\n").write(html)
    json.dump({r: {"x": P[r][0], "y": P[r][1], "w": P[r][2], "h": P[r][3]} for r in refs},
              open(os.path.join(HERE, "placement.json"), "w",
                   encoding="utf-8", newline="\n"), ensure_ascii=False, indent=1)
    print("写出:", out)
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
