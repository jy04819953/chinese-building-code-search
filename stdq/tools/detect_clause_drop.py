#!/usr/bin/env python3
"""入库前跑一次，列出可能被 stdq rebuild 吞掉的条文号

实战沉淀：从 /tmp 临时脚本正式迁入 skills/。
后续入库任何 MinerU/RapidDoc 转出的 MD 都应该先跑这个脚本。

检测三类 bug（详见 docs/ingest-sop.md）:
- Bug #0: 条文号 X.Y.Z 数字间带空格（OCR/MinerU 常见）
- Bug #1: LaTeX 公式结尾紧接下一条文号同行
- Bug #2: 条文号行同时含 <table> 起始

用法（在仓库根目录下运行）:

    python -m stdq.tools.detect_clause_drop <规范.md> [更多.md ...]

不传路径只打印用法，不报错也不检测。多本可一次传多个路径。

本脚本只用 stdlib,任何 Python 3 解释器都能跑。

修复 SOP 见 docs/ingest-sop.md。
"""
import re
import sys
from ..consts import STANDARDS_DIR


def detect(path):
    """检测单个 MD 文件,返回 issues 列表 [(tag, what, ctx), ...]"""
    with open(path, 'r') as f:
        content = f.read()
    issues = []

    # 噪声区间：① LaTeX 公式标签 \tag{3.0. 6-2} ② 表格内部数据（形体系数表等）
    _noise_ranges = [(m.start(), m.end()) for m in re.finditer(r'\\tag\{[^}]*\}', content)]
    _noise_ranges += [(m.start(), m.end()) for m in re.finditer(r'<table>.*?</table>', content, re.S)]

    def _is_noise(pos):
        return any(a <= pos < b for a, b in _noise_ranges)

    # Bug #0: 条文号内部带空格（OCR/MinerU 常见）
    # 扩形态：原正则要求「每个点后都带空格」(2. 0. 17)，漏检另外三种，
    # 导致术语条文静默丢失，且跳号检查只报跳号、不报根因。三种形态：
    #   形态A/B 空格出现在点前后：2. 0. 17  /  2.0. 17  /  2. 1.17
    #           —— 正常行文不会出现这种写法，不限于标题行
    #   形态C   末节数字被空格拆开：2.0.1 0  /  2.1.1 1
    #           —— 必须限定标题行，否则目录行/条文说明序号（如「5.4.2 1 地下建筑…」）大量误报
    _cand = []
    for _m in re.finditer(r'\d{1,3}(?:[ \t]*\.[ \t]*\d{1,3}){1,3}', content):
        _s = _m.group(0)
        if (re.search(r'[ \t]', _s) and _s.count('.') >= 2
                and _s not in _cand and not _is_noise(_m.start())):
            _cand.append(_s)
    for _m in re.finditer(r'(?m)^#{1,4}[ \t]*\d{1,3}\.\d{1,3}\.\d{1,3}[ \t]+\d{1,3}', content):
        _s = _m.group(0).lstrip('# \t')
        if _s not in _cand:
            _cand.append(_s)
    if _cand:
        issues.append(('Bug#0-条文号空格', f'{len(_cand)} 处', f'例: {_cand[:3]}'))

    # Bug #1: \end{...}$$ 紧跟 X.Y.Z
    for m in re.finditer(r'\\end\{[a-z]+\}\$\$?(?=\d{1,2}\.\d{1,2}\.\d{1,2})', content):
        ctx = content[max(0, m.start() - 30):m.end() + 30]
        issues.append(('Bug#1-LaTeX粘连', m.group(0), ctx))

    # Bug #2: 含 X.Y.Z 的行同时含 <table>
    # ⚠️ 表格内部常有 "2.5 3.0 4.0" 这种厚度数据,以及 "表5.4.3的取值" 这种表标题引用,正则会被误判
    # 收紧 2 处:
    # 1. 条文号必须紧跟中文（排除表内纯数字）
    # 2. 条文号位置必须在 <table> 之前（条文引导语 + 表名 + 公式 + <table> 顺序）
    #    表内的 "表5.4.3的取值" 在 <table> 后面,是表标题引用,不是正文条文
    for i, line in enumerate(content.split('\n'), 1):
        if '<table>' in line:
            table_pos = line.find('<table>')
            line_before_table = line[:table_pos]
            ms = re.findall(r'\d{1,2}\.\d{1,2}\.\d{1,2}(?=[\u4e00-\u9fff])', line_before_table)
            if ms:
                issues.append(('Bug#2-table同行', ms, f'line {i}: {line[:120]}...'))

    return issues


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    total_issues = 0
    for path in sys.argv[1:]:
        issues = detect(path)
        if not issues:
            print(f'✅ {path}: 无粘连')
        else:
            total_issues += len(issues)
            print(f'⚠️  {path}: {len(issues)} 处粘连')
            for tag, what, ctx in issues:
                print(f'   [{tag}] {what}')
                print(f'     上下文: {ctx[:200]}')

    if total_issues > 0:
        print()
        print(f'⚠️  共 {total_issues} 处粘连,修复后入库。')
        print('   修复 SOP: docs/ingest-sop.md')
        sys.exit(2)
    else:
        print()
        print('✅ 所有文件无粘连,可以 rebuild。')


if __name__ == '__main__':
    main()