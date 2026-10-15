#!/usr/bin/env python3
"""heading_glue_fix.py — 把「下一章标题 ## X 章名 被粘进上一条条文正文」修掉

背景：extract_clauses 的子条款收集 _collect_subitems 会跳过 '## ' 行判断（只拦 h1 '# '），
      于是 '## 9 文化、纪念建筑' 这类标题行被当成"续行"并进上一条条文 → 会成批受污染。
做法：① 这里用"修好的" _collect_subitems 重新解析全部 MD（不落库）；
      ② 把每条与库内容对比：
         - 归一化后相同 → 不动
         - 库内容 = 新解析 + 若干个 '## …' 片段（即只差标题粘连）→ 判定为"可安全替换"，写 new_parse
         - 其它 → 列入 needs_review，不动
输出：/tmp/heading_fix.json（changed / review / counts），stdout 打摘要。
用法：--dry-run（默认，只出报告） / --apply（写库，写前自动备份）
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from ..parser import clauses as stdq
from ..parser import common as _stdq_common
from ..consts import STANDARDS_DIR
stdq._is_clause_number = _stdq_common._is_clause_number


DB = os.path.join(STANDARDS_DIR, 'standards.db')
OUT = '/tmp/heading_fix.json'
HEAD = re.compile(r'^#{1,6}\s')




def norm(s):
    return ''.join((s or '').split())


def md_headings(mdf):
    out = []
    for l in open(mdf, encoding='utf-8', errors='replace').read().split('\n'):
        s = l.strip()
        if s.startswith('## ') and not s.startswith('###'):
            out.append(s)
    return sorted(set(out), key=len, reverse=True)


def remove_heads(txt, heads):
    """返回 (去掉标题后的文本, 命中的标题列表)。标题按最长优先前缀匹配找 '##' 位置。"""
    hits = []
    i = 0
    res = []
    while True:
        p = txt.find('##', i)
        if p < 0:
            res.append(txt[i:])
            break
        res.append(txt[i:p])
        matched = None
        for h in heads:
            if txt.startswith(h, p):
                matched = h
                break
        if matched:
            hits.append(matched)
            i = p + len(matched)
        else:
            res.append('##')
            i = p + 2
    return ''.join(res), hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()

    def patched_subitems(lines, start_idx):
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
            if HEAD.match(line):          # ★ 标题行不再并入正文
                j += 1
                continue
            is_new = bool(re.match(r'^\d+[\s\u3000]*[\u4e00-\u9fff#0-9a-zA-Z]', line))
            if is_new or not subitems:
                subitems.append(line)
            else:
                subitems[-1] += line
            j += 1
        return subitems, j - start_idx

    stdq._collect_subitems = patched_subitems

    # ★ 必须先把 (code, md_file) 全取出来：边遍历游标边在循环里执行别的查询会把外层结果集冲掉
    #   （实测：老写法只处理到第 1 本规范、500 条，其余全被跳过且不报错）
    stds = cur.execute('SELECT code, md_file FROM standards ORDER BY code').fetchall()

    changed, review, same, parse_fail = [], [], 0, []
    n_head_touched = 0
    t0 = time.time()
    for code, mdf in stds:
        if not mdf or not os.path.exists(mdf):
            parse_fail.append([code, 'MD 缺失'])
            continue
        try:
            got = dict(stdq.extract_clauses(open(mdf, encoding='utf-8', errors='replace').read(),
                                            os.path.basename(mdf)))
        except Exception as e:
            parse_fail.append([code, str(e)])
            continue
        heads = md_headings(mdf)
        have = {r[0]: r[1] for r in cur.execute(
            'SELECT clause_no, content FROM clauses WHERE std_code=?', (code,))}
        for no, new_txt in got.items():
            old = have.get(no)
            if old is None:
                continue
            if norm(old) == norm(new_txt):
                same += 1
                continue
            stripped, hits = remove_heads(old, heads)
            if hits and norm(stripped) == norm(new_txt):
                changed.append({'std': code, 'clause': no, 'heads': hits,
                                'old_len': len(old), 'new_len': len(new_txt)})
                n_head_touched += len(hits)
            else:
                review.append({'std': code, 'clause': no, 'old_len': len(old),
                               'new_len': len(new_txt),
                               'old_tail': old[-60:], 'new_tail': new_txt[-60:],
                               'n_head_hits': len(hits)})
    result = {'scanned': same + len(changed) + len(review),
              'same': same, 'changed': changed, 'review': review,
              'parse_fail': parse_fail, 'head_fragments': n_head_touched,
              'seconds': round(time.time() - t0, 1)}
    json.dump(result, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print(f"比对条文 {result['scanned']} 条：一致 {same} / 可安全修复 {len(changed)}"
          f"（含标题片段 {n_head_touched} 处）/ 需人工看 {len(review)}"
          f"；解析失败 {len(parse_fail)}；耗时 {result['seconds']}s")
    for it in review[:15]:
        print(f"  ⚠️ {it['std']} §{it['clause']}  库{it['old_len']}字→新解析{it['new_len']}字"
              f"（标题命中{it['n_head_hits']}）  旧尾…{it['old_tail'][-30:]!r}")

    if args.apply:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        shutil.copy2(DB, f'{DB}.bak.{stamp}-headingfix')
        n = 0
        for it in changed:
            new_txt = None
            mdf = cur.execute('SELECT md_file FROM standards WHERE code=?', (it['std'],)).fetchone()[0]
            got = dict(stdq.extract_clauses(open(mdf, encoding='utf-8', errors='replace').read(),
                                            os.path.basename(mdf)))
            new_txt = got[it['clause']]
            cur.execute('UPDATE clauses SET content=? WHERE std_code=? AND clause_no=?',
                        (new_txt, it['std'], it['clause']))
            n += 1
        print(f"✔ 已更新 {n} 条；备份 standards.db.bak.{stamp}-headingfix")
    con.close()


if __name__ == '__main__':
    main()
