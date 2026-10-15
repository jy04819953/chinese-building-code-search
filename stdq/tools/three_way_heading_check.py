#!/usr/bin/env python3
"""三向对比定位「标题粘连」修复的影响面：旧解析 vs 新解析 vs 库

为什么不能只比「库 vs 新解析」：库里有一批**当年手工定点补全过**的条文（DB 比解析更全），
它们跟这次解析器改动无关，混在一起会把影响面算错（实测 331 条"需人工看"里绝大多数是这类）。

判定：
  · 旧解析 == 新解析        → 本次改动无影响（库若不同，属历史定点补全，另案）
  · 旧解析 != 新解析 == 库  → 库里已经是修好的样子（无需处理）
  · 旧解析 != 新解析 != 库  → 本次改动的正影响面：库内容仍带粘连 → 写 new
输出 /tmp/three_way.json + 摘要
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
from ..consts import STANDARDS_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DB = 'standards.db'
HEAD = re.compile(r'^#{1,6}\s')


def load_stdq():
    spec = importlib.util.spec_from_file_location('stdq', os.path.join(HERE, 'stdq.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def norm(s):
    return ''.join((s or '').split())


def make_collector(stdq, skip_hash):
    def coll(lines, start_idx):
        subitems = []
        j = start_idx
        while j < len(lines):
            line = lines[j].strip()
            if line and stdq._is_clause_number(line):
                break
            if line.startswith('# ') and not line.startswith('##'):
                if not re.match(r'^#{1,3}\s+\d{1,2}\.\d{1,2}', line):
                    break
            if not line:
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and stdq._is_clause_number(lines[k]):
                    break
                j += 1
                continue
            if line.startswith('>') or line.startswith('**注') or line.startswith('注：'):
                j += 1
                continue
            if skip_hash and HEAD.match(line):      # 新行为：标题行不并入正文
                j += 1
                continue
            is_new = bool(re.match(r'^\d+[\s\u3000]*[\u4e00-\u9fff#0-9a-zA-Z]', line))
            if is_new or not subitems:
                subitems.append(line)
            else:
                subitems[-1] += line
            j += 1
        return subitems, j - start_idx
    return coll


def main():
    stdq = load_stdq()
    old_coll, new_coll = make_collector(stdq, False), make_collector(stdq, True)
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()
    stds = cur.execute('SELECT code, md_file FROM standards ORDER BY code').fetchall()
    res = {'no_impact': 0, 'db_old': 0, 'already_fixed': 0,
           'to_update': [], 'affected_other': [], 'preexisting': []}
    for code, mdf in stds:
        if not mdf or not os.path.exists(mdf):
            continue
        text = open(mdf, encoding='utf-8', errors='replace').read()
        base = os.path.basename(mdf)
        stdq._collect_subitems = old_coll
        old = dict(stdq.extract_clauses(text, base))
        stdq._collect_subitems = new_coll
        new = dict(stdq.extract_clauses(text, base))
        have = {r[0]: r[1] for r in cur.execute(
            'SELECT clause_no, content FROM clauses WHERE std_code=?', (code,))}
        for no in new:
            o, n2, db = old.get(no), new[no], have.get(no)
            if db is None:
                continue
            if norm(o) == norm(n2):
                res['no_impact'] += 1
                if norm(db) != norm(n2):
                    res['preexisting'].append({'std': code, 'clause': no,
                                               'db_len': len(db), 'parse_len': len(n2)})
                continue
            if norm(db) == norm(n2):
                res['already_fixed'] += 1
            elif norm(db) == norm(o):
                res['to_update'].append({'std': code, 'clause': no,
                                         'db_len': len(db), 'new_len': len(n2)})
            else:
                res['affected_other'].append({'std': code, 'clause': no, 'old_len': len(o),
                                              'new_len': len(n2), 'db_len': len(db),
                                              'db_tail': db[-50:], 'new_tail': n2[-50:]})
    json.dump(res, open('/tmp/three_way.json', 'w'), ensure_ascii=False, indent=1)
    print(f"旧解析==新解析（本次无影响）{res['no_impact']} 条"
          f"，其中库与之不同的历史定点补全 {len(res['preexisting'])} 条")
    print(f"库里已是修好的样子 {res['already_fixed']} 条")
    print(f"★ 可安全写库（库==旧解析，新解析去掉了标题）{len(res['to_update'])} 条")
    print(f"⚠️ 三种都不同、需人工看 {len(res['affected_other'])} 条")
    for it in res['affected_other'][:10]:
        print(f"   {it['std']} §{it['clause']} 旧{it['old_len']} 新{it['new_len']} 库{it['db_len']}"
              f" | 库尾 {it['db_tail'][-26:]!r} | 新尾 {it['new_tail'][-26:]!r}")


if __name__ == '__main__':
    main()
