# -*- coding: utf-8 -*-
"""补附录「节级」(X.N，如 D.1 一般规定) 到章节目录 clause_chapters，父 = 附录X。
用法: python3 backfill_apx_sections.py <std_code> [...] [--apply]
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
con = sqlite3.connect(DB)
cur = con.cursor()
if APPLY:
    bak = DB + '.bak.' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-apxsec'
    shutil.copy2(DB, bak)
    print('DB 备份 ->', os.path.basename(bak))
total = 0
for code in codes:
    cur.execute("SELECT md_file FROM standards WHERE code=?", (code,))
    r = cur.fetchone()
    if not r or not r[0]:
        continue
    p = os.path.join(D, r[0])
    lines = open(p, encoding='utf-8', errors='ignore').read().split('\n')
    st = stdq.find_appendix_start(lines)
    if st is None:
        print(f'{code}: 无附录区'); continue
    end = len(lines)
    for j in range(st + 1, len(lines)):
        if stdq.RE_APX_STOP.match(lines[j].strip()):
            end = j
            break
    secs, cur_apx = [], None
    for l in lines[st:end]:
        s = l.strip()
        mh = stdq.RE_APX_HEAD.match(s)
        if mh:
            cur_apx = f'附录{mh.group(1)}'
            continue
        ms = stdq.RE_APX_SEC.match(s)
        if ms:
            g = ms.groups()
            letter, num = (g[0], g[1]) if g[0] else (g[2], g[3])
            title = (g[4] or '').strip()
            secs.append((f'{letter}.{num}', title, f'附录{letter}'))
    cur.execute("SELECT clause_no FROM clause_chapters WHERE std_code=?", (code,))
    have = set(x[0] for x in cur.fetchall())
    new = [s for s in secs if s[0] not in have]
    print(f'{code}: 附录节级标题 {len(secs)} 个，新增 {len(new)}')
    for no, ti, pa in new[:8]:
        print(f'    + {no:6s} {ti[:30]:32s} 父={pa}')
    total += len(new)
    if APPLY:
        for no, ti, pa in new:
            cur.execute('INSERT OR IGNORE INTO clause_chapters (std_code, clause_no, title, level, parent_clause_no)'
                        ' VALUES (?, ?, ?, "section", ?)', (code, no, ti, pa))
if APPLY:
    con.commit()
    cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    print(f'\n写入 {total} 条章节行')
else:
    print(f'\n[DRY-RUN] 合计 {total} 条，未写库')
con.close()
