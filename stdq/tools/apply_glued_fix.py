#!/usr/bin/env python3
"""配合 fix_glued_clause_lines.py：「条号粘在上一句末尾」修完 MD 后，把库与向量同步过去。

做三件事（一次跑完）：
  ① 库：上一条 content 更新为"新解析内容"（只搬走尾巴）；该条号库里没有→ INSERT（适用类型沿用本本），
     库里已有但内容是说明区残文→ 覆盖（覆盖前把旧内容打出来人核）。
  ② FTS rebuild + WAL checkpoint。
  ③ 向量：已存在的 id 原地替换；新条 id 追加到 ids.npy / embeddings.npy（embed_patch.py 只支持替换，
     不支持新增，所以新增必须走这里）。模型与文本格式同 embed_patch.py（BAAI/bge-base-zh-v1.5，`code §no content note`）。

安全：dry 阶段**全部在内存验证**（内存拆行 → 重解析 → 与库逐条对关系），有任一异常就整体中止不写；
      写前备份库与两个 npy（后缀 .bak.<时间>-gluedfix）。

用法（前置：daemon 已停；MD 侧已跑过 fix_glued_clause_lines.py --apply）：
    python3 apply_glued_fix.py            # 预演（打印将做的 20 项）
    python3 apply_glued_fix.py --apply    # 写 MD(若还没写)+库+向量
收尾：systemctl start stdq-daemon → stdq_healthcheck.py → 同步到备份/远端（库 + 2 个 npy + 改过的 MD）
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..consts import STANDARDS_DIR as DATA_DIR
from ..parser.clauses import extract_clauses

S = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DATA_DIR, "standards.db")
EMB = os.path.join(DATA_DIR, "embeddings")
MODEL = "BAAI/bge-base-zh-v1.5"
PLAN = "/tmp/glued_split_plan.json"
pass
pass


def nz(s):
    return "".join((s or "").split())


def split_lines(lines, line_no, no):
    i = line_no - 1
    l = lines[i]
    pos = l.find(no)
    if pos < 0 or not l[:pos].rstrip().endswith(("。", "；", ";")):
        return None
    return lines[:i] + [l[:pos].rstrip(), l[pos:].strip()] + lines[i + 1:]


def main():
    app = "--apply" in sys.argv
    if not os.path.exists(PLAN):
        sys.exit(f"✋ 找不到清单 {PLAN}；先跑 fix_glued_clause_lines.py 生成")
    plan = json.load(open(PLAN))
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()
    ops, bad = [], []
    for p in plan:
        code, no, prev = p["std"], p["clause"], p["prev"]
        lines = open(p["md"], encoding="utf-8", errors="replace").read().split("\n")
        sp = split_lines(lines, p["line"], no)
        if sp is None:
            bad.append((code, no, "锚定复核失败（行内找不到该号 / 前缀未以句读结尾）"))
            continue
        new = dict(extract_clauses("\n".join(sp), os.path.basename(p["md"])))
        if no not in new or len(nz(new[no])) < 8:
            bad.append((code, no, "拆行后仍不成条"))
            continue
        if prev:
            r = cur.execute("select id, content from clauses where std_code=? and clause_no=?",
                            (code, prev)).fetchone()
            if not r:
                bad.append((code, no, f"库里找不到上一条 §{prev}"))
                continue
            if not nz(r[1]).startswith(nz(new[prev])):
                bad.append((code, no, f"上一条§{prev}与新解析不是『只搬走尾巴』"))
                continue
            ops.append({"kind": "upd", "id": r[0], "std": code, "clause": prev, "line": p["line"],
                        "content": new[prev], "old_len": len(nz(r[1])), "new_len": len(nz(new[prev]))})
        got = cur.execute("select id, content from clauses where std_code=? and clause_no=?",
                          (code, no)).fetchone()
        if got and nz(got[1]) != nz(new[no]):
            ops.append({"kind": "rep", "id": got[0], "std": code, "clause": no, "line": p["line"],
                        "content": new[no], "old_len": len(nz(got[1])), "new_len": len(nz(new[no])),
                        "old_head": nz(got[1])[:60]})
        elif not got:
            ops.append({"kind": "ins", "std": code, "clause": no, "line": p["line"],
                        "content": new[no], "old_len": 0, "new_len": len(nz(new[no]))})

    kinds = {"upd": "上一条缩减", "rep": "覆盖错内容", "ins": "新增条文"}
    print(f"★ 待处理 {len(ops)} 项（" + "、".join(f"{kinds[k]} {sum(1 for o in ops if o['kind'] == k)}"
                                                for k in kinds if any(o["kind"] == k for o in ops))
          + f"）；异常 {len(bad)}")
    for o in ops:
        extra = f"  ← 覆盖前是：{o['old_head']}…" if o["kind"] == "rep" else ""
        print(f"   {kinds[o['kind']]:6s} {o['std']:22s} §{o['clause']:9s} {o['old_len']:5d}→{o['new_len']:5d}字{extra}")
    for b in bad:
        print("   ✗", b)
    if bad:
        print("✋ 有异常项，按纪律中止（不写盘）")
        raise SystemExit(1)
    if not app:
        print("（预演通过；加 --apply 才写 MD + 库 + 向量）")
        con.close()
        raise SystemExit(0)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for md in sorted({p["md"] for p in plan}):
        lines = open(md, encoding="utf-8", errors="replace").read().split("\n")
        need = False
        for p in sorted([x for x in plan if x["md"] == md], key=lambda x: -x["line"]):
            sp = split_lines(lines, p["line"], p["clause"])
            if sp is None:
                print(f"   ⚠️ {p['std']} §{p['clause']} 拆行锚定失败，跳过")
                continue
            lines, need = sp, True
        if not need:
            continue
        if not os.path.exists(f"{md}.bak.{stamp}-gluedsplit"):
            shutil.copy2(md, f"{md}.bak.{stamp}-gluedsplit")
        open(md, "w", encoding="utf-8").write("\n".join(lines))
    print(f"✔ MD 已拆行：{len({p['md'] for p in plan})} 本")

    shutil.copy2(DB, f"{DB}.bak.{stamp}-gluedfix")
    new_ids = []
    for o in ops:
        if o["kind"] in ("upd", "rep"):
            cur.execute("update clauses set content=? where id=?", (o["content"], o["id"]))
        else:
            at = cur.execute("select applicable_type from clauses where std_code=? limit 1",
                             (o["std"],)).fetchone()
            cur.execute("insert into clauses (std_code, clause_no, content, is_strong, has_note, note, themes,"
                        " line_in_md, is_strict, applicable_type) values (?,?,?,0,0,NULL,NULL,?,0,?)",
                        (o["std"], o["clause"], o["content"], o["line"], at[0] if at else "通用"))
            o["id"] = cur.lastrowid
            new_ids.append(cur.lastrowid)
    cur.execute("INSERT INTO clauses_fts(clauses_fts) VALUES('rebuild')")
    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"✔ 库已写：条文总数 {cur.execute('select count(*) from clauses').fetchone()[0]}（新增 {len(new_ids)}）")

    import numpy as np  # noqa: E402

    ids = np.load(os.path.join(EMB, "ids.npy"))
    emb = np.load(os.path.join(EMB, "embeddings.npy"))
    for f in ("ids.npy", "embeddings.npy"):
        shutil.copy2(os.path.join(EMB, f), os.path.join(EMB, f"{f}.bak.{stamp}-gluedfix"))
    idx = {int(v): i for i, v in enumerate(ids)}
    rows = [cur.execute("select std_code, clause_no, content, note from clauses where id=?",
                        (o["id"],)).fetchone() for o in ops]
    from sentence_transformers import SentenceTransformer  # noqa: E402

    model = SentenceTransformer(MODEL, cache_folder=os.path.expanduser("~/.cache/huggingface"))
    vecs = model.encode([f"{c} §{n} {ct} {nt}" for c, n, ct, nt in rows], batch_size=64,
                        show_progress_bar=True)
    add = [(o["id"], vecs[k]) for k, o in enumerate(ops) if int(o["id"]) not in idx]
    for k, o in enumerate(ops):
        if int(o["id"]) in idx:
            emb[idx[int(o["id"])]] = vecs[k]
    if add:
        ids = np.concatenate([ids, np.array([a[0] for a in add], dtype=ids.dtype)])
        emb = np.concatenate([emb, np.array([a[1] for a in add], dtype=emb.dtype)], axis=0)
    np.save(os.path.join(EMB, "ids.npy"), ids)
    np.save(os.path.join(EMB, "embeddings.npy"), emb)
    print(f"✔ 向量：替换 {len(ops) - len(add)}、追加 {len(add)}；现共 {len(ids)} 行（{emb.shape}）")
    print(f"备份后缀 {stamp}；收尾：systemctl start stdq-daemon → stdq_healthcheck.py → 同步到备份/远端")
    con.close()



if __name__ == '__main__':
    main()