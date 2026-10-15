#!/usr/bin/env python3
"""全库修复"空行断句"截断条文（共性 bug）。

原理：入库解析遇空行断句，只存了条文第一行。从对应 MD 用同一套解析逻辑
（取条文号第一次出现=正文区、空行跳过不结算、到下一条文号/章节标题才结算）
提取完整文本，与 DB 比对：
  - MD 文本更长 且 DB 文本是 MD 前缀 → 真截断，补全
  - 其他 → 跳过（误报或正常）
用法: python3 fix_truncated_clauses.py [--dry-run]
备份: 自动 cp standards.db.{ts}
"""
import os
import re
import sqlite3
import sys
import shutil
import datetime
from ..consts import STANDARDS_DIR

DB = os.path.join(STANDARDS_DIR, 'standards.db')

clause_start = re.compile(r'^(\d+\.\d+(?:\.\d+)?)(?![\da-zA-Z])\s*(.*)')
next_clause = re.compile(r'^\d+\.\d+(?:\.\d+)?(?![0-9a-zA-Z])')
chapter_hdr = re.compile(r'^#{1,6}\s')


def extract_full_texts(md_path):
    """从 MD 提取 条文号→完整文本（第一次出现=正文区）。"""
    with open(md_path, encoding='utf-8') as f:
        lines = f.readlines()
    full_texts = {}
    seen = set()
    current_no = None
    current_parts = []
    for raw in lines:
        stripped = raw.strip()
        m = clause_start.match(stripped)
        if m:
            if current_no is not None and current_no not in full_texts:
                full_texts[current_no] = ''.join(current_parts)
            no = m.group(1)
            current_no = no
            if no in seen:
                current_parts = []
            else:
                seen.add(no)
                current_parts = [m.group(2)]
        elif current_no is not None:
            if chapter_hdr.match(stripped):
                if current_no not in full_texts:
                    full_texts[current_no] = ''.join(current_parts)
                current_no = None
                current_parts = []
            elif next_clause.match(stripped):
                if current_no not in full_texts:
                    full_texts[current_no] = ''.join(current_parts)
                current_no = None
                current_parts = []
            elif stripped:
                current_parts.append(stripped)
    if current_no is not None and current_no not in full_texts:
        full_texts[current_no] = ''.join(current_parts)
    return full_texts


def normalize(t):
    # 去空白 + 去掉 rebuild 解析吃剩的空 <table> 重复
    t = re.sub(r'\s+', '', t or '').strip()
    t = re.sub(r'(<table>\s*)+$', '', t)  # 末尾连续 <table> 视为噪声
    return t


def main():
    dry = '--dry-run' in sys.argv
    db = sqlite3.connect(DB)
    c = db.cursor()

    # 候选：以连接词结尾、内容偏短（扩到 200 字符 + 加 table 截断标记）
    ending_chars = ['、', '，', '；', '等', '及', '和', '或', '于', '的', '应',
                    '采用', '采取', '设置', '符合', '满足', '不小于', '不大于',
                    '小于', '大于']
    rows = []
    seen_keys = set()
    for ec in ending_chars:
        c.execute('SELECT std_code, clause_no, content FROM clauses WHERE content LIKE ? AND length(content) < 200',
                  ('%' + ec,))
        for r in c.fetchall():
            k = (r[0], r[1])
            if k not in seen_keys:
                seen_keys.add(k)
                rows.append(r)
    # 兜底：含连续空 <table> 重复 = 表格被 rebuild 解析吃掉
    c.execute("SELECT std_code, clause_no, content FROM clauses WHERE content LIKE '%<table>%<table>%'")
    for r in c.fetchall():
        k = (r[0], r[1])
        if k not in seen_keys:
            seen_keys.add(k)
            rows.append(r)

    # 规范 md_file 映射
    c.execute('SELECT code, md_file FROM standards')
    md_map = {r[0]: r[1] for r in c.fetchall()}

    fixed = []
    skipped = []
    no_md = []
    for std_code, clause_no, db_content in rows:
        md_file = md_map.get(std_code)
        if not md_file or not os.path.exists(md_file):
            no_md.append((std_code, clause_no, db_content))
            continue
        full_texts = extract_full_texts(md_file)
        md_text_raw = full_texts.get(clause_no, '')
        md_text = normalize(md_text_raw)  # 用于比对
        db_norm = normalize(db_content)
        if not md_text:
            skipped.append((std_code, clause_no, db_content, 'MD无此条文号'))
            continue
        if len(md_text) > len(db_norm) and db_norm and md_text.startswith(db_norm):
            fixed.append((std_code, clause_no, db_norm, md_text_raw))  # UPDATE 用原文保格式
        else:
            # 误报或 MD 文本不同（可能是提取不准）
            skipped.append((std_code, clause_no, db_content, '非前缀截断/误报'))

    print(f'候选 {len(rows)} 条 | 确认截断 {len(fixed)} | 跳过 {len(skipped)} | 无MD {len(no_md)}')

    if fixed:
        print('\n=== 确认截断，待修复 ===')
        from collections import Counter
        cnt = Counter(r[0] for r in fixed)
        for std, n in cnt.most_common():
            print(f'  {std}: {n} 条')
        print(f'\n共 {len(fixed)} 条')
        for std_code, clause_no, db_norm, md_text in fixed[:30]:
            print(f'  {std_code} §{clause_no}:')
            print(f'    DB: {db_norm[:60]}')
            print(f'    MD: {md_text[:80]}')

    if not dry and fixed:
        # 备份
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        bak = f'{DB}.bak.{ts}-fix-truncated'
        shutil.copy2(DB, bak)
        print(f'\n备份: {bak}')
        for std_code, clause_no, db_norm, md_text in fixed:
            c.execute("UPDATE clauses SET content=? WHERE std_code=? AND clause_no=?",
                      (md_text, std_code, clause_no))
        db.commit()
        print(f'已更新 {len(fixed)} 条')
    db.close()


if __name__ == '__main__':
    main()
