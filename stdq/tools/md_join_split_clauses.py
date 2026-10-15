#!/usr/bin/env python3
"""把 MD 里"被拆成多行"的条文正文并回一行（治 rebuild 会退化的根）

背景：`verify_md_vs_db.py` 报「不一致、库比解析长」的那批（全库 278 条），是 MD 里条文正文
      被换行/空行/公式/表格/注拆成多行，解析器只收到第一行（或前几行）。库里完整是当年
      `fix_truncated_clauses.py` 直接补库的结果，MD 从没跟着改 ⇒ 整库 rebuild 又会退回断句。

做法（**先验证后写盘**，跟"12 条"那批同一手法）：
  1. 定位条文行；取到"下一个条文号 / 章节标题"之前的全部非空行，拼成一行；
  2. **内存里**把这行替换进 MD 文本，重新解析该规范，比较**该条**解析结果与库内容（归一化）；
  3. 逐字相同 ⇒ 判定可并（写盘时按同一区间删行，倒序处理防位移）；不同 ⇒ 记 review，不动。

用法：
  python3 md_join_split_clauses.py                    # 预览：库比解析长 ≥200 字的
  python3 md_join_split_clauses.py --min-gap 0        # 预览：全部不一致的
  python3 md_join_split_clauses.py --min-gap 0 --apply
  python3 md_join_split_clauses.py --std GB-50028-2006
输出 /tmp/md_join_plan.json
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
from ..consts import STANDARDS_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DB = 'standards.db'
OUT = '/tmp/md_join_plan.json'
CL = re.compile(r'^(?:#{1,6}\s*)?(?:第\s*)?'
                r'(\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3}'
                r'|附录\s*[A-Z]\d?'
                r'|[A-Z]\s*[.．]\s*\d{1,2}(?:\s*[.．]\s*\d{1,2}){0,2})'
                r'(?:\s*条)?[\s\u3000]*(.*)$')


def load_stdq():
    spec = importlib.util.spec_from_file_location('stdq', os.path.join(HERE, 'stdq.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def norm(s):
    return ''.join((s or '').split())


def canon(no):
    return re.sub(r'[\s.．]', '', no)


def find_line(lines, no):
    want = canon(no)
    for i, l in enumerate(lines):
        m = CL.match(l.strip())
        if m and canon(m.group(1)) == want:
            return i
    return None


def join_body(lines, i):
    """返回 (要并的行区间末行索引, 拼接后的行)。区间 = i .. 下一个条文号/标题前"""
    parts = [lines[i].rstrip()]
    j = i + 1
    last = i
    while j < len(lines):
        s = lines[j].strip()
        if not s:
            j += 1
            continue
        if s.startswith('#') or (CL.match(s) and (CL.match(s).group(2) or '').strip()):
            break
        parts.append(s)
        last = j
        j += 1
    merged = parts[0] + ''.join(' ' + p for p in parts[1:])
    return last, merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--std', default=None)
    ap.add_argument('--min-gap', type=int, default=200)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    stdq = load_stdq()
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()
    stds = cur.execute('SELECT code, md_file FROM standards ORDER BY code').fetchall()

    plan, review, nolines = [], [], 0
    for code, mdf in stds:
        if args.std and code != args.std:
            continue
        if not mdf or not os.path.exists(mdf):
            continue
        text = open(mdf, encoding='utf-8', errors='replace').read()
        base = os.path.basename(mdf)
        got = dict(stdq.extract_clauses(text, base))
        have = {r[0]: r[1] for r in cur.execute(
            'SELECT clause_no, content FROM clauses WHERE std_code=?', (code,))}
        targets = [no for no, db in have.items()
                   if no in got and len(db) - len(got[no]) >= args.min_gap and norm(db) != norm(got[no])]
        if not targets:
            continue
        lines = text.split('\n')
        for no in targets:
            i = find_line(lines, no)
            if i is None:
                review.append({'std': code, 'clause': no, 'why': 'MD 里找不到条文行'})
                continue
            last, merged = join_body(lines, i)
            if last == i:
                nolines += 1
                continue
            # 内存验证：替换这一行后重新解析
            probe = lines[:]
            probe[i] = merged
            del probe[i + 1:last + 1]
            new = dict(stdq.extract_clauses('\n'.join(probe), base))
            if norm(new.get(no, '')) == norm(have[no]):
                plan.append({'std': code, 'clause': no, 'line': i + 1, 'last': last + 1,
                             'n_extra': last - i, 'db_len': len(have[no]),
                             'md_len_before': len(got[no]), 'merged_len': len(merged)})
            else:
                review.append({'std': code, 'clause': no,
                               'why': '并完仍与库不一致（差 %d 字）'
                                      % (len(norm(have[no])) - len(norm(new.get(no, '')))),
                               'db_len': len(have[no]), 'md_len': len(got[no])})
    json.dump({'plan': plan, 'review': review, 'no_extra_line': nolines},
              open(OUT, 'w'), ensure_ascii=False, indent=1)
    print(f"★ 可安全并（并完 == 库）{len(plan)} 条；需人工看 {len(review)} 条；无续行可并 {nolines} 条")
    for it in plan[:20]:
        print(f"    ✔ {it['std']} §{it['clause']} 行{it['line']}~{it['last']} 吃{it['n_extra']}行"
              f" 库{it['db_len']}字（现解析{it['md_len_before']}）")
    for it in review[:15]:
        print(f"  ⚠️ {it['std']} §{it['clause']} {it['why']}")

    if args.apply and plan:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        for code in sorted({it['std'] for it in plan}):
            items = [it for it in plan if it['std'] == code]
            mdf = cur.execute('SELECT md_file FROM standards WHERE code=?', (code,)).fetchone()[0]
            shutil.copy2(mdf, f'{mdf}.bak.{stamp}-mdjoin')
            lines = open(mdf, encoding='utf-8').read().split('\n')
            for it in sorted(items, key=lambda x: -x['line']):
                i = find_line(lines, it['clause'])
                if i is None or i != it['line'] - 1:
                    print(f"  ⚠️ {code} §{it['clause']} 行号位移，跳过"); continue
                last, merged = join_body(lines, i)
                lines[i] = merged
                del lines[i + 1:last + 1]
            open(mdf, 'w', encoding='utf-8').write('\n'.join(lines))
            print(f"  ✔ {code}: 并 {len(items)} 条")
        print(f"备份后缀 {stamp}")
    con.close()


if __name__ == '__main__':
    main()
