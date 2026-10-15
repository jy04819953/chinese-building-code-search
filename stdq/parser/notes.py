"""parser · notes —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from ..consts import NOTES_KEYWORDS, RE_APX_STOP, RE_APX_STOP_FUZZY, RE_TOC_PAGENUM


from .common import expand_clause_range


def extract_notes(content, clause_nums=None):
    """从MD内容中提取条文说明（目录定位 + 正则切分，零token消耗）"""
    def _is_toc_line(s):
        """判断是否是目录行或非说明内容"""
        if '…' in s or '……' in s:
            return True
        # · 后面跟着数字才是目录（如 "4.1防水涂料· 42"），人名间隔号不算
        if re.search(r'·\s*\d', s):
            return True
        # 短行 + 以数字或页码结尾
        if len(s) < 50 and re.search(r'(\d{1,3}\s*\)?\s*$|[\(（]\d{1,3}[\)）]\s*$)', s):
            return True
        # 公式行（如 "1.1.2G+1.0×1.4W"）
        if len(s) < 40 and re.search(r'[GgWw]\s*[+\-×÷]', s):
            return True
        return False
    
    lines = content.split('\n')
    
    # ===== 第1步：识别该规范使用的说明类关键词 =====
    toc_text = '\n'.join(lines[:120])
    found_kw = None
    for kw in NOTES_KEYWORDS:
        if kw in toc_text:
            found_kw = kw
            break
    
    # 前120行没找到，则搜索全文（处理目录嵌在正文中的情况）
    if not found_kw:
        for i, line in enumerate(lines):
            for kw in NOTES_KEYWORDS:
                if kw in line.strip() and len(line.strip()) < 100 and i > 100:
                    found_kw = kw
                    break
            if found_kw:
                break
    
    if not found_kw:
        return []  # 全文无说明类关键词 → 规范无条文说明
    
    # ===== 第2步：在120行之后找包含该关键词的标题行 =====
    note_header = -1
    for i in range(120, len(lines)):
        stripped = lines[i].strip()
        if found_kw in stripped and len(stripped) < 100:
            # 跳过目录条目（短行 + 以数字结尾）
            if len(stripped) < 50 and re.search(r'\d{1,3}\s*$', stripped):
                continue
            note_header = i
            break
    if note_header < 0:
        return []
    
    notes = []
    # ===== 第3步：找第一个章标题（如 "# 1总则" 或 "# 四、条文说明"），跳过制订说明/目次等 =====
    actual_start = -1
    for i in range(note_header + 1, min(note_header + 200, len(lines))):
        stripped = lines[i].strip()
        if stripped.startswith('#') and re.search(r'#\s*[\d一二三四五六七八九十]', stripped):
            # 章标题行本身可能嵌有条文说明（如 "# 1总则1.0.1..."）
            embedded = re.search(r'[\u4e00-\u9fff](\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)', stripped)
            if embedded:
                cn = embedded.group(1)
                if clause_nums and cn not in clause_nums:
                    actual_start = i + 1
                    continue
                rest = stripped[embedded.start(1) + len(cn):].strip()
                if rest and len(rest) >= 5:
                    notes.append((cn, rest, cn))
            actual_start = i + 1  # 内容从章标题下一行开始
            break
    
    # 降级：如果没找到章标题，找第一个以条文号开头的非目录行
    if actual_start < 0:
        for i in range(note_header + 1, min(note_header + 200, len(lines))):
            stripped = lines[i].strip()
            if re.match(r'^\d{1,2}\.\d{1,2}(?:\.\d{1,2})?(?=[^.\d])', stripped) and not _is_toc_line(stripped):
                actual_start = i
                break
    
    if actual_start < 0:
        return []
    
    # ===== 第4步：逐行扫描，按条文号切分 =====
    clause_re = r'(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)'
    # 范围式条文号（X.X.X～X.X.X 或 X.X.X、X.X.X、X.X.X）
    range_re = re.compile(r'^(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*[～~—–-]\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)')
    multi_re = re.compile(r'^(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(?:[、,]\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?))+')
    current_clause = None
    current_lines = []
    
    def _expand_and_save(clause_no, note_text):
        """保存说明，支持范围展开和多条文号"""
        if len(note_text) < 5:
            return
        # 过滤章标题（两级条文号 + 无实质说明内容）
        if re.match(r'^\d{1,2}\.\d{1,2}$', clause_no) and len(note_text) < 60:
            return
        # 检查范围式：clause_no～X.X.X 或 clause_no~X.X.X
        range_m = re.match(r'^[～~—–-]\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)', note_text)
        if range_m:
            end_clause = range_m.group(1)
            expanded = expand_clause_range(f"{clause_no}~{end_clause}")
            # 说明内容从范围结束之后开始
            content = note_text[range_m.end():].strip()
            if not content:
                content = note_text
            for cn in expanded:
                notes.append((cn, content, cn))
        elif re.match(r'^[、,]\s*' + clause_re, note_text):
            # 多条文号：从说明文本开头提取所有条文号
            all_clauses = re.findall(clause_re, clause_no + note_text)
            # 说明内容从最后一个条文号之后开始
            last_cn = all_clauses[-1] if all_clauses else clause_no
            idx = note_text.rfind(last_cn)
            content = note_text[idx + len(last_cn):].strip() if idx >= 0 else note_text
            if not content:
                content = note_text
            for cn in all_clauses:
                notes.append((cn, content, cn))
        else:
            notes.append((clause_no, note_text, clause_no))
    
    for i in range(actual_start, len(lines)):
        stripped = lines[i].strip()
        
        m = re.match(r'^' + clause_re + r'(?=[^.\d])', stripped)
        if m and not _is_toc_line(stripped):
            candidate = m.group(1)
            # 过滤OCR假条文号：如果不在正文条文号集合里，跳过
            # （如 "5.4.21" 实为 5.4.2 的子条款1，正文没有5.4.21）
            if clause_nums and candidate not in clause_nums:
                continue
            # 新条文号出现：保存上一条
            if current_clause and current_lines:
                note_text = ' '.join(current_lines).strip()
                _expand_and_save(current_clause, note_text)
            current_clause = candidate
            rest = stripped[m.end():].strip()
            current_lines = [rest] if rest else []
        elif stripped.startswith('#'):
            # 先保存之前积累的说明
            if current_clause and current_lines:
                note_text = ' '.join(current_lines).strip()
                _expand_and_save(current_clause, note_text)
                current_clause = None
                current_lines = []
            # 章标题行：检查是否内嵌了条文号说明
            # 格式4: # X.X.X内容（条文号直接跟在 # 后）
            fmt4 = re.match(r'^#\s+(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)', stripped)
            if fmt4:
                cn = fmt4.group(1)
                # 同样过滤OCR假条文号
                if clause_nums and cn not in clause_nums:
                    continue
                rest = stripped[fmt4.end():].strip()
                if rest:
                    if current_clause and current_lines:
                        note_text = ' '.join(current_lines).strip()
                        _expand_and_save(current_clause, note_text)
                    current_clause = cn
                    current_lines = [rest]
                continue
            embedded = re.search(r'[\u4e00-\u9fff](\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)', stripped)
            if embedded:
                cn = embedded.group(1)
                # 过滤OCR假条文号
                if clause_nums and cn not in clause_nums:
                    continue
                rest = stripped[embedded.start(1) + len(cn):].strip()
                if rest and len(rest) >= 5:
                    _expand_and_save(cn, rest)
            continue
        elif current_clause:
            # 目录行跳过
            if _is_toc_line(stripped):
                continue
            if stripped:
                current_lines.append(stripped)
    
    # 最后一条
    if current_clause and current_lines:
        note_text = ' '.join(current_lines).strip()
        _expand_and_save(current_clause, note_text)
    
    return notes

def is_notes_marker(line):
    """判断是否"条文说明"区标记行（含页眉式变体）。"""
    s = (line or '').strip()
    if RE_APX_STOP.match(s):
        return True
    return len(s) <= 32 and bool(RE_APX_STOP_FUZZY.match(s))

def _notes_start_index(lines):
    """说明区起点行号；跳过目录行（含 …… 或末尾页码）。"""
    for j, l in enumerate(lines):
        s = (l or '').strip()
        if '…' in s or RE_TOC_PAGENUM.search(s):
            continue
        if is_notes_marker(s):
            return j
    return None
