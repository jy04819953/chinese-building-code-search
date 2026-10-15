#!/usr/bin/env python3
"""对 heading_glue_fix.py 的 review 清单做安全定性：
   对每条「库内容 vs 新解析」：
     ① 去掉库里粘连的 '## 标题' 片段后还差多少
     ② 差出来的那段文字（extra）是否在**同规范其它条文**里出现过（= 别人的正文被复制过来）
   分组输出：dup（extra 在别处有 → 可安全替换） / orphan（extra 别处没有 → 需人工看）
用法：python3 review_classify.py
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


def common_prefix_suffix(a, b):
    """a、b 归一化字符串：返回 (a 比 b 多的前缀, a 比 b 多的后缀)"""
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a) - i, len(b) - i) and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    return a[:i], a[len(a) - j:]


def main():
    stdq = load_stdq()
    # 复刻 heading_glue_fix.py 的解析（标题行跳过版）
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
            if HEAD.match(line):
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
    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()
    stds = cur.execute('SELECT code, md_file FROM standards ORDER BY code').fetchall()

    groups = {'same': 0, 'dup': [], 'orphan': [], 'shrunk': []}
    for code, mdf in stds:
        if not mdf or not os.path.exists(mdf):
            continue
        got = dict(stdq.extract_clauses(open(mdf, encoding='utf-8', errors='replace').read(),
                                        os.path.basename(mdf)))
        have = {r[0]: r[1] for r in cur.execute(
            'SELECT clause_no, content FROM clauses WHERE std_code=?', (code,))}
        others = {no: norm(c) for no, c in have.items()}
        for no, new_txt in got.items():
            old = have.get(no)
            if old is None or norm(old) == norm(new_txt):
                groups['same'] += 1
                continue
            a, b = norm(old), norm(new_txt)
            pre, suf = common_prefix_suffix(a, b)
            extra = a[len(pre):len(a) - len(suf)] if len(pre) + len(suf) <= len(a) else a
            # extra 里可能夹着 '## 标题'，先去掉
            heads = re.findall(r'##[^\n]{0,60}', extra)
            cleaned = extra
            for h in heads:
                cleaned = cleaned.replace(h, '')
            rec = {'std': code, 'clause': no, 'old_len': len(old), 'new_len': len(new_txt),
                   'extra_len': len(extra), 'extra_head': extra[:70]}
            if len(cleaned) <= 2:
                groups['same'] += 1
                continue
            # extra（或其中 12 字片段）是否出现在同规范其它条文里
            key = cleaned[:20] if len(cleaned) >= 20 else cleaned
            hit = [n2 for n2, c in others.items() if n2 != no and key and key in c]
            if hit:
                rec['also_in'] = hit[:3]
                groups['dup'].append(rec)
            else:
                groups['orphan'].append(rec)
    out = {k: (v if isinstance(v, int) else v) for k, v in groups.items()}
    json.dump(out, open('/tmp/review_classify.json', 'w'), ensure_ascii=False, indent=1)
    print(f"一致 {groups['same']}；需处理：重复文本(可安全替换) {len(groups['dup'])}；"
          f"疑似独有文本(必须人工看) {len(groups['orphan'])}")
    print("\n—— 重复文本样例（extra 在同规范别处也有）——")
    for r in groups['dup'][:10]:
        print(f"  {r['std']} §{r['clause']} 库{r['old_len']}→新{r['new_len']}；"
              f"多出 {r['extra_len']} 字，见于 {r['also_in']}：{r['extra_head']!r}")
    print("\n—— 疑似独有文本样例（extra 别处找不到）——")
    for r in groups['orphan'][:12]:
        print(f"  {r['std']} §{r['clause']} 库{r['old_len']}→新{r['new_len']}；"
              f"多出 {r['extra_len']} 字：{r['extra_head']!r}")


if __name__ == '__main__':
    main()
