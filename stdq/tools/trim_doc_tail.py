#!/usr/bin/env python3
"""trim_doc_tail.py — 剪掉条文内容里粘着的「标准尾部整块」（用词说明 / 引用标准名录 / 参考文献）。

背景：这些整块不属于任何条文；解析器 起遇到它们的标题行就停（`stdq.RE_DOC_TAIL_HEAD`），
但**库里历史数据**还粘着（全库曾 58 条 / 52 本，如 GB 50011-2010 §M.3.2 尾粘
"## 本规范用词说明 1 为了便于在执行本规范条文时区别对待…"）。本脚本按新解析把尾巴剪掉。

安全闸（三条同时满足才更新，只剪不增）：
  ① 归一化后「库内容 startswith 新解析」；
  ② 库比新解析长；
  ③ **被剪掉的那截确实以尾部块开头**（用词说明/引用标准名录/参考文献，或 `1为便于…`/`1）表示很严格…`
     这类正文首句；允许前置 `#` 与标点）。
⚠️ 只走①②会误剪「库长 MD 短」的历史补齐类（如 GB 50015-2019 §5.3.16 库里正确的 `0.75m/s。`）——
   实测：只按①②出 105 条误判，加③后收敛到 58 条真目标、47 条自动跳过。

用法：
  python3 trim_doc_tail.py            # 预览（打印每条被剪片段开头）
  python3 trim_doc_tail.py --apply    # 写盘（备份库 + UPDATE content，id 不变 + FTS rebuild + WAL checkpoint）
收尾：embed_patch.py 刷这些条 + 重启 daemon + 同步到备份/远端。

规则④（内部截断）：尾部块标记出现在内容中间（非简单前缀关系，如 `## 本规范用词说明` 夹在表格之后）
⇒ 从标记处截断，要求「截断结果 == 新解析」才写（GB-50974 附录G / JGJ-298 附录A 实测属此类）。
⚠️ 编写本类脚本必遵：**外层结果集先 fetchall()**（见 main() 里的注释），否则静默漏扫。
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

from ..consts import STANDARDS_DIR as DATA_DIR  # noqa: E402
from ..parser import clauses as stdq
from ..parser import common as _stdq_common
stdq._is_clause_number = _stdq_common._is_clause_number
from ..consts import RE_DOC_TAIL_HEAD as _RE_DOC_TAIL_HEAD
stdq.RE_DOC_TAIL_HEAD = _RE_DOC_TAIL_HEAD


DB = os.path.join(str(DATA_DIR), "standards.db")

PLAN_TAIL = re.compile(
    r'^(?:#{1,6})?\s*[；;，,、。：:)）\]]*\s*'
    r'(?:本(?:规范|标准|规程|导则|办法)用词说明|用词说明|引用标准名录|参考文献'
    r'|[12][）)]?\s*为便于|[12][）)]?\s*为了便于|[12][）)]?\s*表示很严格|[12]\s*条文中指明)')

# 规则④：尾部块/封面标记出现在内容**内部**（不是简单前缀关系，例如库把“## 本规范用词说明”
# 夹在表格之后的中间位置）⇒ 从该标记处截断，并要求“截断结果 == 新解析”（归一化）才写。
# 实测：GB-50974 附录G、JGJ-298 附录A 属此类（严格前缀规则③抓不到）。
INLINE_TAIL = re.compile(
    r'#{0,6}\s*(?:本(?:规范|标准|规程|导则|办法)用词说明|用词说明|引用标准名录|参考文献'
    r'|中华人民共和国(?:国家|行业)标准|附[:：]?条文说明)')


def norm(s):
    return "".join((s or "").split())


def tail_is_doc_block(tail: str) -> bool:
    return bool(PLAN_TAIL.match(tail))




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()

    plan, manual = [], []
    # ⚠️ 游标陷阱：外层结果集必须先 fetchall()。若边遍历 `cur.execute(...)` 边在循环体内用
    #    同一 cursor 查 clauses，外层结果集会被冲掉 —— 本脚本初版就因此只处理了第 1 本、静默报 0 条。
    stds = cur.execute("select code, md_file from standards order by code").fetchall()
    for code, mdf in stds:
        if not mdf or not os.path.exists(mdf):
            continue
        text = open(mdf, encoding="utf-8", errors="replace").read()
        new = dict(stdq.extract_clauses(text, os.path.basename(mdf)))
        for no, n in new.items():
            r = cur.execute("select id, content from clauses where std_code=? and clause_no=?", (code, no)).fetchone()
            if not r:
                continue
            cid, db = r
            nd, nn = norm(db), norm(n)
            if nd == nn or not nd.startswith(nn) or len(nd) <= len(nn):
                continue
            tail = nd[len(nn):]
            if tail_is_doc_block(tail):
                plan.append({"std": code, "clause": no, "id": cid, "mode": "前缀剪尾", "cut": len(nd) - len(nn),
                             "db_len": len(nd), "new_len": len(nn),
                             "tail": tail[:70].replace("\n", "⏎"), "new": n})
                continue
            # 规则④：尾部块标记在内容内部 → 从标记处截断，须「截断结果 == 新解析」
            m = INLINE_TAIL.search(db)
            if m and m.start() > 0 and nn:
                cut = db[:m.start()].rstrip()
                if norm(cut) == nn:
                    plan.append({"std": code, "clause": no, "id": cid, "mode": "内部截断",
                                 "cut": len(nd) - len(nn), "db_len": len(nd), "new_len": len(nn),
                                 "tail": db[m.start():m.start() + 70].replace("\n", "⏎"), "new": cut})
                    continue
            manual.append({"std": code, "clause": no, "db_len": len(nd), "parse_len": len(nn),
                           "tail": nd[len(nn):][:60].replace("\n", "⏎")})

    print(f"★ 可剪尾 {len(plan)} 条（{len({p['std'] for p in plan})} 本）；按第三条闸门跳过 {len(manual)} 条")
    for p in plan:
        print(f"   [{p.get('mode','前缀剪尾')}] {p['std']:22s} §{p['clause']:12s} {p['db_len']:6d} → {p['new_len']:6d}（剪 {p['cut']:6d}）被剪: 「{p['tail']}」")
    if manual:
        print("\n跳过（库内容不是新解析的超集，或剪掉的那截不是尾部块 → 多为「库长 MD 短」的历史补齐类）：")
        for m in manual[:20]:
            print(f"   {m['std']} §{m['clause']} 库{m['db_len']}/解析{m['parse_len']} 尾: 「{m['tail']}」")

    if a.apply and plan:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB, f"{DB}.bak.{stamp}-trimtail")
        for p in plan:
            cur.execute("update clauses set content=? where id=?", (p["new"], p["id"]))
        cur.execute("INSERT INTO clauses_fts(clauses_fts) VALUES('rebuild')")
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        json.dump(plan, open("/tmp/trim_doc_tail_plan.json", "w"), ensure_ascii=False, indent=1)
        print(f"\n✔ 已更新 {len(plan)} 条 content（id 不变）+ FTS rebuild + WAL checkpoint")
        print(f"库备份: {DB}.bak.{stamp}-trimtail；计划: /tmp/trim_doc_tail_plan.json")
        print("收尾：embed_patch.py 刷这些条 + 重启 daemon + 同步到备份/远端")
    con.close()


if __name__ == "__main__":
    main()
