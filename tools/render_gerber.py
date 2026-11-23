#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Gerber (RS-274X) / Excellon 文件直接光栅化出 PCB 预览图。

为什么不用 EDA 软件截图：
    Gerber 是下单时发给板厂的那份文件，是这块板唯一的"事实"。截图证明不了
    截图里的东西和下单文件一致。这里反过来做：把 Gerber 当输入，自己解析、
    自己画。画得出来 = 文件自洽；画得对 = 我的解析器对。两者互相印证。

    这是和 arm-mujoco-sim 里 check_axes.py / check_chain.py 同一套思路：
    数据是唯一源，图是数据的函数，不是手工产物。

支持的 Gerber 子集（够覆盖 EasyEDA Pro / KiCad / AD 导出的常规板）：
    %FSLAX..Y..*%   坐标格式（省略前导零 / 绝对 / 整数位.小数位）
    %MOMM / %MOIN   单位
    %ADDnn C/R/O/P  圆 / 矩形 / 椭圆(obround) / 正多边形孔径
    %LPD / %LPC     正片 / 负片极性（负片只对 flash 生效，简化处理）
    G01 / G02 / G03 直线 / 顺时针弧 / 逆时针弧（弧按弦高误差离散）
    D01 / D02 / D03 拉线 / 移动 / 闪光
    G36 / G37       区域填充（多边形）
    G54Dnn / Dnn    选孔径

支持的 Excellon 子集：
    METRIC / INCH、Tnn C<dia>、Tnn 选刀、X..Y.. 打孔

依赖：numpy + Pillow。两者都是本机既有环境，不额外装东西。

用法：
    python tools/render_gerber.py --gerber pcb-alcohol/gerber --out pcb-alcohol/shots
    python tools/render_gerber.py --gerber pcb-alcohol/gerber --out /tmp/x --px-per-mm 40 --ss 6
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys

import numpy as np
from PIL import Image, ImageDraw


# --------------------------------------------------------------------------
# 一、Gerber 解析
# --------------------------------------------------------------------------

# 孔径定义：%ADD10C,1.80086*%  →  ("C", [1.80086])
_APERTURE_RE = re.compile(r"%ADD(\d+)([CROP]),([0-9.X]+)\*%")
# 格式串：%FSLAX45Y45*%
_FORMAT_RE = re.compile(r"%FS([LTD]?)([AI]?)X(\d)(\d)Y(\d)(\d)\*%")
# 坐标指令：X123Y456D03  允许缺 X 或 缺 Y，允许负号（split('*') 后已不含结尾星号）
_COORD_RE = re.compile(r"(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?(D0[123])?")


class Aperture:
    """一个孔径。shape ∈ C/R/O/P。"""

    __slots__ = ("shape", "params")

    def __init__(self, shape: str, params: list[float]):
        self.shape = shape
        self.params = params

    @property
    def size(self) -> tuple[float, float]:
        """外接尺寸 (w, h)，单位 mm。"""
        if self.shape == "C":
            d = self.params[0]
            return d, d
        if self.shape in ("R", "O"):
            return self.params[0], self.params[1]
        if self.shape == "P":
            d = self.params[0]
            return d, d
        return 0.0, 0.0

    def width(self) -> float:
        """画线时用的线宽（矩形孔径取短边，简化）。"""
        w, h = self.size
        return min(w, h) if self.shape in ("R", "O") else w

    def __repr__(self) -> str:
        return f"Aperture({self.shape},{self.params})"


class GerberLayer:
    """把一份 Gerber 文件解析成图元列表。

    图元是三元组的形式：
        ("flash", x, y, Aperture)
        ("line",  x0, y0, x1, y1, Aperture)
        ("region", [(x, y), ...])
    单位统一成 mm，原点不动（Y 向上）。
    """

    def __init__(self, path: str):
        self.path = path
        self.apertures: dict[int, Aperture] = {}
        self.prims: list[tuple] = []
        self.unit_scale = 1.0          # 转成 mm 的系数
        self.int_digits = 4
        self.dec_digits = 5
        self._parse()

    # -- 坐标换算 --------------------------------------------------------
    def _coord(self, raw: str | None) -> float | None:
        """把 Gerber 里省了小数点的整数串换算成 mm。

        %FSLAX45Y45*% 表示 4 位整数 5 位小数，X8000000 → 80.00000 mm。
        单位是 MOMM 时不再乘；是 MOIN 时乘 25.4。
        """
        if raw is None:
            return None
        v = int(raw) / (10.0 ** self.dec_digits)
        return v * self.unit_scale

    # -- 主循环 ----------------------------------------------------------
    def _parse(self) -> None:
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        # 先扫一遍头部，把单位和格式拿到手
        m = _FORMAT_RE.search(text)
        if m:
            self.int_digits = int(m.group(3))
            self.dec_digits = int(m.group(4))
        if "%MOIN*%" in text:
            self.unit_scale = 25.4
        for am in _APERTURE_RE.finditer(text):
            code = int(am.group(1))
            shape = am.group(2)
            nums = [float(v) for v in am.group(3).split("X") if v]
            self.apertures[code] = Aperture(shape, nums)

        cur_ap: Aperture | None = None
        x = y = 0.0
        interp = 1                       # 1=G01 直线, 2=G02 顺弧, 3=G03 逆弧
        in_region = False
        region_pts: list[tuple[float, float]] = []

        # 去掉所有 %...% 的扩展指令和 G04 注释，剩下的按 * 分句
        body = re.sub(r"%[^%]*%", "", text)
        for stmt in body.split("*"):
            stmt = stmt.strip().replace("\n", "").replace("\r", "")
            if not stmt or stmt.startswith("G04"):
                continue

            # 选孔径：G54D10 / D10
            md = re.fullmatch(r"(?:G54)?D(\d+)", stmt)
            if md:
                code = int(md.group(1))
                if code >= 10:
                    cur_ap = self.apertures.get(code, cur_ap)
                continue

            if stmt.startswith("G36"):
                in_region, region_pts = True, []
                continue
            if stmt.startswith("G37"):
                if len(region_pts) >= 3:
                    self.prims.append(("region", region_pts))
                in_region, region_pts = False, []
                continue

            mg = re.fullmatch(r"G0([123])", stmt)
            if mg:
                interp = int(mg.group(1))
                continue
            if stmt in ("G75", "G74", "G90", "G91", "M02", "M00", "M01"):
                continue

            # G01X0Y0D02 这种「插补码和坐标写在同一句」的形式，先把前缀剥掉
            mpre = re.match(r"G0([123])", stmt)
            if mpre:
                interp = int(mpre.group(1))
                stmt = stmt[3:]

            mc = _COORD_RE.fullmatch(stmt)
            if not mc or not any(mc.groups()):
                # X..Y..D01 这种不带 G 的，_COORD_RE 能吃到；其余跳过
                continue

            nx, ny = self._coord(mc.group(1)), self._coord(mc.group(2))
            ci, cj = self._coord(mc.group(3)), self._coord(mc.group(4))
            op = mc.group(5)
            tx = x if nx is None else nx
            ty = y if ny is None else ny

            if in_region:
                if op == "D02" or not region_pts:
                    region_pts = [(tx, ty)]
                else:
                    region_pts.append((tx, ty))
                x, y = tx, ty
                continue

            if op == "D03":
                if cur_ap is not None:
                    self.prims.append(("flash", tx, ty, cur_ap))
            elif op == "D01":
                if cur_ap is not None:
                    if interp in (2, 3) and (ci is not None or cj is not None):
                        pts = _arc_points(x, y, tx, ty, ci or 0.0, cj or 0.0, interp == 3)
                        for i in range(len(pts) - 1):
                            self.prims.append(("line", pts[i][0], pts[i][1],
                                               pts[i + 1][0], pts[i + 1][1], cur_ap))
                    else:
                        self.prims.append(("line", x, y, tx, ty, cur_ap))
            x, y = tx, ty

    def bbox(self) -> tuple[float, float, float, float] | None:
        xs, ys = [], []
        for p in self.prims:
            if p[0] == "flash":
                xs.append(p[1]); ys.append(p[2])
            elif p[0] == "line":
                xs += [p[1], p[3]]; ys += [p[2], p[4]]
            elif p[0] == "region":
                xs += [q[0] for q in p[1]]; ys += [q[1] for q in p[1]]
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def stats(self) -> dict:
        out = {"flash": 0, "line": 0, "region": 0}
        for p in self.prims:
            out[p[0]] += 1
        out["apertures"] = len(self.apertures)
        return out


def _arc_points(x0, y0, x1, y1, i, j, ccw, max_err=0.01):
    """把 (I,J) 圆弧离散成折线，弦高误差 ≤ max_err mm。"""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(x0 - cx, y0 - cy)
    if r < 1e-9:
        return [(x0, y0), (x1, y1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if ccw:
        while a1 <= a0:
            a1 += 2 * math.pi
    else:
        while a1 >= a0:
            a1 -= 2 * math.pi
    sweep = abs(a1 - a0)
    # 弦高 h = r(1-cos(dθ/2)) ≤ err  →  dθ ≤ 2·acos(1-err/r)
    if r <= max_err:
        return [(x0, y0), (x1, y1)]
    dth = 2 * math.acos(max(-1.0, 1 - max_err / r))
    n = max(2, int(math.ceil(sweep / dth)))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n),
             cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]


# --------------------------------------------------------------------------
# 二、Excellon 钻孔解析
# --------------------------------------------------------------------------

class DrillFile:
    """钻孔文件 → [(x_mm, y_mm, dia_mm), ...]"""

    def __init__(self, path: str):
        self.path = path
        self.holes: list[tuple[float, float, float]] = []
        self.tools: dict[int, float] = {}
        self._parse()

    def _parse(self) -> None:
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f]

        scale = 25.4 if any(ln.upper().startswith("INCH") for ln in lines
                            if not ln.startswith(";")) else 1.0
        cur_dia = 0.0
        for ln in lines:
            if not ln or ln.startswith(";"):
                continue
            mt = re.fullmatch(r"T(\d+)C([0-9.]+)", ln)
            if mt:
                self.tools[int(mt.group(1))] = float(mt.group(2)) * scale
                continue
            mt = re.fullmatch(r"T(\d+)", ln)
            if mt:
                cur_dia = self.tools.get(int(mt.group(1)), cur_dia)
                continue
            mh = re.fullmatch(r"X(-?[0-9.]+)Y(-?[0-9.]+)", ln)
            if mh:
                self.holes.append((float(mh.group(1)) * scale,
                                   float(mh.group(2)) * scale, cur_dia))

    def stats(self) -> dict:
        by = {}
        for _, _, d in self.holes:
            by[round(d, 3)] = by.get(round(d, 3), 0) + 1
        return {"count": len(self.holes), "tools": by}


# --------------------------------------------------------------------------
# 三、光栅化
# --------------------------------------------------------------------------

class Raster:
    """把图元画到 PIL 的 'L' 掩膜上。坐标 mm（Y 向上）→ 像素（Y 向下）。

    ss = 超采样倍数。所有绘制在 ss 倍画布上做，最后 LANCZOS 缩回去当抗锯齿。
    """

    def __init__(self, bbox, px_per_mm: float = 30.0, ss: int = 4, pad_mm: float = 1.0):
        x0, y0, x1, y1 = bbox
        self.x0, self.y0 = x0 - pad_mm, y0 - pad_mm
        self.x1, self.y1 = x1 + pad_mm, y1 + pad_mm
        self.ppm = px_per_mm * ss
        self.ss = ss
        self.w = int(round((self.x1 - self.x0) * self.ppm))
        self.h = int(round((self.y1 - self.y0) * self.ppm))
        self.img = Image.new("L", (self.w, self.h), 0)
        self.draw = ImageDraw.Draw(self.img)

    def _t(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.x0) * self.ppm, (self.y1 - y) * self.ppm)

    def _len(self, v: float) -> int:
        return max(1, int(round(v * self.ppm)))

    # -- 单个孔径的形状 --------------------------------------------------
    def flash(self, x: float, y: float, ap: Aperture) -> None:
        px, py = self._t(x, y)
        w, h = ap.size
        if ap.shape == "C":
            r = w / 2.0 * self.ppm
            self.draw.ellipse([px - r, py - r, px + r, py + r], fill=255)
        elif ap.shape == "R":
            hw, hh = w / 2.0 * self.ppm, h / 2.0 * self.ppm
            self.draw.rectangle([px - hw, py - hh, px + hw, py + hh], fill=255)
        elif ap.shape == "O":
            hw, hh = w / 2.0 * self.ppm, h / 2.0 * self.ppm
            rad = min(hw, hh)
            self.draw.rounded_rectangle([px - hw, py - hh, px + hw, py + hh],
                                        radius=rad, fill=255)
        elif ap.shape == "P":
            r = w / 2.0 * self.ppm
            n = int(ap.params[1]) if len(ap.params) > 1 else 6
            rot = math.radians(ap.params[2]) if len(ap.params) > 2 else 0.0
            pts = [(px + r * math.cos(rot + 2 * math.pi * k / n),
                    py - r * math.sin(rot + 2 * math.pi * k / n)) for k in range(n)]
            self.draw.polygon(pts, fill=255)

    def line(self, x0, y0, x1, y1, ap: Aperture) -> None:
        p0, p1 = self._t(x0, y0), self._t(x1, y1)
        lw = self._len(ap.width())
        if lw <= 1:
            self.draw.line([p0, p1], fill=255, width=1)
            return
        self.draw.line([p0, p1], fill=255, width=lw)
        # 圆头补端帽（PIL 的 line 是方头）
        r = lw / 2.0
        for px, py in (p0, p1):
            self.draw.ellipse([px - r, py - r, px + r, py + r], fill=255)

    def region(self, pts) -> None:
        if len(pts) >= 3:
            self.draw.polygon([self._t(x, y) for x, y in pts], fill=255)

    def holes(self, holes, grow: float = 0.0) -> None:
        for x, y, d in holes:
            px, py = self._t(x, y)
            r = (d / 2.0 + grow) * self.ppm
            if r < 0.5:
                continue
            self.draw.ellipse([px - r, py - r, px + r, py + r], fill=255)

    def mask(self) -> np.ndarray:
        """缩回目标分辨率，返回 0..1 的 float 掩膜。"""
        if self.ss > 1:
            small = self.img.resize((self.w // self.ss, self.h // self.ss),
                                    Image.LANCZOS)
        else:
            small = self.img
        return np.asarray(small, dtype=np.float32) / 255.0

    def size(self) -> tuple[int, int]:
        return self.w // self.ss, self.h // self.ss


def _trace_ring(layer: GerberLayer):
    """把板框的线段串成一个闭合环，返回点序列（不含重复的收尾点）。

    EasyEDA 导出的 GKO 是「D02 移到起点 + 连续 D01 走一圈」，所以按端点
    首尾相接的顺序串就行。串不成闭合环就返回 None，走 floodfill 兜底。
    """
    segs = [((round(p[1], 4), round(p[2], 4)), (round(p[3], 4), round(p[4], 4)))
            for p in layer.prims if p[0] == "line"]
    if not segs:
        return None
    ring = [segs[0][0], segs[0][1]]
    used = {0}
    while len(used) < len(segs):
        tail = ring[-1]
        for i, (a, b) in enumerate(segs):
            if i in used:
                continue
            if a == tail:
                ring.append(b); used.add(i); break
            if b == tail:
                ring.append(a); used.add(i); break
        else:
            break
        if ring[-1] == ring[0]:
            return ring[:-1]
    return None


def board_fill_mask(layer: GerberLayer, bbox, px_per_mm: float, ss: int) -> np.ndarray:
    """得到「板内」的实心掩膜，用来做外形裁切。

    优先把板框线段串成闭合多边形直接填充（O(n)，毫秒级）。串不起来才退到
    从画布中心做 floodfill —— 那个在超采样画布上要跑两分钟，只能当兜底。
    """
    r = Raster(bbox, px_per_mm=px_per_mm, ss=ss)
    ring = _trace_ring(layer)
    if ring and len(ring) >= 3:
        r.draw.polygon([r._t(x, y) for x, y in ring], fill=255)
    else:
        draw_layer(layer, r)
        ImageDraw.floodfill(r.img, (r.w // 2, r.h // 2), 200, thresh=8)
        r.img = r.img.point(lambda v: 255 if v >= 100 else v)
    small = r.img.resize((r.w // ss, r.h // ss), Image.LANCZOS)
    return np.asarray(small, dtype=np.float32) / 255.0


def draw_layer(layer: GerberLayer, raster: Raster) -> None:
    for p in layer.prims:
        kind = p[0]
        if kind == "flash":
            raster.flash(p[1], p[2], p[3])
        elif kind == "line":
            raster.line(p[1], p[2], p[3], p[4], p[5])
        elif kind == "region":
            raster.region(p[1])


# --------------------------------------------------------------------------
# 四、配色与合成
# --------------------------------------------------------------------------

# 阻焊绿（板厂最常见的哑光绿）、露铜 HASL 银、白色丝印
C_MASK_TOP = (22, 84, 54)
C_MASK_BOT = (20, 74, 48)
C_COPPER_UNDER = (48, 130, 86)      # 被绿油盖住的铜：油墨在铜面上更平整，反而更亮
C_PAD = (206, 208, 198)             # 喷锡焊盘
C_SILK = (238, 238, 232)
C_DRILL = (18, 22, 20)
C_BARE = (12, 40, 26)               # 开窗但下面没铜 —— 基材


def blend(base: np.ndarray, color, mask: np.ndarray) -> np.ndarray:
    """按掩膜做 alpha 混合。color 是 0-255 的 RGB。"""
    a = mask[..., None]
    c = np.array(color, dtype=np.float32)
    return base * (1 - a) + c * a


def knock_out(img: Image.Image, outline_mask: np.ndarray,
              bg=(246, 245, 242)) -> Image.Image:
    """把板框外换成纸张底色，做出「一块板放在桌上」的观感。"""
    arr = np.asarray(img, dtype=np.float32)
    a = outline_mask[..., None]
    arr = arr * a + np.array(bg, dtype=np.float32) * (1 - a)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def compose_side(copper, solder_mask, silk, drill, mask_color) -> Image.Image:
    """合成一面的观感。

    真实板子的层次（从上往下看）：
        阻焊油墨盖在铜上 → 只能看到隐约的走线轮廓
        阻焊开窗处露出铜 → 喷锡后是银白色
        丝印印在油墨上 → 白色
        孔是通孔 → 黑色（想象成看不到底）
    """
    h, w = copper.shape
    img = np.empty((h, w, 3), dtype=np.float32)
    img[:] = np.array(mask_color, dtype=np.float32)

    # 1) 绿油下的铜：比底色略亮，模拟油墨叠在铜上的厚度差
    img = blend(img, C_COPPER_UNDER, copper * 0.85)

    # 2) 开窗区域：有铜就是焊盘，没铜就是基材
    open_mask = solder_mask
    pad_mask = np.minimum(open_mask, copper)
    bare_mask = np.clip(open_mask - copper, 0.0, 1.0)
    img = blend(img, C_BARE, bare_mask * 0.75)
    img = blend(img, C_PAD, pad_mask)

    # 3) 丝印
    img = blend(img, C_SILK, silk)

    # 4) 钻孔最后打，盖住一切
    img = blend(img, C_DRILL, drill)

    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


# --------------------------------------------------------------------------
# 五、主流程
# --------------------------------------------------------------------------

# 文件名 → 用途。不同 EDA 导出的后缀不一样，两套都认。
LAYER_ALIASES = {
    "top":        ["Gerber_TopLayer.GTL", "*.GTL", "*.gtl"],
    "bottom":     ["Gerber_BottomLayer.GBL", "*.GBL", "*.gbl"],
    "silk_top":   ["Gerber_TopSilkscreenLayer.GTO", "*.GTO", "*.gto"],
    "silk_bot":   ["Gerber_BottomSilkscreenLayer.GBO", "*.GBO", "*.gbo"],
    "mask_top":   ["Gerber_TopSolderMaskLayer.GTS", "*.GTS", "*.gts"],
    "mask_bot":   ["Gerber_BottomSolderMaskLayer.GBS", "*.GBS", "*.gbs"],
    "outline":    ["Gerber_BoardOutlineLayer.GKO", "*.GKO", "*.gko", "*.GM1"],
    "drill":      ["Drill_PTH_Through.DRL", "*.DRL", "*.drl", "*.XLN"],
}


def find_file(folder: str, patterns) -> str | None:
    import glob
    for pat in patterns:
        hit = sorted(glob.glob(os.path.join(folder, pat)))
        if hit:
            return hit[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Gerber / Excellon → PNG 预览图")
    ap.add_argument("--gerber", required=True, help="Gerber 文件所在目录")
    ap.add_argument("--out", required=True, help="PNG 输出目录")
    ap.add_argument("--px-per-mm", "--ppi", dest="px_per_mm", type=float, default=30.0,
                    help="每毫米像素数（默认 30）。--ppi 是旧名，保留兼容；注意 PPI 本义是每英寸像素数，30 px/mm = 762 PPI")
    ap.add_argument("--ss", type=int, default=4, help="超采样倍数（默认 4）")
    ap.add_argument("--name", default="", help="输出文件名前缀")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    pre = args.name

    paths = {k: find_file(args.gerber, v) for k, v in LAYER_ALIASES.items()}
    missing = [k for k, v in paths.items() if v is None]
    print("=== 输入 ===")
    for k, v in paths.items():
        print(f"  {k:10s} {os.path.basename(v) if v else '(缺)'}")
    if paths["outline"] is None and paths["top"] is None:
        print("！既没有板框也没有顶铜，无法确定画布范围", file=sys.stderr)
        return 2

    # 画布范围优先用板框，没有就用所有层的并集
    outline = GerberLayer(paths["outline"]) if paths["outline"] else None
    bbox = outline.bbox() if outline else None
    if bbox is None:
        boxes = [GerberLayer(p).bbox() for p in paths.values() if p]
        boxes = [b for b in boxes if b]
        bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    print(f"\n=== 板框 === {bw:.3f} × {bh:.3f} mm, 原点 ({bbox[0]:.3f}, {bbox[1]:.3f})")

    # 一层一个 Raster（共用同一套变换，才能对齐）
    def new_raster():
        return Raster(bbox, px_per_mm=args.px_per_mm, ss=args.ss)

    layers = {}
    for key in ("top", "bottom", "silk_top", "silk_bot", "mask_top", "mask_bot", "outline"):
        if not paths[key]:
            continue
        gl = GerberLayer(paths[key])
        r = new_raster()
        draw_layer(gl, r)
        layers[key] = r
        st = gl.stats()
        print(f"  {key:10s} flash={st['flash']:4d} line={st['line']:4d} "
              f"region={st['region']:3d} aperture={st['apertures']}")

    drills = []
    if paths["drill"]:
        df = DrillFile(paths["drill"])
        drills = df.holes
        st = df.stats()
        print(f"  {'drill':10s} 孔 {st['count']} 个，刀具 {st['tools']}")

    W, H = None, None
    zero = np.zeros((1, 1), dtype=np.float32)

    def get(key):
        nonlocal W, H
        if key not in layers:
            return None
        m = layers[key].mask()
        W, H = layers[key].size()
        return m

    top = get("top")
    bot = get("bottom")
    silk_t = get("silk_top") if "silk_top" in layers else None
    silk_b = get("silk_bot") if "silk_bot" in layers else None
    mask_t = get("mask_top") if "mask_top" in layers else None
    mask_b = get("mask_bot") if "mask_bot" in layers else None
    if W is None:
        print("！没有可画的铜层", file=sys.stderr)
        return 2
    z = np.zeros((H, W), dtype=np.float32)

    # 钻孔掩膜
    dr = new_raster()
    dr.holes(drills)
    drill_m = dr.mask()

    # 板框掩膜 —— 填充出「板内」实心区域，用来做外形裁切
    if outline:
        outline_m = board_fill_mask(outline, bbox, args.px_per_mm, args.ss)
    else:
        outline_m = np.ones((H, W), dtype=np.float32)

    print(f"\n=== 输出 === 画布 {W} × {H} px @ {args.px_per_mm} px/mm, 超采样 {args.ss}×")

    # --- 01 顶面 ---
    img_top = compose_side(top, mask_t if mask_t is not None else z,
                           silk_t if silk_t is not None else z, drill_m, C_MASK_TOP)
    img_top = knock_out(img_top, outline_m)
    p = os.path.join(args.out, f"{pre}01-top.png")
    img_top.save(p, optimize=True)
    print(f"  {p}  {os.path.getsize(p)//1024} KB")

    # --- 02 底面（镜像，从下往上看）---
    flip = lambda a: a[:, ::-1] if a is not None else None
    img_bot = compose_side(flip(bot), flip(mask_b) if mask_b is not None else z,
                           flip(silk_b) if silk_b is not None else z,
                           flip(drill_m), C_MASK_BOT)
    img_bot = knock_out(img_bot, flip(outline_m))
    p = os.path.join(args.out, f"{pre}02-bottom.png")
    img_bot.save(p, optimize=True)
    print(f"  {p}  {os.path.getsize(p)//1024} KB")

    # --- 03 双面铜叠色（设计检查视角）---
    canvas = np.empty((H, W, 3), dtype=np.float32)
    canvas[:] = np.array([16, 18, 24], dtype=np.float32)
    canvas = blend(canvas, (214, 78, 58), top)              # 顶铜 红
    canvas = blend(canvas, (58, 132, 214), flip(bot))       # 底铜 蓝
    canvas = blend(canvas, (255, 255, 255), drill_m * 0.9)  # 孔 白
    img_cu = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    p = os.path.join(args.out, f"{pre}03-copper.png")
    img_cu.save(p, optimize=True)
    print(f"  {p}  {os.path.getsize(p)//1024} KB")

    # --- 04 丝印单独看（印字是否能读清）---
    canvas = np.empty((H, W, 3), dtype=np.float32)
    canvas[:] = np.array([22, 26, 30], dtype=np.float32)
    if silk_t is not None:
        canvas = blend(canvas, (235, 235, 230), silk_t)
    img_sk = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    p = os.path.join(args.out, f"{pre}04-silkscreen.png")
    img_sk.save(p, optimize=True)
    print(f"  {p}  {os.path.getsize(p)//1024} KB")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
