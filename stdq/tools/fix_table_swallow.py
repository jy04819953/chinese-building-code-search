#!/usr/bin/env python3
"""修「表被吞」：MD 里表号被拆行（"…应符合表" + "5.4.1的规定。"），
解析器把 `5.4.1的规定。` 当成新条文开头 → 紧随其后的**整张表连同该条正文尾部一起不入库**
（库内容停在「…按表」）。全库指纹 = `rtrim(content) like '%表'`（实测 46 条，其中 30 条真错）。

做法（先验证后写盘，照 md_join_split_clauses.py 的手法）：
  1. 在 MD 里定位「上一行以表结尾 + 下一非空行以该条文号（实为表号）开头」的拆行点；
  2. 内存里把那行并进上一行，重新解析该规范；
  3. 该条新解析内容必须是「库内容的超集」（归一化后以库内容为前缀）且更长 ⇒ 判定可修；
  4. 副作用检查：整本逐条比对，抓出「并前与库一致、并后变了」的其它条（有则人工看）。
写盘顺序按行号**倒序**；写前锚定复核（上一行仍以「表」结尾 / 拆行行仍以表号开头）；
库内容以「改写后 MD 重新解析」为准，保证 MD↔库一致。id 不变 ⇒ 不用 embed-build，但要按  定点刷向量 + 重启 daemon。

用法：
  python3 fix_table_swallow.py            # 预览（只读，计划写 /tmp/table_swallow_plan.json）
  python3 fix_table_swallow.py --apply    # 写盘（备份 MD + UPDATE 库 content，id 不变 + FTS rebuild + WAL checkpoint）
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ..consts import STANDARDS_DIR as DATA_DIR

DB = os.path.join(str(DATA_DIR), "standards.db")
OUT = "/tmp/table_swallow_plan.json"
# 拆行续行：表号被切到下一行。⚠ 必须允许前缀 `#`（"## 7.1.6的要求：" 这类"标题式续行"），
# 否则这类会被当成误报跳过（实测：GB 50011-2010 §7.1.6 因此漏修，表一直不在库里）。
SPLIT = re.compile(r"^#{0,6}\s*(\d{1,2}\.\d{1,2}(?:-\d+)?(?:\.\d{1,2})?)(?=[^\s\d.．])")


def load_stdq():
    spec = importlib.util.spec_from_file_location("stdq", os.path.join(HERE, "stdq.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def norm(s):
    return "".join((s or "").split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    stdq = load_stdq()
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()
    mds = dict(cur.execute("select code, md_file from standards").fetchall())

    cands = cur.execute(
        "select std_code, clause_no, id, content from clauses where rtrim(content) like '%表'"
    ).fetchall()

    plan, review = [], []
    for code, no, cid, content in cands:
        mdf = mds.get(code)
        if not mdf or not os.path.exists(mdf):
            review.append({"std": code, "clause": no, "why": "无 md_file"})
            continue
        text = open(mdf, encoding="utf-8", errors="replace").read()
        lines = text.split("\n")
        base = os.path.basename(mdf)
        tail = content.rstrip()[-60:]
        p = next((i for i, ln in enumerate(lines) if tail[:40] in ln), None)
        if p is None:
            review.append({"std": code, "clause": no, "why": "MD 里找不到库尾片段（多为误报：结尾本来就是词）"})
            continue
        k = p + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k >= len(lines):
            review.append({"std": code, "clause": no, "why": "拆行点在文件末"})
            continue
        m = SPLIT.match(lines[k].strip())
        if not m:
            review.append({"std": code, "clause": no, "why": "下一行不是表号续行（误报）"})
            continue
        # ⚠ 不断言“续行数字 == 本条文号”：条文常引用**别章的表**（如 §3.7.3 引 `表3.2.2`）。
        #   对错由下面的“超集 + 整本条数不变 + 副作用”三道验证定。
        probe = lines[:]
        probe[p] = lines[p].rstrip() + re.sub(r"^#{1,6}\s*", "", lines[k].strip())   # 去续行前的 ## 
        del probe[k]
        old_parse = dict(stdq.extract_clauses(text, base))
        new_parse = dict(stdq.extract_clauses("\n".join(probe), base))
        n_new, n_old = norm(new_parse.get(no, "")), norm(old_parse.get(no, ""))
        item = {
            "std": code, "clause": no, "id": cid, "md": mdf,
            "line_prev": p + 1, "line_split": k + 1,
            "db_len": len(norm(content)), "parse_before": len(n_old), "parse_after": len(n_new),
        }
        if n_new.startswith(norm(content)) and len(n_new) > len(norm(content)):
            if len(new_parse) != len(old_parse):
                review.append({"std": code, "clause": no,
                               "why": f"并后条数变了（{len(old_parse)}→{len(new_parse)}），人工看"})
                continue
            side = []
            for cno, after in new_parse.items():
                before = old_parse.get(cno)
                r = cur.execute("select content from clauses where std_code=? and clause_no=?", (code, cno)).fetchone()
                dbc = r[0] if r else None
                if dbc is not None and before is not None and cno != no \
                        and norm(before) == norm(dbc) and norm(after) != norm(before):
                    side.append(cno)
            item["side_effects"] = side
            item["new_content"] = new_parse[no]
            plan.append(item)
        else:
            review.append({"std": code, "clause": no,
                           "why": f"并后非超集/未变长（库{len(norm(content))}字，新解析{len(n_new)}字）"})

    by_std = {}
    for it in plan:
        by_std.setdefault(it["std"], []).append(it["clause"])
    print(f"★ 可修 {len(plan)} 条（{len(by_std)} 本规范）；另需人工 {len(review)} 条")
    for code in sorted(by_std):
        print(f"  {code}: {len(by_std[code])} 条 → {', '.join(sorted(by_std[code]))}")
    for it in plan:
        if it["side_effects"]:
            print(f"⚠️ {it['std']} §{it['clause']} 副作用（同本其它条也会变）: {it['side_effects']}")
    json.dump({"plan": plan, "review": review}, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"计划已写 {OUT}")

    if args.apply and plan:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for code in sorted(by_std):
            items = [it for it in plan if it["std"] == code]
            mdf = items[0]["md"]
            shutil.copy2(mdf, f"{mdf}.bak.{stamp}-tableswallow")
            lines = open(mdf, encoding="utf-8").read().split("\n")
            n_merged = 0
            for it in sorted(items, key=lambda x: -x["line_split"]):   # 倒序，避免行号位移
                p2, k2 = it["line_prev"] - 1, it["line_split"] - 1
                # 锚定复核：上一行仍以「表」结尾 + 续行仍是表号续行（**不要求数字等于本条文号**：
                # 条文常引别章的表，如 §3.7.3 引 `表3.2.2`）
                if not (k2 < len(lines) and SPLIT.match(lines[k2].strip())
                        and lines[p2].rstrip().endswith("表")):
                    print(f"  ⚠️ {code} §{it['clause']} 行号锚不住，跳过")
                    continue
                lines[p2] = lines[p2].rstrip() + re.sub(r"^#{1,6}\s*", "", lines[k2].strip())  # 去续行前的 ##
                del lines[k2]
                n_merged += 1
            open(mdf, "w", encoding="utf-8").write("\n".join(lines))
            fresh = dict(stdq.extract_clauses("\n".join(lines), os.path.basename(mdf)))
            n_upd = 0
            for it in items:
                newc = fresh.get(it["clause"])
                if newc is None or not norm(newc).startswith(norm(it["new_content"])[:200]):
                    print(f"  ⚠️ {code} §{it['clause']} 重解析对不上，跳过写库")
                    continue
                cur.execute("update clauses set content=? where id=?", (newc, it["id"]))
                n_upd += 1
            print(f"  ✔ {code}: MD 实际并 {n_merged}/{len(items)} 处 / 写库 {n_upd} 条（id 不变）")
        cur.execute("INSERT INTO clauses_fts(clauses_fts) VALUES('rebuild')")
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"FTS rebuild + WAL checkpoint 完成；MD 备份后缀 {stamp}")
        print("收尾还需：embed_patch.py 刷这些条 + 重启 daemon + 同步到备份/远端")
    con.close()


if __name__ == "__main__":
    main()
