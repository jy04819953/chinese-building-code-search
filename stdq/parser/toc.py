"""parser · toc —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from ..consts import DB_PATH, _TOC_SKIP_RE, _TOC_TAIL_RE


def parse_toc(md_text: str) -> list[dict]:
    r"""从 MD 文本抽取目录章节条目。

    返回 [{"clause_no": "4.5", "title": "卷材及涂膜防水层设计", "level": "section"}, ...]
    level: chapter(X) / section(X.Y) / appendix(附录X)

    识别规则:
    - 起点:`## 目 次` / `## 目 录` / `# 目 次` / OCR 粘连形式 `## 目次1总则...(1)`
    - 终点优先级:① `## Contents`(英文目录标题) ② 兜底 50 行(防 OCR 粘连无限延伸)
    - 切掉尾部点点点+页码:`(?<!\d)[\.…]{1,}(?:\s+[\.…]{1,})*(\s*[（(]?\d+[）)]?)?\s*$`
    - 跳过"本规范用词说明"等非章节行
    """
    lines = md_text.split("\n")
    toc_start = None
    for i, line in enumerate(lines):
        if re.match(r"^#+\s*目\s*[次录]\s*$", line.strip()):
            toc_start = i
            break
    if toc_start is None:
        # 兼容 OCR 把"目次"和第一条粘连:`## 目次1总则...(1)`
        for i, line in enumerate(lines):
            if re.match(r"^#+\s*目\s*[次录]\s*\S", line.strip()):
                toc_start = i
                break
    if toc_start is None:
        return []
    # 终点:① 优先找英文 Contents ② 兜底 50 行(标准);粘连形式用 200 行兜底
    # 粘连(整行)时,L35 一行=整本目录,但 L36 紧跟 Contents 截止 → 0 条,所以粘连用更大范围
    limit = 50
    if toc_start is None:
        return []
    # 终点:① 优先找英文 Contents / 重复目录标题 ② 兜底 200 行(大型规范目录超过 50 行的很多)
    # 修订:之前 toc_end 永远 50 行(GB-50017-2017 钢结构目录 112 行,§10.4 后全漏!17 本规范目录 >50 行)
    # 现在按 ## Contents / ## 目 录 终止,兜底 200 行
    toc_end = None
    for j, line in enumerate(lines[toc_start + 1:], start=toc_start + 1):
        stripped = line.strip()
        # 找英文 Contents 或重复中文目录标题
        if re.match(r'^#+\s*Contents\s*$', stripped) or re.match(r'^#+\s*目\s*[次录]\s*$', stripped):
            toc_end = j
            break
        # 也找下一个 ## 一级标题(非空)
        if re.match(r'^##\s+\S', stripped):
            toc_end = j
            break
    if toc_end is None:
        toc_end = min(toc_start + 1 + 200, len(lines))  # 兜底 200 行
    else:
        toc_end = min(toc_end, len(lines))  # 兜底
    # 如果 toc_end 计算过短(< toc_start + 1),强制 50 行兜底
    if toc_end <= toc_start + 1:
        toc_end = min(toc_start + 1 + 50, len(lines))

    entries = []
    for line in lines[toc_start + 1: toc_end]:
        line = line.strip()
        if not line or _TOC_SKIP_RE.search(line):
            continue
        # A1:剥离 <sub>N</sub> HTML 标签(MinerU OCR 把页码写成 <sub>16</sub>)
        # 替换成纯数字,后面 _TOC_TAIL_RE 当页码切掉
        line = re.sub(r'<sub>(\d+)</sub>', r'\1', line)
        # 切尾部点点点 + 可选页码(全/半角括号,页码可空)
        line_clean = _TOC_TAIL_RE.sub("", line).strip()
        # A2:去标题末尾 OCR 误混入的纯数字页码(GB-50007 那种 "无筋扩展基础61" 末尾页码)
        # 规则:只去末尾 1-3 位纯数字(不是章节号,章节号前面有点号)
        # 例:"无筋扩展基础61" → "无筋扩展基础","桩基础出86" → "桩基础出86"(只去末尾纯数字)
        line_clean = re.sub(r'([一-鿿])\d{1,3}$', r'\1', line_clean).strip()
        # B1:OCR 异常预处理(JGJ-102-2003 玻璃幕墙等 8 本规范 OCR 切碎模式)
        # B1.1:字母 I/l 替数字 1(OCR 常见错识)
        #   "11. I 般规定" → "11.1 般规定","I 1.2 框支承" → "1.2 框支承"(剥离前缀 I)
        line_clean = re.sub(r'(\d+)\.\s*[Il]\s*', r'\1.', line_clean)
        line_clean = re.sub(r'^[Il]\s*(\d+\.\d+)', r'\1', line_clean)
        # B1.2:数字之间空格(OCR 切碎)
        #   "12. 1 般规定" → "12.1 般规定","3. 3出入口" → "3.3出入口"
        line_clean = re.sub(r'(\d+)\.\s+(\d+)(?=\s|$|[\u4e00-\u9fff]|[\.\u4e00-\u9fff+])', r'\1.\2', line_clean)
        # 英文过滤 — 标题必须含中文字符,避免 GB 50345 那种中英文双目录污染
        # 兜底 50 行扫到 ## Contents 段后,正则会匹英文目录行(4.1 General Requirements)
        # 用 re.search 过滤:无中文字符的标题不收录
        if not re.search(r"[\u4e00-\u9fff]", line_clean):
            continue

        m = re.match(r"^附录\s*([A-Z])\s*(.+)$", line_clean)
        if m:
            entries.append({"clause_no": f"附录{m.group(1)}", "title": m.group(2).strip(), "level": "appendix"})
            continue
        # 优先匹 X.Y.Z 三段(DG/JGJ 标准的 §1.0.1 / §1.0.2 等),避免 §1.0.1 被错匹成 §1.0 节
        m = re.match(r"^(\d+\.\d+\.\d+)\s*(.+)$", line_clean)
        if m:
            # X.Y.Z 子条文不进 chapters 表(parse_toc 是抽章节不进子条文)
            # 但匹配结果留个 wait 用 — 跳过即可
            continue
        m = re.match(r"^(\d+\.\d+)\s*(.+)$", line_clean)
        if m:
            entries.append({"clause_no": m.group(1), "title": m.group(2).strip(), "level": "section"})
            continue
        m = re.match(r"^(\d+)\s*(.+)$", line_clean)
        if m:
            entries.append({"clause_no": m.group(1), "title": m.group(2).strip(), "level": "chapter"})
            continue
    return entries

def parse_toc_v2(md_text: str) -> list[dict]:
    """parse_toc 增强版,加 OCR 切碎修复

    改动:每行识别前先调正则修复合并切碎的章节号
    """
    base_entries = parse_toc(md_text)
    # parse_toc 已经返回结果了——OCR 修复其实应该在 parse_toc 里加,这里留空
    # (实际改动在下一步打到 parse_toc 里)
    return base_entries

def validate_chapter_tree(std_code: str) -> dict[str, list[str]]:
    """校对 pipeline,扫一本规范的 chapters + clauses 结构性问题,产分类报告(不自动改)

    约定:
    - 不自动修,只产报告
    - 出错去原文核查
    - X.0.X 直接对应 X(不补 X.0 这一级)

    返回 {分类: [issue 列表, ...]} 字典,每类 issue 列表

    校验项分类:
    1. ORPHAN_SECTION:节 X.Y 父章 X 不在 chapters(31 条真问题根因)— parse_toc 漏抽
    2. ORPHAN_SUB:子条文 X.Y.Z 父节 X.Y 不在 chapters(4264 条)— 可能是 X.0 缺失连锁
    3. EMPTY_SECTION:节 X.Y 0 子条文(可能空壳,需人工核查)
    4. APPENDIX_PARENT:附录 X 父应 NULL 不为 NULL
    5. X0_FALLBACK:子条文 X.0.Y 父节 X.0 不在 chapters,但父章 X 在 — 应直接对应 X
    """
    issues = {'ORPHAN_SECTION': [], 'ORPHAN_SUB': [], 'EMPTY_SECTION': [], 'APPENDIX_PARENT': [], 'X0_FALLBACK': []}
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()

        # 1. 章节表
        cur.execute('SELECT clause_no, title, level, parent_clause_no FROM clause_chapters WHERE std_code=?', (std_code,))
        chapters = {row[0]: {'title': row[1], 'level': row[2], 'parent': row[3]} for row in cur.fetchall()}

        # 2. 子条文表(只 X.Y.Z 三段)
        cur.execute("SELECT DISTINCT clause_no FROM clauses WHERE std_code=? AND clause_no LIKE '%.%.%'", (std_code,))
        sub_clauses = {row[0] for row in cur.fetchall()}

        if not chapters and not sub_clauses:
            issues['ORPHAN_SECTION'].append('NO_CHAPTERS_OR_SUBS')
            return issues

        # 校验 5(优先做):X.0.Y 子条文,X.0 不在但 X 在 — 应直接对应 X
        # 这是用户 反馈的根本要求
        x0_fallback = set()  # 记下 X.0 这种"应直接对应 X"的子条文
        for sub in sub_clauses:
            parts = sub.split('.')
            # 子条文 X.0.Y 模式:parts = ['X', '0', 'Y']
            if len(parts) == 3 and parts[1] == '0':
                x_chapter = parts[0]
                parent_xy = f'{x_chapter}.0'
                if parent_xy not in chapters and x_chapter in chapters:
                    # X.0 不在 chapters,但 X 章在 — 按约定:直接对应 X(不需要 X.0 这一级)
                    issues['X0_FALLBACK'].append(f'{sub} 父节 {parent_xy} 不在,但章 {x_chapter} 在 — 应直接对应 {x_chapter}')

        # 校验 1:每条 X.Y 必属于存在的 X (不是 X.0 fallback 的)
        for cn, info in chapters.items():
            if info['level'] != 'section':
                continue
            if '.' in cn:
                parent_x = cn.split('.')[0]
                if parent_x not in chapters:
                    issues['ORPHAN_SECTION'].append(f'{cn} "{info["title"]}" → 父章 {parent_x} 不在 chapters')

        # 校验 2:每条 X.Y.Z 必属于存在的 X.Y (排除 X.0.Fallback 情况)
        for sub in sub_clauses:
            parts = sub.split('.')
            parent_xy = '.'.join(parts[:-1])
            if parent_xy in chapters:
                continue  # 父节在,OK
            # 检查是不是 X.0 fallback 情况
            if len(parts) == 3 and parts[1] == '0' and parts[0] in chapters:
                continue  # X.0.Y 子条文 → 父 X(已在 X0_FALLBACK 报告)
            issues['ORPHAN_SUB'].append(f'{sub} → 父 {parent_xy} 不在 chapters')

        # 校验 3:每条 X.Y 至少 1 条子条文
        for cn, info in chapters.items():
            if info['level'] != 'section':
                continue
            children = [s for s in sub_clauses if s.startswith(cn + '.')]
            if not children:
                issues['EMPTY_SECTION'].append(f'{cn} "{info["title"][:30]}" 0 子条文(可能漏记或真真空壳)')

        # 校验 4:附录 X 的 parent_clause_no 必为 NULL
        for cn, info in chapters.items():
            if cn.startswith('附录') and info['parent'] is not None:
                issues['APPENDIX_PARENT'].append(f'{cn} 父应是 NULL 当前 "{info["parent"]}"')

    return issues

def write_report(std_codes: list[str], output_path: str = '/tmp/chapter_report.md') -> dict[str, int]:
    """跑所有规范,产分类报告(写到文件,不改 DB)

    约定:有出入 → 去查原文,根据实际情况补充(不自动改)。
    这个函数把每个规范的 issues 写到文件,用户自己看报告再决定怎么补。
    """
    import datetime
    total_stats = {'ORPHAN_SECTION': 0, 'ORPHAN_SUB': 0, 'EMPTY_SECTION': 0, 'APPENDIX_PARENT': 0, 'X0_FALLBACK': 0}
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f'# 章节目录校对报告\n\n')
        f.write(f'生成时间: {datetime.datetime.now().isoformat()}\n')
        f.write(f'扫描规范数: {len(std_codes)}\n\n')
        f.write('--- 报告阅读说明 ---\n\n')
        f.write('- **ORPHAN_SECTION**:节 X.Y 父章 X 不在 → parse_toc 漏抽,需查原文\n')
        f.write('- **ORPHAN_SUB**:子条文 X.Y.Z 父节 X.Y 不在 → 可能是 ORPHAN_SECTION 连锁\n')
        f.write('- **X0_FALLBACK**:子条文 X.0.Y 父 X.0 不在但 X 在 → **应直接对应 X**(无需补 X.0)\n')
        f.write('- **EMPTY_SECTION**:节 X.Y 0 子条文(可能空壳,需查原文)\n')
        f.write('- **APPENDIX_PARENT**:附录 X 父应 NULL 不为 NULL\n\n')
        f.write('--- 详细报告 ---\n\n')

        for std_code in std_codes:
            issues = validate_chapter_tree(std_code)
            has_any = any(len(v) > 0 for v in issues.values())
            if not has_any:
                continue
            f.write(f'## {std_code}\n\n')
            for category, items in issues.items():
                if not items:
                    continue
                f.write(f'### {category} ({len(items)} 条)\n\n')
                for item in items[:10]:
                    f.write(f'- {item}\n')
                if len(items) > 10:
                    f.write(f'- ... (还有 {len(items) - 10} 条)\n')
                f.write('\n')
                total_stats[category] += len(items)
        f.write('--- 总计 ---\n\n')
        for cat, cnt in total_stats.items():
            f.write(f'- {cat}: {cnt}\n')
    return total_stats
