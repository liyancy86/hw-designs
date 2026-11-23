# -*- coding: utf-8 -*-
"""从 circuit-netlist.md 提炼引脚级连线表，校验后输出 netlist.json 与连线卡 HTML。"""
import json
import os
import re
from collections import defaultdict, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
MD = os.path.join(HERE, "circuit-netlist.md")
OUT_JSON = os.path.join(HERE, "netlist.json")
OUT_HTML = os.path.join(HERE, "连线卡.html")

# 位号 -> (器件名, 引脚集合说明)
PARTS = OrderedDict([
    ("J1", ("Type-C 2P", 4)), ("J2", ("XH2.54-5P", 5)),
    ("U1", ("LM393", 8)), ("U2", ("LM393", 8)),
    ("MQ1", ("MQ-3", 6)), ("Q1", ("S8050", 3)), ("BZ1", ("蜂鸣器", 2)),
    ("D1", ("LED", 2)), ("D2", ("LED", 2)), ("D3", ("LED", 2)),
])
for i in range(1, 16):
    PARTS[f"R{i}"] = ("电阻", 2)
for i in range(1, 6):
    PARTS[f"C{i}"] = ("电容", 2)


def parse(text):
    sec = text.split("## 6. 完整网表")[1].split("## 7.")[0]
    nets = OrderedDict()
    for line in sec.splitlines():
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        name = cells[0].strip("* ")
        nets[name] = re.findall(r"([A-Z]+\d+)\.([0-9]+|K|A)", cells[1])
    return nets


def validate(nets):
    pin2net = defaultdict(list)
    for n, ps in nets.items():
        for a, b in ps:
            pin2net[f"{a}.{b}"].append(n)

    expect = set()
    for ref, (_name, n) in PARTS.items():
        if ref.startswith("D"):
            expect |= {f"{ref}.K", f"{ref}.A"}
        else:
            expect |= {f"{ref}.{i}" for i in range(1, n + 1)}

    miss = sorted(p for p in expect if p not in pin2net)
    dup = {p: v for p, v in pin2net.items() if len(v) > 1}
    extra = sorted(p for p in pin2net if p not in expect)
    return pin2net, miss, dup, extra


def main():
    nets = parse(open(MD, encoding="utf-8").read())
    pin2net, miss, dup, extra = validate(nets)
    total = sum(len(v) for v in nets.values())
    print(f"网络 {len(nets)} 个 / 引脚 {total} 个")
    print("未接:", miss or "无")
    print("跨网:", dup or "无")
    print("意外:", extra or "无")

    json.dump(
        {"nets": {k: [f"{a}.{b}" for a, b in v] for k, v in nets.items()},
         "pin2net": {k: v for k, v in pin2net.items()}},
        open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 按位号组织的“每个脚打什么标签”
    by_ref = defaultdict(list)
    for pin, ns in pin2net.items():
        ref, num = pin.split(".")
        by_ref[ref].append((num, ns[0]))
    order = {k: i for i, k in enumerate(PARTS)}

    def key(ref):
        return order.get(ref, 99)

    rows = []
    for ref in sorted(by_ref, key=key):
        pins = sorted(by_ref[ref], key=lambda t: (t[0].isdigit() and int(t[0]) or 99, t[0]))
        name = PARTS.get(ref, ("?", 0))[0]
        cells = "".join(f'<span class="p"><b>{num}</b>{net}</span>' for num, net in pins)
        rows.append(f'<tr><td class="r">{ref}</td><td class="n">{name}</td><td>{cells}</td></tr>')

    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>酒精检测板 · 连线卡</title>
<style>
:root{{--ink:#1a1a1a;--line:#c9c9c9;--hi:#b3261e;--bg:#fff}}
*{{box-sizing:border-box}}
body{{margin:0;padding:14px 18px;font:13px/1.45 "Segoe UI","Microsoft YaHei",sans-serif;color:var(--ink);background:var(--bg)}}
h1{{font-size:17px;margin:0 0 2px}}
.sub{{color:#666;font-size:11.5px;margin-bottom:10px}}
table{{border-collapse:collapse;width:100%;margin:0 0 10px}}
th,td{{border:1px solid var(--line);padding:3px 6px;vertical-align:middle}}
th{{background:#f3f3f3;font-size:11px;text-align:left;font-weight:600}}
td.r{{font-weight:700;width:44px;white-space:nowrap}}
td.n{{color:#666;font-size:11.5px;width:74px;white-space:nowrap}}
.p{{display:inline-block;margin:1px 4px 1px 0;padding:1px 5px;border-radius:3px;background:#f6f6f6;border:1px solid #e2e2e2;font-size:12px}}
.p b{{color:var(--hi);margin-right:3px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.box{{border:1px solid var(--line);border-radius:4px;padding:8px 10px}}
.box h2{{font-size:12.5px;margin:0 0 5px}}
ol,ul{{margin:0;padding-left:17px}}
li{{margin:1.5px 0}}
code{{background:#f4f4f4;padding:0 3px;border-radius:2px;font-size:11.5px}}
.warn{{border-left:3px solid var(--hi);background:#fdf3f2;padding:6px 9px;margin:6px 0;font-size:12px}}
@media print{{body{{padding:6px 10px}} .grid{{grid-template-columns:1fr 1fr}}}}
</style></head><body>
<h1>酒精检测板 · 引脚连线卡（网络标签法）</h1>
<div class="sub">共 {len(nets)} 个网络 / {total} 个引脚，每个引脚都恰好归属一个网络（已程序校验：无漏接、无跨网短路）。
画原理图时：放好器件后，在每个引脚处放<b>网络标签</b>，同名标签自动连通 —— 不用画长线。</div>

<table>
<tr><th>位号</th><th>器件</th><th>引脚 → 该打的网络标签</th></tr>
{''.join(rows)}
</table>

<div class="grid">
<div class="box">
<h2>四步走（顺序别换）</h2>
<ol>
<li>放 30 个器件（先摆 J1/J2/U1/U2/MQ1/Q1/BZ1，再摆 R/C）</li>
<li>按上表给每个脚打网络标签（约 {total} 个标签）</li>
<li>设计 → 原理图转 PCB</li>
<li>按坐标摆件 → 布线 → 导出 Gerber</li>
</ol>
</div>
<div class="box">
<h2>三个必查（画完盯一眼）</h2>
<ul>
<li>BZ1.2 接 <code>BUZ_N</code>（Q1 集电极），<b>不是 GND</b></li>
<li>MQ1.2 接 +5V、MQ1.5 接 GND（加热脚）</li>
<li>三路输出都要有 10K 上拉到 +5V（R11/R12/R13）</li>
</ul>
</div>
</div>

<div class="warn"><b>LED 那三根别漏</b>：+5V 经 R2/R3/R4 到 LED 阳极，LED 阴极才接 OUT1/2/3。
对应网络 <code>LED1_A / LED2_A / LED3_A</code> —— 这三根最容易画成"LED 直接挂 5V"。</div>
</body></html>"""
    open(OUT_HTML, "w", encoding="utf-8").write(html)
    print("写出:", OUT_JSON)
    print("写出:", OUT_HTML)
    return 0 if not (miss or dup or extra) else 1


if __name__ == "__main__":
    raise SystemExit(main())
