# -*- coding: utf-8 -*-
"""全库附录抽取稳定性交叉验证：同一本规范重复抽取结果是否稳定。
对每本有附录的规范，用「独立正则扫 MD 行首编号」vs「解析器 extract_clauses 输出」对比，
列出不一致的书 + 缺/多的条号。只读，不写库。
用法: python3 apx_crosscheck.py [std_code ...]
"""
import os, re, sys, sqlite3
from ..consts import STANDARDS_DIR

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..parser.appendix import extract_appendix_clauses, find_appendix_start
from ..consts import RE_APX_STOP

D = STANDARDS_DIR
cur = sqlite3.connect(os.path.join(D, 'standards.db')).cursor()
codes = sys.argv[1:] or [r[0] for r in cur.execute("SELECT code FROM standards").fetchall()]

CL_RE = re.compile(r'^#{0,4}\s*([A-Z])\s*[.．]\s*(\d{1,2})\s*[.．]\s*(\d{1,2})'
                   r'(?!\s*[.．]\s*\d)(?![0-9\-–])')
HEAD_RE = re.compile(r'^#{0,4}\s*附录\s*[A-Z]')
bad = []
for code in codes:
    row = cur.execute("SELECT md_file FROM standards WHERE code=?", (code,)).fetchone()
    if not row or not row[0]:
        continue
    p = os.path.join(D, row[0])
    if not os.path.exists(p):
        continue
    md = open(p, encoding='utf-8', errors='ignore').read()
    lines = md.split('\n')
    start = find_appendix_start(lines)
    if start is None:
        continue
    stop = next((i for i in range(start, len(lines)) if RE_APX_STOP.match(lines[i].strip())), len(lines))
    seg = lines[start:stop]
    md_cl = set()
    n_head = 0
    for s in (x.strip() for x in seg):
        if HEAD_RE.match(s) and '…' not in s:
            n_head += 1
        m = CL_RE.match(s)
        if m:
            md_cl.add(f'{m.group(1)}.{m.group(2)}.{m.group(3)}')
    got = extract_appendix_clauses(md)
    db_cl = set(k for k, _ in got if not k.startswith('附录'))
    miss, extra = md_cl - db_cl, db_cl - md_cl
    status = 'OK ' if not miss and not extra else '!! '
    print(f'{status}{code:22s} MD: 标题{n_head:3d} 条{len(md_cl):4d} | 解析器: 章{sum(1 for k,_ in got if k.startswith("附录")):3d} 条{len(db_cl):4d}'
          + (f' | 缺 {sorted(miss)[:6]}' if miss else '') + (f' | 多 {sorted(extra)[:6]}' if extra else ''))
    if miss or extra:
        bad.append((code, sorted(miss), sorted(extra)))
print(f'\n== 有附录且不一致的规范: {len(bad)} 本 ==')
for code, miss, extra in bad:
    print(f'  {code}: 缺 {len(miss)} {miss[:8]} / 多 {len(extra)} {extra[:8]}')
