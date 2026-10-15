#!/usr/bin/env python3
"""「条号粘在上一句末尾 → 整条被漏掉」的 MD 拆行修复（先验证后写盘）。

现象：MD 里 `…宜设置纵向受拉钢筋。10.3.9厂房柱间支撑…`（条号粘在上一句末尾、不在行首），
      解析器只认行首条号（或行中条号后带空格）⇒ 该条整条不进库，文字被算进上一条或直接丢掉。
手法：把粘住的位置**拆成两行**（原行只留到标点，条号+正文另起一行）→ 重解析。

四道闸（全部在内存里验，过了才写）：
  ① 拆完该条号必须出现，内容 ≥8 字且含中文；
  ② 其它条里最多只能有 1 条变化，且必须是"上一条只被搬走尾巴"（旧内容是新技术的前缀）。
     「纯捡回」型（那截文字此前没归任何条）允许 0 条变化——同样是净增益；
  ③ 库侧预检：库上一条内容必须以"新上一条内容"开头（否则中止，说明库里有历史补齐）；
  ④ 该条号库里已有的话标出来（说明区残文 → 要用正文内容覆盖，不是新增）。

用法（必须用 stdq venv 跑）：
    python3 fix_glued_clause_lines.py            # 预演，出清单
    python3 fix_glued_clause_lines.py --apply    # 写 MD（备份 .bak.<时间>-gluedsplit）

⚠ 本脚本只改 MD。库里内容要跟着一起改（上一条 UPDATE 缩减 + 新条 INSERT + 向量），
  用同目录 apply_glued_fix.py；写完必须重解析对账，确认"库 == 解析"。
⚠ 预演为空时**不覆盖**已有清单文件（曾被空扫描冲掉过一份待处理清单）。
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..consts import STANDARDS_DIR as DATA_DIR

S = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DATA_DIR, "standards.db")
PLAN = "/tmp/glued_split_plan.json"
from ..parser.clauses import extract_clauses, find_body_start
from ..parser.notes import is_notes_marker
from ..parser.common import is_doc_tail_head

PAT = re.compile(r'[。；;]\s*(\d{1,2}\.\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*(?=[\u4e00-\u9fff])')
NOISE = re.compile(r'colspan|rowspan|<td|</tr>|<table|<img|!\[', re.I)
UNIT = "级位倍度年月日个种类层段条块片根%℃"  # 编号后紧跟这些字 = 量纲/等级，不是条号
app = "--apply" in sys.argv


def nz(s):
    return "".join((s or "").split())


def body_span(lines):
    bs = find_body_start(lines)
    heads = [i for i, l in enumerate(lines) if i >= bs and is_doc_tail_head(l.strip())]
    notes = [i for i, l in enumerate(lines) if i >= bs and is_notes_marker(l)]
    return bs, (min(heads + notes) if (heads or notes) else len(lines))


con = sqlite3.connect(DB, isolation_level=None)
cur = con.cursor()
stds = cur.execute("select code, md_file from standards order by code").fetchall()
plan, reject = [], []
for code, mdf in stds:
    if not mdf or not os.path.exists(mdf):
        continue
    txt = open(mdf, encoding="utf-8", errors="replace").read()
    lines = txt.split("\n")
    bs, end = body_span(lines)
    old = dict(extract_clauses(txt, os.path.basename(mdf)))
    prefixes = {k.rsplit(".", 1)[0] for k in old if "." in k}
    dbs = {r[0]: r[1] for r in cur.execute(
        "select clause_no, content from clauses where std_code=?", (code,)).fetchall()}
    for i in range(bs, end):
        l = lines[i]
        if NOISE.search(l):
            continue
        for m in PAT.finditer(l):
            no = m.group(1)
            if no in old:
                continue
            # 假阳性两道过滤（实测：附录注里的产品等级「12.5级密封材料」被当条号）
            if m.end() < len(l) and l[m.end()] in UNIT:
                continue
            pref = no.rsplit(".", 1)[0]
            if pref not in prefixes and not any(k.startswith(pref + ".") for k in old):
                continue
            pos = l.find(no, max(0, m.start() - 2))
            if pos < 0:
                continue
            probe = lines[:i] + [l[:pos].rstrip(), l[pos:].strip()] + lines[i + 1:]
            new = dict(extract_clauses("\n".join(probe), os.path.basename(mdf)))
            if no not in new or len(nz(new[no])) < 8 or not re.search(r'[\u4e00-\u9fff]', nz(new[no])[:30]):
                reject.append((code, no, i + 1, "拆完仍不成条/内容过短"))
                continue
            changed = [k for k in set(old) | set(new) if nz(old.get(k, "")) != nz(new.get(k, ""))]
            others = [k for k in changed if k != no]
            if len(others) == 0:
                plan.append({"std": code, "md": mdf, "line": i + 1, "clause": no, "prev": None,
                             "prev_old_len": 0, "prev_new_len": 0, "db_prev_ok": True,
                             "db_has_no": no in dbs, "pure_add": True,
                             "new_len": len(nz(new[no])), "head": nz(new[no])[:46]})
                continue
            if len(others) != 1:
                reject.append((code, no, i + 1, f"副作用：变了 {len(changed)} 条 {changed[:5]}"))
                continue
            p = others[0]
            if not (len(nz(new[p])) < len(nz(old[p])) and nz(old[p]).startswith(nz(new[p]))):
                reject.append((code, no, i + 1, f"上一条§{p}不是『只被搬走尾巴』"))
                continue
            plan.append({"std": code, "md": mdf, "line": i + 1, "clause": no, "prev": p,
                         "prev_old_len": len(nz(old[p])), "prev_new_len": len(nz(new[p])),
                         "db_prev_ok": p in dbs and nz(dbs[p]).startswith(nz(new[p])),
                         "db_has_no": no in dbs, "pure_add": False,
                         "new_len": len(nz(new[no])), "head": nz(new[no])[:46]})
print(f"★ 可安全拆行 {len(plan)} 处（{len({p['std'] for p in plan})} 本）；跳过 {len(reject)} 处")
for p in plan:
    _tail = ("（纯捡回：文字此前没归任何条）" if p.get("pure_add")
             else f"上一条§{p['prev']:8s} {p['prev_old_len']:5d}→{p['prev_new_len']:5d}字 ")
    print(f"   {p['std']:22s} 行{p['line']:5d} 新条§{p['clause']:9s}{p['new_len']:5d}字 | {_tail}"
          f"[库上条可改={p['db_prev_ok']} 库已有新条号={p['db_has_no']}]")
    print(f"        {p['head']}…")
for r in reject:
    print(f"   ✗ {r[0]} §{r[1]} 行{r[2]}: {r[3]}")
if plan:
    json.dump(plan, open(PLAN, "w"), ensure_ascii=False, indent=1)
    print(f"清单 → {PLAN}")
else:
    print("（本次无可修项，保留已有清单文件不动）")
if app and plan:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for md in sorted({p["md"] for p in plan}):
        shutil.copy2(md, f"{md}.bak.{stamp}-gluedsplit")
        lines = open(md, encoding="utf-8", errors="replace").read().split("\n")
        for p in sorted([x for x in plan if x["md"] == md], key=lambda x: -x["line"]):
            i = p["line"] - 1
            l = lines[i]
            pos = l.find(p["clause"])
            if pos < 0 or not l[:pos].rstrip().endswith(("。", "；", ";")):
                print(f"   ⚠️ {p['std']} §{p['clause']} 锚定复核失败，跳过")
                continue
            lines[i:i + 1] = [l[:pos].rstrip(), l[pos:].strip()]
        open(md, "w", encoding="utf-8").write("\n".join(lines))
    print(f"✔ MD 已拆行（{len({p['md'] for p in plan})} 本），备份后缀 {stamp}")
    print("收尾：跑 apply_glued_fix.py --apply 同步库与向量")
con.close()
