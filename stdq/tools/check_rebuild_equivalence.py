#!/usr/bin/env python3
"""重建等价性检验 —— 判断"如果现在跑一次全量 rebuild，库会不会退化"

【为什么需要这个脚本】
`rebuild` 是 `DELETE FROM clauses` + 全目录重解析，所以：
  · 条文 id 会全部重排（向量要全量重建）
  · 更隐蔽的是：**库里的内容会被解析器当前行为重置**
    手工补过的条、按老解析行为落库的条，跑一次 rebuild 就没了。

于是「入库新规范 ⇒ 全量 rebuild ⇒ 前几天修好的东西又回去了」反复发生。
本脚本回答唯一重要的问题：**重建后的结果与当前库是否等价**。

判据：用 rebuild 的同一条解析路径（`extract_clauses`）解析全部 MD，与库逐条比对。
  · 会消失的条   → 重建后用户就查不到了（最严重，必须为 0）
  · 内容会变的条 → 逐条定性：变全=好事；变短/退化=必须先修解析器或 MD
  · 会新增的条   → 通常是好事（条目被正确拆出/补收）
  · 已知类差异   → KNOWN 白名单里的，不算失败（历史手工版比解析版更全等）

【用法】
    # 全库
    python -m stdq.tools.check_rebuild_equivalence

    # 只看某几本
    ... check_rebuild_equivalence GB-55038-2025 DG-TJ-08-205-2024

【退出码】0 = 等价（重建不会退化）；1 = 有差异，需逐条定性
【推荐时机】入库流程最后一步（重建等价性检验），以及每次改了解析器之后
"""
import argparse
import glob
import os
import sqlite3
import sys

from ..consts import STANDARDS_DIR as DATA_DIR
from ..parser.clauses import extract_clauses
from ..parser.common import nz_cmp

# 已知类差异白名单：(std_code, clause_no) -> 原因
# 重建后会与库不同，但**已知且可接受**，记在这里避免把信噪比打没。
# 各条原因按本项目实测填，别人换数据时按自己情况增删。
KNOWN = {}


def collect(md_files=None, codes=None):
    """返回 (lost, diffs, extra, known_hits)"""
    db_path = os.path.join(DATA_DIR, 'standards.db')
    cur = sqlite3.connect(db_path).cursor()
    code_by_md = {}
    for code, mf in cur.execute('SELECT code, md_file FROM standards').fetchall():
        if mf:
            code_by_md[os.path.basename(mf)] = code
    lost, diffs, extra, known = [], [], [], []
    targets = md_files or sorted(glob.glob(os.path.join(DATA_DIR, '*.md')))
    for md in targets:
        bn = os.path.basename(md)
        if '.bak' in bn or '条文索引' in bn:
            continue
        code = code_by_md.get(bn)
        if not code:
            continue
        if codes and code not in codes:
            continue
        try:
            with open(md, encoding='utf-8', errors='replace') as f:
                parsed = extract_clauses(f.read(), bn)
        except Exception as e:                       # noqa: BLE001
            print(f'  ⚠️ 解析异常 {bn}: {e}', file=sys.stderr)
            continue
        pmap = dict(parsed or [])
        db = {r[0]: r[1] for r in
              cur.execute('SELECT clause_no, content FROM clauses WHERE std_code=?', (code,)).fetchall()}
        for no, new in pmap.items():
            key = (code, no)
            if no not in db:
                (known if key in KNOWN else extra).append(
                    (code, no, KNOWN.get(key, ''), 0, len(new)))
            elif nz_cmp(db[no]) != nz_cmp(new):
                (known if key in KNOWN else diffs).append(
                    (code, no, KNOWN.get(key, ''), len(nz_cmp(db[no])), len(nz_cmp(new))))
        for no in db:
            if no not in pmap:
                key = (code, no)
                (known if key in KNOWN else lost).append(
                    (code, no, KNOWN.get(key, ''), len(nz_cmp(db[no])), 0))
    return lost, diffs, extra, known


def main():
    ap = argparse.ArgumentParser(description='重建等价性检验：重建后的库与当前库是否等价')
    ap.add_argument('codes', nargs='*', help='只查这几本规范（默认全库）')
    args = ap.parse_args()

    if not os.path.exists(os.path.join(DATA_DIR, 'standards.db')):
        print(f'找不到数据库：{DATA_DIR}/standards.db', file=sys.stderr)
        print('提示：设置 STDQ_DATA_DIR 指向你的数据目录', file=sys.stderr)
        return 2

    lost, diffs, extra, known = collect(codes=set(args.codes) if args.codes else None)

    print('=' * 74)
    print('重建等价性检验（vs 当前库）')
    print('=' * 74)
    print(f'  ① 会消失的条   : {len(lost)}')
    print(f'  ② 内容会变的条 : {len(diffs)}')
    print(f'  ③ 会新增的条   : {len(extra)}')
    print(f'  ④ 已知类差异   : {len(known)}（白名单内，不算失败）')

    if lost:
        print('\n⚠️ 会消失（必须处理 —— 重建后用户就查不到了）：')
        for c, n, _why, a, _b in lost[:30]:
            print(f'   - {c:<18} §{n:<10} {a:>5}字')
        if len(lost) > 30:
            print(f'   ...（共 {len(lost)} 条）')
    if diffs:
        print('\n内容会变（逐条定性：变全=好事 / 退化=要修）：')
        for c, n, _why, a, b in sorted(diffs, key=lambda x: x[3] - x[4], reverse=True)[:30]:
            arrow = f'变全 +{b - a}' if b > a else f'⚠️ 退化 {b - a}'
            print(f'   {c:<18} §{n:<10} {a:>5} → {b:<5} {arrow}')
        if len(diffs) > 30:
            print(f'   ...（共 {len(diffs)} 条）')
    if extra:
        print('\n会新增（通常是好事：条目被正确拆出/补收）：')
        for c, n, _why, _a, b in extra[:20]:
            print(f'   + {c:<18} §{n:<10} {b:>5}字')
        if len(extra) > 20:
            print(f'   ...（共 {len(extra)} 条）')
    if known:
        print('\n已知类差异（白名单）：')
        for c, n, why, a, b in known[:15]:
            print(f'   · {c:<18} §{n:<10} {a:>5} → {b:<5} {why}')

    bad = len(lost) + len(diffs)
    print('\n结论: ' + ('✅ 等价（重建不会退化）' if bad == 0 else f'❌ 有 {bad} 处差异需处理'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
