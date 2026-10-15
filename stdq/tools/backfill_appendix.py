# -*- coding: utf-8 -*-
"""附录条目「定点补库」：只 INSERT 新增的附录条目，不动现有条文、不 rebuild。
用法: python3 backfill_appendix.py <std_code> [<std_code> ...] [--apply]
"""
import os, re, sys, sqlite3, shutil, datetime
from ..consts import STANDARDS_DIR

SC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SC)
import stdq

APPLY = '--apply' in sys.argv
codes = [a for a in sys.argv[1:] if not a.startswith('--')]
D = STANDARDS_DIR
DB = os.path.join(D, 'standards.db')
APX_RE = re.compile(r'^[A-Z]\s*\.\s*\d+\.\d+$')
SEC_RE = re.compile(r'^[A-Z]\s*\.\s*\d+$')

con = sqlite3.connect(DB)
cur = con.cursor()
if APPLY:
    bak = DB + '.bak.' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-apx'
    shutil.copy2(DB, bak)
    print('DB 备份 ->', os.path.basename(bak))

all_new = 0
for code in codes:
    cur.execute("SELECT md_file FROM standards WHERE code=?", (code,))
    r = cur.fetchone()
    if not r or not r[0]:
        print(f'!! {code}: 库里无 md_file 记录'); continue
    mf = r[0]
    p = os.path.join(D, mf)
    if not os.path.exists(p):
        print(f'!! {code}: MD 不存在 {p}'); continue
    md = open(p, encoding='utf-8', errors='ignore').read()
    got = stdq.extract_clauses(md, mf)
    apx = [(k, v) for k, v in got if k.startswith('附录') or APX_RE.match(k)]
    cur.execute("SELECT clause_no FROM clauses WHERE std_code=?", (code,))
    have = set(x[0] for x in cur.fetchall())
    cur.execute("SELECT applicable_type, COUNT(*) FROM clauses WHERE std_code=? GROUP BY 1 ORDER BY 2 DESC LIMIT 1", (code,))
    app = (cur.fetchone() or ('通用', 0))[0]
    new = [(k, v) for k, v in apx if k not in have]
    # 已存在的附录条目：解析器改了（断行/拆粘连/去重）后内容会变 → 一并更新，避免旧内容留吞并文本
    stale = []
    for k, v in apx:
        if k in have:
            r = cur.execute("SELECT content FROM clauses WHERE std_code=? AND clause_no=?", (code, k)).fetchone()
            if r and r[0] != v:
                stale.append((k, len(r[0]), len(v)))
    print(f'\n{code}: 附录条目 {len(apx)} 条，库中已有 {len(apx) - len(new)}，待补 {len(new)}，内容需更新 {len(stale)}')
    for k, v in new[:6]:
        print(f'    + {k:10s} {v[:52]!r}')
    if len(new) > 6:
        print(f'    ... 其余 {len(new) - 6} 条')
    all_new += len(new)
    if APPLY:
        for k, v in new:
            st = 1 if stdq.is_strict_clause(v) else 0
            cur.execute('INSERT INTO clauses (std_code, clause_no, content, is_strong, is_strict, applicable_type)'
                        ' VALUES (?, ?, ?, 0, ?, ?)', (code, k, v, st, app))
        for k, _o, _n in stale:
            v = dict(apx)[k]
            cur.execute('UPDATE clauses SET content=? WHERE std_code=? AND clause_no=?', (v, code, k))
        # 节级（如 D.1）写入章节目录，父 = 附录X
        secs = []
        for k, v in got:
            if SEC_RE.match(k):
                letter = k.split('.')[0].strip()
                secs.append((k, f'附录{letter}'))
        for k, parent in secs:
            cur.execute('INSERT OR IGNORE INTO clause_chapters (std_code, clause_no, title, level, parent_clause_no)'
                        ' VALUES (?, ?, ?, "section", ?)', (code, k, '', parent))
        print(f'    已 INSERT {len(new)} 条条文 + {len(secs)} 条节级章节')
        n_written = 0
        for cn, nt, _cn in stdq.extract_appendix_notes(md):
            cur.execute('UPDATE clauses SET note=?, has_note=1 WHERE std_code=? AND clause_no=?', (nt, code, cn))
            n_written += cur.rowcount
        print(f'    已写入附录条文说明 {n_written} 条')

if APPLY:
    cur.execute('INSERT INTO clauses_fts(clauses_fts) VALUES("rebuild")')
    con.commit()
    cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    cur.execute("SELECT COUNT(*) FROM clauses")
    print('\n条文总数:', cur.fetchone()[0])
    print('FTS 重建 + WAL checkpoint 完成')
else:
    print(f'\n[DRY-RUN] 合计待补 {all_new} 条，未写库。加 --apply 执行。')
con.close()
