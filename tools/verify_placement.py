#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交叉验证：摆件坐标表里的坐标，真的落到 Gerber / 飞针文件里了吗？

三个互相独立的来源：
    placement.json           摆件时人工填的封装中心坐标（**设计意图**）
    gerber/*.DRL             Excellon 钻孔，板上真实存在的孔位（**产物**）
    gerber/FlyingProbeTesting.json   EDA 导出的器件坐标，单位 mil（**产物**）

只对得上「意图」，说明表格是自己写的；只对得上「产物」，说明表格是事后
抄的。两头都独立存在又互相咬合，才说明这张表描述的是板上真实发生的事。

三类检查：
    A 板框   —— GKO 的包围盒是否等于声明的板尺寸
    B 安装孔 —— 钻孔文件里 Ø3.0 的 4 个孔位是否等于声明的 4 个坐标（误差 0）
    C 器件   —— 飞针文件换算成 mm 后，是否落在 placement.json 声明的本体框内

用法：
    python tools/verify_placement.py
    python tools/verify_placement.py --json      # 机器可读输出，便于接 CI
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from render_gerber import DrillFile, GerberLayer, find_file, LAYER_ALIASES  # noqa: E402

MIL = 0.0254   # 1 mil → mm

# place.py 里声明的东西，这里作为「意图」的对照基准
BOARD_MM = (80.0, 40.0)
MOUNT_HOLES = [(5.0, 5.0), (75.0, 5.0), (5.0, 35.0), (75.0, 35.0)]
MOUNT_DIA = 3.0
TOL_BOARD = 0.01      # 板框容差 mm
TOL_HOLE = 0.01       # 孔位容差 mm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcb", default=os.path.join(ROOT, "pcb-alcohol"))
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    pcb = args.pcb
    gdir = os.path.join(pcb, "gerber")
    place = json.load(open(os.path.join(pcb, "placement.json"), encoding="utf-8"))

    fails, warns, lines = [], [], []

    def say(s=""):
        lines.append(s)

    # ---------------- A 板框 ----------------
    say("=== A. 板框 ===")
    ok_p, bb = find_file(gdir, LAYER_ALIASES["outline"]), None
    if ok_p:
        bb = GerberLayer(ok_p).bbox()
    if not bb:
        fails.append("A: 找不到板框层或板框为空")
        say("  ✗ 板框层缺失")
    else:
        w = bb[2] - bb[0]
        h = bb[3] - bb[1]
        d_ok = abs(w - BOARD_MM[0]) <= TOL_BOARD and abs(h - BOARD_MM[1]) <= TOL_BOARD
        say(f"  GKO 包围盒 {w:.3f} × {h:.3f} mm   声明 {BOARD_MM[0]:.1f} × {BOARD_MM[1]:.1f} mm"
            f"   {'✓ 一致' if d_ok else '✗ 不符'}")
        if not d_ok:
            fails.append(f"A: 板框 {w:.3f}×{h:.3f} != {BOARD_MM}")

    # ---------------- B 安装孔 ----------------
    say("\n=== B. 安装孔 ===")
    dp = find_file(gdir, LAYER_ALIASES["drill"])
    if not dp:
        fails.append("B: 找不到钻孔文件")
        say("  ✗ 钻孔文件缺失")
    else:
        df = DrillFile(dp)
        big = [(x, y, d) for x, y, d in df.holes if abs(d - MOUNT_DIA) < 0.05]
        say(f"  钻孔 {len(df.holes)} 个，其中 Ø{MOUNT_DIA} 安装孔 {len(big)} 个")
        if len(big) != len(MOUNT_HOLES):
            fails.append(f"B: 安装孔数量 {len(big)} != {len(MOUNT_HOLES)}")
        for hx, hy in MOUNT_HOLES:
            best = min(big, key=lambda t: (t[0] - hx) ** 2 + (t[1] - hy) ** 2) if big else None
            if best is None:
                fails.append(f"B: 声明孔 ({hx},{hy}) 在钻孔文件里没有对应")
                continue
            dist = ((best[0] - hx) ** 2 + (best[1] - hy) ** 2) ** 0.5
            tag = "✓" if dist <= TOL_HOLE else "✗"
            say(f"  {tag} ({hx:5.1f},{hy:5.1f}) → 实际 ({best[0]:.5f},{best[1]:.5f})  偏差 {dist*1000:.2f} µm")
            if dist > TOL_HOLE:
                fails.append(f"B: 孔 ({hx},{hy}) 偏差 {dist:.4f} mm")

    # ---------------- C 器件坐标 ----------------
    say("\n=== C. 器件坐标 vs 飞针文件 ===")
    fp_path = os.path.join(gdir, "FlyingProbeTesting.json")
    if not os.path.exists(fp_path):
        warns.append("C: 没有飞针文件，跳过器件交叉验证")
        say("  · 无 FlyingProbeTesting.json，跳过")
    else:
        fp = json.load(open(fp_path, encoding="utf-8"))
        rows = fp["components"]["rows"]
        fields = fp["components"]["fields"]
        idx = {k: i for i, k in enumerate(fields)}
        unit = MIL if fp.get("lengthUnit", "mil") == "mil" else 1.0

        fp_map = {r[idx["COMPONENT_NAME"]]: (r[idx["X_COORDINATE"]] * unit,
                                            r[idx["Y_COORDINATE"]] * unit)
                  for r in rows}
        say(f"  飞针文件 {len(fp_map)} 个器件，单位 {fp.get('lengthUnit')} → mm")

        inside = 0
        worst = []
        for ref, it in sorted(place.items()):
            if ref not in fp_map:
                warns.append(f"C: {ref} 不在飞针文件里")
                continue
            fx, fy = fp_map[ref]
            dx = fx - it["x"]
            dy = fy - it["y"]
            # 判据：飞针点必须落在声明的本体框内（框是器件真实占位，不是随便写的）
            ok = abs(dx) <= it["w"] / 2 + 1e-6 and abs(dy) <= it["h"] / 2 + 1e-6
            inside += 1 if ok else 0
            worst.append((abs(dx) + abs(dy), ref, dx, dy, ok, it["w"], it["h"]))
        worst.sort(reverse=True)

        say(f"  落在声明本体框内：{inside} / {len(place)}")
        say(f"  偏差最大的 6 个（dx, dy 是飞针点相对声明中心的偏移）：")
        for _, ref, dx, dy, ok, w, h in worst[:6]:
            say(f"    {'✓' if ok else '✗'} {ref:4s} dx={dx:+7.3f} dy={dy:+7.3f}  "
                f"框 {w:.1f}×{h:.1f}")
        if inside < len(place):
            fails.append(f"C: {len(place)-inside} 个器件的飞针点落在声明框外")
        # 最大偏差只作提示：不同封装的坐标原点（中心/1 脚/边缘）本来就不同
        mx = worst[0][0] if worst else 0.0
        if mx > 3.0:
            warns.append(f"C: 最大偏移 {mx:.2f} mm，检查封装原点定义")
        say(f"  · 注意：飞针点取的是封装内的一个参考点，各封装原点定义不同"
            f"（中心 / 1 脚 / 边缘），所以判据用的是「落在本体框内」而不是「完全重合」。")

    # ---------------- 汇总 ----------------
    say("\n=== 汇总 ===")
    say(f"  失败 {len(fails)} 项 / 提示 {len(warns)} 项")
    for f in fails:
        say(f"  ✗ {f}")
    for w in warns:
        say(f"  · {w}")

    if args.json:
        print(json.dumps({"fails": fails, "warns": warns, "log": lines},
                         ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
