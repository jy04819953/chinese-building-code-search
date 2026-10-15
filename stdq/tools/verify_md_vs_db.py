#!/usr/bin/env python3
"""入库对账：把 MD 重解析结果和库里逐条比，揪出漏抽/截断/多余

【为什么需要这个脚本】
入库的坑从来不是单一种类，而是"每本新规范换一种花样"：
  · 条文号段数超上限被静默截断（GB/T 7106-2019 的 5 段编号，24 条没进库，无任何报错）
  · 正文含间隔号「·」被整条丢弃（单位写法 W/(m²·K) / kV·A，20 本规范 24 条被误杀）
  · 术语章"词名一行、释义另一段"只收了词名（全库 1286 条定义缺失）
  · 条文号写错（`2.0. 33` / `3.3.7.`）生成垃圾条目
靠"见一个错补一条正则"是追着错跑。**本脚本是对答案**：不管什么原因漏的，只要
「MD 里有、库里没有」或「两边内容对不上」，就会被列出来。
（注意：纯条文号比对抓不到"条文号在、但内容被截断"，所以本脚本**连内容一起比**。）

【用法】
    # 全库对账（入库后 / 定期体检）
    ~/.local/share/stdq-venv/bin/python3 scripts/verify_md_vs_db.py

    # 只查某几本（新入库规范必跑）
    ... verify_md_vs_db.py GB-55038-2025 DG-TJ-08-205-2024

    # JSON 输出（给 cron / agent 消费）
    ... verify_md_vs_db.py --json

    # 跳过 MD 后处理（对比"原始 MD"而非"入库时会变成的样子"）
    ... verify_md_vs_db.py --no-postprocess

【退出码】0 = 完全一致；1 = 发现差异（需人工定性）；2 = 环境/参数错误

【差异怎么定性（重要，不要一律当 bug 修）】
  · 库中缺失   → 几乎都是真漏抽，优先查（最常见的三类原因见文件头）
  · 内容不一致 → 两边都有但文本不同。**先看长度**：库短、MD 长 = 被截断；
                 库长、MD 短 = 库里可能混进了条文说明
  · 库中多余   → 库里有、MD 解析不出。多半是**历史手工补的条目**（正常），
                 或"条文说明被误当条文收进来"的幻影（垃圾，该删）
  逐条定性后再动手，改库前 `cp` 备份。

【与 detect_clause_drop.py 的分工】
  detect_clause_drop.py  = 只看 MD，用正则找"可疑写法"（能提前预警，但只认见过的花样）
  verify_md_vs_db.py     = MD 与库对账（结果导向，任何原因都能发现，但要求库已 rebuild）
  两个都要跑，互补。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..consts import STANDARDS_DIR as DATA_DIR

import importlib.util
import sqlite3



def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


from ..parser.clauses import extract_clauses as _extract_clauses
try:
    from .postprocess_md import postprocess
except Exception:
    postprocess = None


def _norm(s):
    """比对用归一化：去掉全部空白。

    为什么必须做：库是**当时的解析器版本**建的，而解析器/后处理的空格规范化会随版本微调
    （实测 $q _ { f }$ vs $q _{f}$、$\\mathrm { C }$ vs $\\mathrm{C}$），
    逐字符比会把这类"格式漂移"全报成差异，把真问题淹掉。
    去掉空白后仍能抓到真正的漏抽与截断。
    """
    return ''.join(s.split())


# 条文说明被误当条文收进来的典型特征（只提示，不过滤——最终仍由人定性）
_PHANTOM_PAT = re.compile(r'^\s*[.．]|本条规定了|这两条规定了|本条引用|本条文主要参考|本条规定的是')


def _phantom_hint(txt):
    return bool(_PHANTOM_PAT.search(txt or ''))


def main():
    ap = argparse.ArgumentParser(description='MD 重解析 vs 库 逐条对账')
    ap.add_argument('std_codes', nargs='*', help='只查这些规范号（默认全库）')
    ap.add_argument('--json', action='store_true', help='JSON 输出')
    ap.add_argument('--quiet', action='store_true', help='只输出汇总')
    ap.add_argument('--limit', type=int, default=8, help='每类最多列几条明细（默认 8）')
    ap.add_argument('--postprocess', action='store_true',
                    help='解析前先跑 MD 后处理（仅用于排查"后处理是否影响解析"；'
                         '默认不跑 —— 盘上的 MD 入库前已处理过，重复处理会改变解析结果）')
    args = ap.parse_args()

    db = os.path.join(str(DATA_DIR), 'standards.db')
    if not os.path.exists(db):
        sys.stderr.write(f'找不到 {db}\n')
        return 2
    con = sqlite3.connect(db)
    cur = con.cursor()

    if args.std_codes:
        rows = []
        for c in args.std_codes:
            r = cur.execute('SELECT code, md_file FROM standards WHERE code=?', (c,)).fetchone()
            if not r:
                sys.stderr.write(f'⚠️ 库里没有规范 {c}，跳过\n')
            else:
                rows.append(r)
    else:
        rows = cur.execute('SELECT code, md_file FROM standards ORDER BY code').fetchall()

    result = {'scanned': 0, 'missing': [], 'differ': [], 'extra': [], 'skipped_files': []}

    for code, mdf in rows:
        if not mdf or not os.path.exists(mdf):
            result['skipped_files'].append(code)
            continue
        result['scanned'] += 1
        raw = open(mdf, encoding='utf-8', errors='replace').read()
        text = postprocess(raw) if (args.postprocess and postprocess is not None) else raw
        try:
            got = dict(_extract_clauses(text, os.path.basename(mdf)))
        except Exception as e:
            sys.stderr.write(f'⚠️ {code} 解析失败: {e}\n')
            continue
        have = {r[0]: r[1] for r in cur.execute(
            'SELECT clause_no, content FROM clauses WHERE std_code=?', (code,))}

        for no, txt in got.items():
            if no not in have:
                result['missing'].append({'std': code, 'clause': no, 'md_len': len(txt),
                                          'phantom_hint': _phantom_hint(txt),
                                          'md_head': txt[:80]})
            elif _norm(have[no]) != _norm(txt):
                result['differ'].append({'std': code, 'clause': no,
                                         'db_len': len(have[no]), 'md_len': len(txt),
                                         'db_head': have[no][:80], 'md_head': txt[:80]})
        for no, c in have.items():
            if no not in got:
                result['extra'].append({'std': code, 'clause': no, 'db_len': len(c),
                                        'phantom_hint': _phantom_hint(c),
                                        'db_head': c[:80]})
    con.close()

    n_bad = len(result['missing']) + len(result['differ']) + len(result['extra'])

    if args.json:
        result['n_diff'] = n_bad
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if n_bad else 0

    print('=' * 74)
    print(f'MD vs 库 对账  扫描 {result["scanned"]} 本'
          f'{"（解析前跑了 MD 后处理）" if args.postprocess else "（按盘上 MD 原样解析）"}')
    print('=' * 74)
    if result['skipped_files']:
        print(f'⚠️ MD 文件缺失跳过: {len(result["skipped_files"])} 本 '
              f'({", ".join(result["skipped_files"][:5])}...)')
    if not n_bad:
        print('✅ 完全一致 —— 没有漏抽、没有截断、没有多余条目')
        return 0

    def dump(title, items, fmt):
        print(f'\n── {title}: {len(items)} 条 ' + '─' * max(0, 44 - len(title)))
        if args.quiet:
            return
        for it in items[:args.limit]:
            hint = '  ⚠️疑「条文说明被误当条文」' if it.get('phantom_hint') else ''
            print(f'   ● {it["std"]} §{it["clause"]}{hint}')
            print('     ' + fmt(it).replace('\n', '\n     '))
        if len(items) > args.limit:
            print(f'   ... 其余 {len(items) - args.limit} 条（--limit N 调整）')

    dump('库中缺失（MD 有、库没有 → 多半真漏抽）', result['missing'],
         lambda it: f'MD({it["md_len"]}字): {it["md_head"]}')
    dump('内容不一致（先看长度判断哪边是正文）', result['differ'],
         lambda it: f'库({it["db_len"]}字): {it["db_head"]}\nMD({it["md_len"]}字): {it["md_head"]}')
    dump('库中多余（库有、MD 解析不出 → 多半是历史手工补的，也可能是幻影）', result['extra'],
         lambda it: f'库({it["db_len"]}字): {it["db_head"]}')

    print(f'\n{"=" * 74}')
    print(f'合计差异 {n_bad} 条：缺失 {len(result["missing"])} / '
          f'不一致 {len(result["differ"])} / 多余 {len(result["extra"])}')
    print('⚠️ 逐条定性后再动手；改库前先 cp 备份。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
