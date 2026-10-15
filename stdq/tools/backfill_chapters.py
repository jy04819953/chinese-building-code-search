#!/usr/bin/env python3
"""一次性回填脚本:给 standards.db 里所有已有规范补全 clause_chapters 表。

用法:
    python -m stdq.tools.backfill_chapters.py [--dry-run]

逻辑:
    1. 读 standards 表所有 (code, md_file)
    2. 过滤有 md_file 的
    3. 对每本 MD 调 stdq.parse_toc 抽目次
    4. 派生 parent_clause_no(X.Y.Z → X.Y)
    5. INSERT OR IGNORE 到 clause_chapters
    6. 统计:扫了几本 / 写了几条 / 跳了几本(没目录)

不需要跑 rebuild — 只动 clause_chapters 表,不影响 clauses / standards / embeddings。
"""
import argparse
import os
import re
import sqlite3
import sys
from ..consts import STANDARDS_DIR

# 复用 stdq.parse_toc
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from ..parser.toc import parse_toc  # noqa: E402

DB_PATH = os.path.join(STANDARDS_DIR, 'standards.db')


# 正文 ## 标题提取(只读探测层)。用途:目录段(parse_toc)抽 0 条或异常少时,
# 交叉看 MD 正文里还有没有可恢复的章/节结构,提示操作者"该走视觉/人工补",不自动写库。
# 只抽:X 章 + X.Y 节 + 附录 X。不抽 X.Y.Z 子条文、不录 X.0。
# 已知边界(预览层,可查不写):术语标准的"章名带英文"条目、个别纯中文术语(如 GB-50007"2 三桩承台")
# 可能被误计入,故结果仅作告警/预览用,不自动入库。
_SKIP_TOC_BODY = re.compile(r'^(前\s*言|目\s*[次录]|Contents|附[：:]?\s*条文说明|引用标准|用语说明|用词说明|总说明)')


def extract_chapters_from_body(md_text):
    """从 MD 正文 `##` 标题抽 章/节/附录 (只读,返回 [(clause_no, title, level)])。

    目录段 OCR 压成一行、parse_toc 抽不到时,正文标题往往仍完好——用它对扫描/OCR 烂的
    规范做交叉探测(见 main() 的 0 条告警分支)。
    """
    out, seen = [], set()
    for line in md_text.splitlines():
        if not line.startswith('## '):
            continue
        t = line[3:].strip()
        if _SKIP_TOC_BODY.match(t):
            continue
        t = re.sub(r'\s+', '', t)
        m = re.match(r'^附录\s*([A-Z])\.?(.*)$', t)
        if m:
            k = ('appendix', m.group(1))
            if k not in seen:
                seen.add(k)
                out.append(('附录' + m.group(1), m.group(2) or ('附录' + m.group(1)), 'appendix'))
            continue
        m = re.match(r'^(\d{1,2})\.?(\d{1,2})?\.?\s*(.*)$', t)
        if not m:
            continue
        a, b, title = m.group(1), m.group(2), m.group(3)
        if b:
            # X.Y 后 title 以数字开头 => 实际是 X.Y.Z 更深层,跳过(防术语污染)
            if re.match(r'^\d', title):
                continue
            k = ('section', a + '.' + b)
            if k not in seen:
                seen.add(k)
                out.append((a + '.' + b, title, 'section'))
        else:
            if not title:
                continue  # 空标题(OCR 残留如 '## 4')跳过
            if re.search(r'[a-zA-Z]', title):
                continue  # 章名含英文 => 术语条目(术语库污染),跳过
            k = ('chapter', a)
            if k not in seen:
                seen.add(k)
                out.append((a, title, 'chapter'))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只统计,不写库')
    ap.add_argument('--purge', action='store_true',
                    help='清空 clause_chapters 表(配合回填;不读用户输入直接清,先看 dry-run 预览)')
    ap.add_argument('--validate', action='store_true',
                    help='扫所有规范的 chapters + clauses 结构性问题,产出报告')
    ap.add_argument('--fix-orphans', action='store_true',
                    help='自动补缺失的父章节到 chapters 表(立;默认 dry_run 模式,加 --apply 才真改)')
    ap.add_argument('--apply', action='store_true',
                    help='配合 --fix-orphans:实际写入数据库(无 --apply 默认 dry-run 预览)')
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # --fix-orphans 模式:扫所有规范,自动补缺失父章节
    if args.fix_orphans:
        from stdq import fix_all_orphan_parents
        if args.apply:
            results = fix_all_orphan_parents(dry_run=False)
            print(f'=== 自动补缺失父章节(实写) ===\n')
            total_added = 0
            for std_code, added in sorted(results.items()):
                print(f'[{std_code}] 补了 {len(added)} 条:')
                for a in added[:5]:
                    print(f'  + §{a["clause_no"]:8s} level={a["level"]:8s} title={a["title"]}')
                if len(added) > 5:
                    print(f'  ...(还有 {len(added) - 5} 条)')
                total_added += len(added)
            print(f'\n=== 总计: 6 本规范共补 {total_added} 条章节 ===')
        else:
            # dry-run
            print(f'=== 自动补缺失父章节(dry-run 预览) ===\n')
            print('(加 --apply 才会真写入)\n')
            results = fix_all_orphan_parents(dry_run=True)
            total = 0
            for std_code, items in sorted(results.items()):
                cnt = items[0].get('count', 0)
                print(f'  [{std_code}] 需补 {cnt} 条父章节')
                total += cnt
            print(f'\n=== 总计: {total} 条需补 ===')
        con.close()
        return

    # --validate 模式:跑校对 pipeline,产出报告
    if args.validate:
        from ..parser.toc import validate_chapter_tree
        # 取所有有 chapters 行的规范
        cur.execute("SELECT DISTINCT std_code FROM clause_chapters ORDER BY std_code")
        std_codes = [r[0] for r in cur.fetchall()]
        print(f'=== 校对 pipeline 扫描 {len(std_codes)} 本规范 ===\n')
        total_issues = 0
        std_with_issues = 0
        for std_code in std_codes:
            issues = validate_chapter_tree(std_code)  # dict[str, list[str]],5 个分类
            # 5 个分类都空 = 该规范无问题
            cat_nonempty = {k: v for k, v in issues.items() if v}
            if not cat_nonempty:
                continue
            std_with_issues += 1
            cat_cnt = sum(len(v) for v in cat_nonempty.values())
            total_issues += cat_cnt
            detail = ', '.join(f'{k}={len(v)}' for k, v in cat_nonempty.items())
            print(f'[{std_code}] {cat_cnt} 个 issue ({detail})')
            for cat, items in cat_nonempty.items():
                for i in items[:5]:
                    print(f'  - [{cat}] {i}')
                if len(items) > 5:
                    print(f'    ...(该分类还有 {len(items) - 5} 个)')
            print()
        print(f'=== 校对总结 ===')
        print(f'扫描规范: {len(std_codes)} 本')
        print(f'有问题的规范: {std_with_issues} 本({std_with_issues*100//len(std_codes)}%)')
        print(f'总 issue 数: {total_issues} 条')
        con.close()
        return

    # 建表(与 stdq.py cmd_rebuild 一致)
    cur.execute('''CREATE TABLE IF NOT EXISTS clause_chapters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        std_code TEXT NOT NULL,
        clause_no TEXT NOT NULL,
        title TEXT NOT NULL,
        level TEXT NOT NULL,
        parent_clause_no TEXT,
        UNIQUE(std_code, clause_no)
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chapters_std ON clause_chapters(std_code)')
    if not args.dry_run:
        con.commit()

    # --purge:列出当前要清空的内容,等用户确认(数据操作前先 SELECT 预览)
    if args.purge and not args.dry_run:
        cur.execute('SELECT COUNT(*) FROM clause_chapters')
        total = cur.fetchone()[0]
        cur.execute('SELECT COUNT(DISTINCT std_code) FROM clause_chapters')
        std_count = cur.fetchone()[0]
        print(f'⚠️  --purge 将清空 clause_chapters 全部 {total} 条(覆盖 {std_count} 本规范)')
        # 列前 5 条预览
        cur.execute('SELECT std_code, clause_no, title FROM clause_chapters ORDER BY id LIMIT 5')
        print('预览(前 5 条):')
        for r in cur.fetchall():
            print(f'  {r[0]} §{r[1]} | {r[2][:40]}')
        print()
        # 修:非交互环境下不读 stdin,直接要求用户二次确认
        confirm = input('确认清空?输入 yes 继续,其他取消: ')
        if confirm.strip().lower() != 'yes':
            print('取消,未清空')
            con.close()
            return
        cur.execute('DELETE FROM clause_chapters')
        con.commit()
        print(f'✅ 已清空 {total} 条')

    # 查所有有 md_file 的规范
    cur.execute("SELECT code, md_file FROM standards WHERE md_file IS NOT NULL AND md_file != ''")
    rows = cur.fetchall()
    print(f'待处理: {len(rows)} 本规范')

    ok = 0
    skipped = 0
    total_chapters = 0
    for std_code, md_file in rows:
        if not os.path.exists(md_file):
            skipped += 1
            print(f'  ⏭️  {std_code}: MD 文件不存在 {md_file}')
            continue
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                md_text = f.read()
        except (UnicodeDecodeError, IOError) as e:
            skipped += 1
            print(f'  ⏭️  {std_code}: 读取失败 {e}')
            continue

        entries = parse_toc(md_text)
        if not entries:
            # 目录段抽 0 条的静默失败兜底——交叉看正文标题还能不能恢复
            # 注意:术语标准(消防词汇/术语标准)无传统目录,parse_toc 抽 0 是正常(走正文里的节标题),不告警
            is_term = bool(re.search(r'消防词汇|术语标准', os.path.basename(md_file) or ''))
            body = extract_chapters_from_body(md_text)
            n_chap = sum(1 for x in body if x[2] == 'chapter')
            n_sec = sum(1 for x in body if x[2] == 'section')
            skipped += 1
            if is_term:
                print(f'  ⏭️  {std_code}: 术语标准无传统目录,走正文节标题流程(见 docs/ingest-sop.md)')
            elif body:
                print(f'  ⚠️  {std_code}: 目录识别 0 条,但正文 ## 标题抽到 {len(body)} 条(章{n_chap} 节{n_sec})'
                      f' — 目录段疑似 OCR 烂,建议视觉/人工补章节,未自动写入')
                for x in body[:6]:
                    print(f'      预览 {x[2]:9s} §{x[0]:8s} {x[1]}')
            else:
                print(f'  ⚠️  {std_code}: 目录识别 0 条且正文亦无章节标题(可能纯扫描件,需视觉)')
            continue

        for ce in entries:
            cn = ce['clause_no']
            if cn.startswith('附录'):
                parent = None
            elif '.' in cn:
                # 去掉最后一段:4.1 → 4;4.1.1 → 4.1(修,跟 stdq.py 对齐)
                parts = cn.split('.')
                parent = '.'.join(parts[:-1])
            else:
                parent = cn
            if not args.dry_run:
                cur.execute('''INSERT OR IGNORE INTO clause_chapters
                               (std_code, clause_no, title, level, parent_clause_no)
                               VALUES (?, ?, ?, ?, ?)''',
                            (std_code, cn, ce['title'], ce['level'], parent))
        ok += 1
        total_chapters += len(entries)

    if not args.dry_run:
        con.commit()

    # 统计
    cur.execute('SELECT COUNT(*) FROM clause_chapters')
    db_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT std_code) FROM clause_chapters")
    db_std_count = cur.fetchone()[0]

    print()
    print(f'--- {"DRY-RUN" if args.dry_run else "写入完成"} ---')
    print(f'扫描规范: {len(rows)} 本')
    print(f'  成功: {ok} 本')
    print(f'  跳过(无目录): {skipped} 本')
    print(f'新增章节: {total_chapters} 条(本次)')
    print(f'库里章节总数: {db_count} 条,覆盖 {db_std_count} 本规范')

    con.close()


if __name__ == '__main__':
    main()