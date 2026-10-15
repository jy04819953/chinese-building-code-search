#!/usr/bin/env python3
"""定点刷新语义向量（不跑全量 embed-build）

用途：只改了**少数几条**条文的 content（如剪掉粘连尾巴、定点补条文）时，不必花 30~100 分钟
      重跑全量；本脚本读现成的 ids.npy / embeddings.npy，只重算指定条文的向量并原地替换。

硬前提（不满足就别用，改回全量）：
  1. **模型没换**——换模型则旧向量与新向量不在同一坐标系，必须全量。
  2. **没有跑过 rebuild**——rebuild 是"删了重插"，条文 id 会重排，向量文件按 id 对齐会错位。
  3. **模型与文本拼接方式必须与全量脚本一致**：`embed_build.py` 用
     `SentenceTransformer('BAAI/bge-base-zh-v1.5')` + `f'{std_code} §{clause_no} {content} {note or ""}'`
     + `model.encode(texts, batch_size=64)`（**不归一化**）。本脚本照抄这三处。

用法：
  python3 embed_patch.py --dry-run                    # 只报告将替换哪些行
  python3 embed_patch.py --ids 2788511 2788512        # 按条文 id
  python3 embed_patch.py --clauses JGJ-336-2016:6.3.1 GB-T-50504-2009:3.5.14
  python3 embed_patch.py --std JGJ-336-2016           # 整本刷新（用于批量小改后）
执行前请先 `systemctl stop stdq-daemon`（daemon 会读这两个文件），跑完再 start。
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ..consts import STANDARDS_DIR as DATA_DIR

DATA = str(DATA_DIR)
DB = os.path.join(DATA, 'standards.db')
EMB_DIR = os.path.join(DATA, 'embeddings')
MODEL = 'BAAI/bge-base-zh-v1.5'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ids', type=int, nargs='*', default=[])
    ap.add_argument('--clauses', nargs='*', default=[], help='形如 GB-50010-2010:6.7.12')
    ap.add_argument('--std', nargs='*', default=[], help='整本刷新')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    import numpy as np
    ids = np.load(os.path.join(EMB_DIR, 'ids.npy'))
    emb = np.load(os.path.join(EMB_DIR, 'embeddings.npy'))
    idx = {int(v): i for i, v in enumerate(ids)}

    con = sqlite3.connect(DB)
    cur = con.cursor()
    want_ids = set(args.ids)
    for pair in args.clauses:
        code, no = pair.rsplit(':', 1)
        r = cur.execute('SELECT id FROM clauses WHERE std_code=? AND clause_no=?', (code, no)).fetchone()
        if r:
            want_ids.add(int(r[0]))
        else:
            print(f'⚠️ 库中找不到 {code} §{no}')
    for code in args.std:
        for (i,) in cur.execute('SELECT id FROM clauses WHERE std_code=?', (code,)):
            want_ids.add(int(i))

    rows = []
    for i in sorted(want_ids):
        r = cur.execute('SELECT std_code, clause_no, content, note FROM clauses WHERE id=?', (i,)).fetchone()
        if not r:
            print(f'⚠️ id={i} 不在库里，跳过')
            continue
        if i not in idx:
            print(f'⚠️ id={i} 不在向量文件里（新增条文？需要全量补），跳过')
            continue
        rows.append((i, r[0], r[1], r[2], r[3] or ''))
    print(f'将重算 {len(rows)} 条；向量文件共 {len(ids)} 条（形状不变：{emb.shape}）')
    for i, c, n, _ct, _nt in rows[:20]:
        print(f'   id={i} {c} §{n}')
    if len(rows) > 20:
        print(f'   ... 其余 {len(rows) - 20} 条')
    if args.dry_run or not rows:
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL, cache_folder=os.path.expanduser('~/.cache/huggingface'))
    texts = [f'{c} §{n} {ct} {nt}' for _i, c, n, ct, nt in rows]
    new = model.encode(texts, batch_size=64, show_progress_bar=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for f in ('ids.npy', 'embeddings.npy'):
        shutil.copy2(os.path.join(EMB_DIR, f), os.path.join(EMB_DIR, f'{f}.bak.{stamp}-patch'))
    for k, (i, _c, _n, _ct, _nt) in enumerate(rows):
        emb[idx[i]] = new[k]
    np.save(os.path.join(EMB_DIR, 'embeddings.npy'), emb)
    np.save(os.path.join(EMB_DIR, 'ids.npy'), ids)
    print(f'✔ 已原地替换 {len(rows)} 条（形状 {emb.shape}）；备份后缀 {stamp}-patch')
    print('  记得：systemctl start stdq-daemon，然后跑 stdq_healthcheck.py')
    con.close()


if __name__ == '__main__':
    main()
