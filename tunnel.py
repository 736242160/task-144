#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隧道施工「开挖 → 支护 → 衬砌」流程校验工具（纯 Python 标准库，单文件）。

用法:
    python3 tunnel.py 输入.json        # 从 JSON 文件读取输入
    python3 tunnel.py - < 输入.json    # 从标准输入读取
    python3 tunnel.py --demo           # 运行内置示例（覆盖各类错误场景）

输入 JSON 结构:
{
  "围岩分级": [
    {"名称": "II", "支护要求": {"锚杆": 10, "喷射混凝土": 5},
                  "衬砌要求": {"模筑混凝土": 20}}
  ],
  "隧道段": [
    {"名称": "K0+000", "围岩级": "II", "初始状态": "未开挖"}
  ],
  "材料库存": [
    {"名称": "锚杆", "数量": 100}
  ],
  "操作流": [
    {"类型": "开挖", "段": "K0+000"},
    {"类型": "支护", "段": "K0+000"},
    {"类型": "衬砌", "段": "K0+000", "材料": {"模筑混凝土": 20}},
    {"类型": "围岩变更", "段": "K0+000", "围岩级": "IV"}
  ]
}

说明:
- 支护/衬砌操作中的 "材料" 可省略，省略时按该段当前围岩级的要求自动取料；
  若显式给出但不满足围岩级要求，报告「要求与围岩级不匹配」。
- 「围岩变更」为扩展操作：变更后对该段及相邻段（按隧道段定义顺序）
  级联重检已完成的支护/衬砌是否满足新要求，不达要求报告缺项。
- 材料不足时报「段、缺料、缺口量」，该操作不生效；状态跨操作延续。
"""

import json
import sys


class TunnelSim:
    def __init__(self, data):
        self.errors = []
        self.grades = {}
        for g in data.get("围岩分级", []):
            self.grades[g["名称"]] = {
                "支护要求": dict(g.get("支护要求", {})),
                "衬砌要求": dict(g.get("衬砌要求", {})),
            }
        self.segments = []  # 有序，用于相邻段级联
        self.seg_by_name = {}
        for s in data.get("隧道段", []):
            name, grade = s["名称"], s["围岩级"]
            init = s.get("初始状态", "未开挖")
            if name in self.seg_by_name:
                self._err(None, name, "段重复定义", name)
                continue
            if grade not in self.grades:
                self._err(None, name, "围岩级未定义", grade)
            if init != "未开挖":
                self._err(None, name, "初始状态非法", init)
                init = "未开挖"
            seg = {"名称": name, "围岩级": grade, "状态": init,
                   "支护完成": {}, "衬砌完成": {}}
            self.segments.append(seg)
            self.seg_by_name[name] = seg
        self.materials = {}
        for m in data.get("材料库存", []):
            self.materials[m["名称"]] = self.materials.get(m["名称"], 0) + m.get("数量", 0)
        self.ops = data.get("操作流", [])

    def _err(self, op_idx, seg, kind, detail):
        self.errors.append({"操作序号": op_idx, "段": seg, "错误": kind, "详情": detail})

    def run(self):
        for idx, op in enumerate(self.ops, 1):
            self._run_op(idx, op)
        return self

    def _run_op(self, idx, op):
        otype, name = op.get("类型"), op.get("段")
        seg = self.seg_by_name.get(name)
        if seg is None:
            self._err(idx, name, "段不存在", f"操作引用了未定义的段: {name}")
            return
        if otype == "开挖":
            self._excavate(idx, seg)
        elif otype == "支护":
            self._advance(idx, seg, op, "支护", "已开挖", "已支护", "支护完成")
        elif otype == "衬砌":
            self._advance(idx, seg, op, "衬砌", "已支护", "已衬砌", "衬砌完成")
        elif otype == "围岩变更":
            self._change_grade(idx, seg, op)
        else:
            self._err(idx, name, "未知操作类型", str(otype))

    def _excavate(self, idx, seg):
        if seg["状态"] != "未开挖":
            self._err(idx, seg["名称"], "重复开挖", f"段当前状态为「{seg['状态']}」")
            return
        seg["状态"] = "已开挖"

    def _advance(self, idx, seg, op, phase, need_state, done_state, done_key):
        st, name = seg["状态"], seg["名称"]
        if st == "未开挖":
            self._err(idx, name, f"未开挖不能{phase}", "段尚未开挖")
            return
        if phase == "衬砌" and st == "已开挖":
            self._err(idx, name, "跳序", "缺的工序: 支护")
            return
        if st != need_state:
            self._err(idx, name, f"重复{phase}", f"段当前状态为「{st}」")
            return
        use = dict(op["材料"]) if "材料" in op else self._requirement(seg, phase)
        req = self._requirement(seg, phase)
        missing = {m: q - use.get(m, 0) for m, q in req.items() if use.get(m, 0) < q}
        if missing:
            self._err(idx, name, f"{phase}要求与围岩级不匹配",
                      "缺项 " + "、".join(f"{m}x{g}" for m, g in missing.items()))
            return
        if not self._consume(idx, seg, use, phase):
            return
        seg[done_key] = use
        seg["状态"] = done_state

    def _consume(self, idx, seg, need, phase):
        """检查并消耗材料；不足则逐项报「段、缺料、缺口量」，操作不生效。"""
        lack = [(m, q - self.materials.get(m, 0))
                for m, q in need.items() if self.materials.get(m, 0) < q]
        if lack:
            for mat, gap in lack:
                self._err(idx, seg["名称"], "材料不足", f"{phase}缺料 {mat}，缺口量 {gap}")
            return False
        for mat, qty in need.items():
            self.materials[mat] -= qty
        return True

    def _requirement(self, seg, phase):
        return dict(self.grades.get(seg["围岩级"], {}).get(phase + "要求", {}))

    def _change_grade(self, idx, seg, op):
        new_grade = op.get("围岩级")
        if new_grade not in self.grades:
            self._err(idx, seg["名称"], "围岩级未定义", str(new_grade))
            return
        seg["围岩级"] = new_grade
        # 级联：本段及相邻段按各自当前围岩级重检已完成的支护/衬砌
        i = self.segments.index(seg)
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(self.segments):
                self._recheck(idx, self.segments[j])

    def _recheck(self, idx, seg):
        for phase, done_key, states in (
            ("支护", "支护完成", ("已支护", "已衬砌")),
            ("衬砌", "衬砌完成", ("已衬砌",)),
        ):
            if seg["状态"] not in states:
                continue
            req, done = self._requirement(seg, phase), seg[done_key]
            miss = {m: q - done.get(m, 0) for m, q in req.items() if done.get(m, 0) < q}
            if miss:
                self._err(idx, seg["名称"], f"级联重检：{phase}不达新要求",
                          "缺项 " + "、".join(f"{m}x{g}" for m, g in miss.items()))

    def report(self):
        out = ["=== 隧道状态 ==="]
        for s in self.segments:
            extra = ""
            if s["支护完成"]:
                extra += "  支护:" + ",".join(f"{k}x{v}" for k, v in s["支护完成"].items())
            if s["衬砌完成"]:
                extra += "  衬砌:" + ",".join(f"{k}x{v}" for k, v in s["衬砌完成"].items())
            out.append(f"  {s['名称']}  围岩级={s['围岩级']}  状态={s['状态']}{extra}")
        out.append("=== 材料库存（剩余） ===")
        out += [f"  {m}: {q}" for m, q in self.materials.items()] or ["  （空）"]
        out.append("=== 错误清单 ===")
        if not self.errors:
            out.append("  无错误")
        for e in self.errors:
            where = f"操作#{e['操作序号']}" if e["操作序号"] else "输入定义"
            out.append(f"  [{where}] 段={e['段']}  {e['错误']}: {e['详情']}")
        return "\n".join(out)


DEMO = {
    "围岩分级": [
        {"名称": "II", "支护要求": {"锚杆": 10, "喷射混凝土": 5},
                       "衬砌要求": {"模筑混凝土": 20}},
        {"名称": "IV", "支护要求": {"锚杆": 20, "喷射混凝土": 10, "钢拱架": 4},
                       "衬砌要求": {"模筑混凝土": 35, "钢筋": 8}},
    ],
    "隧道段": [
        {"名称": "K0+000", "围岩级": "II", "初始状态": "未开挖"},
        {"名称": "K0+050", "围岩级": "II", "初始状态": "未开挖"},
        {"名称": "K0+100", "围岩级": "II", "初始状态": "未开挖"},
        {"名称": "K0+150", "围岩级": "II", "初始状态": "未开挖"},
        {"名称": "K0+200", "围岩级": "II", "初始状态": "未开挖"},
    ],
    "材料库存": [
        {"名称": "锚杆", "数量": 30},
        {"名称": "喷射混凝土", "数量": 15},
        {"名称": "模筑混凝土", "数量": 100},
        {"名称": "钢拱架", "数量": 10},
        {"名称": "钢筋", "数量": 20},
    ],
    "操作流": [
        {"类型": "开挖", "段": "K0+000"},
        {"类型": "支护", "段": "K0+000"},
        {"类型": "衬砌", "段": "K0+000"},
        {"类型": "开挖", "段": "K0+000"},                                  # 重复开挖
        {"类型": "开挖", "段": "K0+050"},
        {"类型": "支护", "段": "K0+050"},
        {"类型": "衬砌", "段": "K0+050"},
        {"类型": "开挖", "段": "K0+100"},
        {"类型": "衬砌", "段": "K0+100"},                                  # 跳序：缺支护
        {"类型": "支护", "段": "K0+100"},                                  # 锚杆/喷混凝土恰好耗尽
        {"类型": "衬砌", "段": "K0+100", "材料": {"模筑混凝土": 15}},      # 与II级要求(20)不匹配
        {"类型": "衬砌", "段": "K0+100"},
        {"类型": "开挖", "段": "K0+150"},
        {"类型": "支护", "段": "K0+150"},                                  # 材料不足：锚杆/喷混凝土
        {"类型": "支护", "段": "K0+200"},                                  # 未开挖不能支护
        {"类型": "衬砌", "段": "K0+200"},                                  # 未开挖不能衬砌
        {"类型": "开挖", "段": "K9+999"},                                  # 段不存在
        {"类型": "围岩变更", "段": "K0+050", "围岩级": "IV"},              # 级联重检本段及邻段
    ],
}


def main(argv):
    if len(argv) >= 2 and argv[1] == "--demo":
        data = DEMO
    elif len(argv) >= 2:
        src = sys.stdin.read() if argv[1] == "-" else open(argv[1], encoding="utf-8").read()
        data = json.loads(src)
    else:
        print(__doc__)
        return 1
    print(TunnelSim(data).run().report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
