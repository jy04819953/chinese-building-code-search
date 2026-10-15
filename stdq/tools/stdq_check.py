#!/usr/bin/env python3
"""条文号连续性检查（带跳号自动分类）

逻辑：检查已有条文之间是否有跳号，以及每节第一条是否为 .1。
不检查最后一个编号之后（规范本身可以不连续）。

【为什么需要"分类"】
只输出"缺 [X.Y.Z]"会淹没在假警报里——**跳号绝大多数不是真缺**。实测：
  · 假·节标题   ~11  （`## 9.2.4 变形检测` —— 该规范用 5 段编号，9.2.4 是**小节标题**不是条文）
  · 假·目录行   ~6   （`2.6.2泡沫灭火剂 ……… 12` —— 目录条目）
  · 假·行内引用 ~2   （`…经本标准第6.2.4～6.2.7条…` —— 别的条文里提了一嘴）
  · ★真漏抽     ~10  （编号在 MD 行首且后面跟正文，但没进库）
  · ？需查 PDF  ~24  （整本 MD 找不到该编号 → 可能真丢，也可能原文本来就跳）
**只有 ★真漏抽 需要动手补**；假警报忽略；？的要查 PDF 定性（SOP 见
docs/ingest-sop.md §3.5）。所以"必须修到 0"是错的口径——永远修不到 0。

【用法】
    python -m stdq.tools.stdq_check            # 全库
    ... stdq_check.py GB-55038-2025                                           # 单本
    ... stdq_check.py --json                                                  # JSON 输出
    ... stdq_check.py --only-real                                             # 只列 ★真漏抽

【退出码】0 = 没有 ★真漏抽；1 = 存在 ★真漏抽（需补）；2 = 环境错误
"""
import sys, os, re, json, sqlite3, argparse
from collections import defaultdict

from ..consts import STANDARDS_DIR as DATA_DIR

DB_PATH = os.path.join(str(DATA_DIR), 'standards.db')
CJK = re.compile(r'[\u4e00-\u9fff]')


def parse_clause_no(s):
    parts = s.split('.')
    return tuple(int(p) for p in parts)


def check_standard(std_code, std_name, clause_nos):
    """原有检测逻辑（不变）：找出所有跳号位置"""
    issues = []
    if not clause_nos:
        return issues
    parsed = []
    for cn in clause_nos:
        try:
            parsed.append((parse_clause_no(cn), cn))
        except (ValueError, AttributeError):
            continue
    parsed.sort(key=lambda x: x[0])

    groups = defaultdict(list)
    for tup, orig in parsed:
        if len(tup) >= 3:                      # 只看三级及以上条文号
            # 修: 原来按 tup[:2] 分组、取末位比较 —— **混了不同层级**。
            # 实测：某规范的 8.5.1 / 8.5.1.6 / 8.5.2 / 8.5.2.3 末位被排成 [1,6,2,3]，
            # 于是凭空报"缺 8.5.2~8.5.5"（其实 §8.5.2 就在库里）。同类四段编号的规范同理。
            # 正确做法: 按**父路径** tup[:-1] 分组，同级才比 —— 3 段归 (X.Y)，4 段归 (X.Y.Z)。
            groups[tup[:-1]].append((tup, orig))

    for key, items in groups.items():
        if len(items) < 2:
            continue
        label = '.'.join(str(x) for x in key)
        nums = [t[0][-1] for t in items]
        if nums[0] != 1:                       # 起始号不是1 → 可能丢了 3.2.1
            missing = [f'{label}.{m}' for m in range(1, nums[0])]
            issues.append({'group': label, 'after': '(本组起始)',
                           'before': items[0][1], 'missing': missing, 'gap': nums[0] - 1})
        for i in range(len(nums) - 1):         # 包括最后一条
            gap = nums[i + 1] - nums[i]
            if gap >= 2:
                missing = [f'{label}.{m}' for m in range(nums[i] + 1, nums[i + 1])]
                if len(missing) <= 5:
                    issues.append({'group': label, 'after': items[i][1],
                                   'before': items[i + 1][1], 'missing': missing, 'gap': gap})
    return issues


def classify(no, lines, note_at):
    """把「缺的编号」在 MD 里定性。返回 (类别, 证据行)

    类别含义见文件头。判定优先级：目录行 > 节标题 > 真漏抽 > 说明区 > 引用 > 查PDF
    """
    esc = re.escape(no)
    head = re.compile(r'^\s*#*\s*' + esc + r'(?!\.?\d)')
    anyw = re.compile(r'(?<![\d.])' + esc + r'(?!\.?\d)')

    for i, l in enumerate(lines):
        if not head.search(l):
            continue
        in_body = (note_at is None or i < note_at)
        t = l.strip()
        body = re.sub(r'^\s*#*\s*' + esc + r'\s*', '', t)
        if '…' in t or re.search(r'[\s.]\d{1,3}\s*$', t):
            return '假·目录行', t
        if not in_body:
            return '？编号只在说明区', t
        if len(CJK.findall(body)) < 8 and '#' in l:
            return '假·节标题', t
        # 加: 有 # 前缀 + 正文没有句末标点 + 内容短 → 也是节标题
        # 实测 GB-T 7106-2019 `## 9.2.2 确定测点和安装位移计`（10 个汉字,超过上面 8 字阈值）
        if '#' in l and not re.search(r'[。；：]', body) and len(CJK.findall(body)) < 25:
            return '假·节标题', t
        return '★真漏抽', t

    # 行内出现（非行首）
    for i, l in enumerate(lines):
        m = anyw.search(l)
        if not m:
            continue
        after, before = l[m.end():m.end() + 30], l[max(0, m.start() - 6):m.start()]
        if re.search(r'条\s*$', after) or re.search(r'第\s*$', before) or re.search(r'[～~—–→至]\s*$', before):
            return '假·行内引用', l.strip()
        if len(CJK.findall(after)) >= 8:
            return '★真漏抽·编号粘在上一行末尾', l.strip()
    return '？需查 PDF', ''


def main():
    ap = argparse.ArgumentParser(description='条文号连续性检查（含跳号分类）')
    ap.add_argument('std_codes', nargs='*', help='只查这些规范号（默认全库）')
    ap.add_argument('--json', action='store_true', help='JSON 输出')
    ap.add_argument('--only-real', action='store_true', help='只列 ★真漏抽')
    ap.add_argument('--no-classify', action='store_true', help='只做连续性检测，不分类（快）')
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.stderr.write(f'找不到 {DB_PATH}\n')
        return 2
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()

    if args.std_codes:
        rows = []
        for code in args.std_codes:
            r = c.execute('SELECT code, name FROM standards WHERE code=?', (code,)).fetchone()
            if r:
                rows.append(r)
            else:
                sys.stderr.write(f'⚠️ 库里没有规范 {code}\n')
    else:
        rows = c.execute('SELECT code, name FROM standards ORDER BY code').fetchall()

    md_of = dict(c.execute('SELECT code, md_file FROM standards').fetchall())
    per_std, summary = [], defaultdict(int)
    real = 0

    for code, name in rows:
        clause_nos = [r[0] for r in c.execute(
            'SELECT clause_no FROM clauses WHERE std_code = ? ORDER BY clause_no', (code,))]
        issues = check_standard(code, name, clause_nos)
        if not issues:
            per_std.append({'code': code, 'name': name, 'n': len(clause_nos), 'issues': []})
            continue

        lines, note_at = None, None
        if not args.no_classify:
            mdf = md_of.get(code)
            if mdf and os.path.exists(mdf):
                lines = open(mdf, encoding='utf-8', errors='replace').read().split('\n')
                note_at = next((i for i, l in enumerate(lines)
                                if re.match(r'^#*\s*(条文说明|起草说明|编制说明)\s*$', l.strip())), None)

        out = []
        for iss in issues:
            classified = []
            for no in iss['missing']:
                if lines is None:
                    cat, ev = '未分类', ''
                else:
                    cat, ev = classify(no, lines, note_at)
                summary[cat] += 1
                if cat.startswith('★'):
                    real += 1
                classified.append({'no': no, 'cat': cat, 'evidence': ev[:100]})
            out.append({'group': iss['group'], 'after': iss['after'], 'before': iss['before'],
                        'missing': classified})
        per_std.append({'code': code, 'name': name, 'n': len(clause_nos), 'issues': out})

    db.close()

    if args.json:
        print(json.dumps({'standards': per_std, 'summary': dict(summary), 'n_real': real},
                         ensure_ascii=False, indent=1))
        return 1 if real else 0

    for st in per_std:
        if not st['issues']:
            print(f'✅ {st["code"]}: {st["n"]}条，无异常跳号')
            continue
        n_real = sum(1 for iss in st['issues'] for m in iss['missing'] if m['cat'].startswith('★'))
        flag = '⚠️' if n_real else '·'
        print(f'\n{flag}  {st["code"]} ({st["name"]}) — {len(st["issues"])}处跳号'
              f'{"，其中 ★真漏抽 " + str(n_real) + " 条" if n_real else "（全为假警报）"}:')
        for iss in st['issues']:
            for m in iss['missing']:
                if args.only_real and not m['cat'].startswith('★'):
                    continue
                print(f'    [{m["cat"]}] {m["no"]}')
                if m['evidence']:
                    print(f'        {m["evidence"]}')

    print('\n' + '=' * 70)
    print(f'检查完成: {len(rows)}本规范')
    for k in sorted(summary):
        print(f'   {k:<22} {summary[k]} 处')
    print(f'\n★ 需要动手补的（真漏抽）: {real} 条')
    if not real:
        print('✅ 没有需要补的真漏抽（假警报不影响使用）')
    else:
        print('⚠️ 真漏抽按 docs/ingest-sop.md §3.5 修；假警报忽略；？需查 PDF 的走同一节 SOP。')
    return 1 if real else 0


if __name__ == '__main__':
    sys.exit(main())
