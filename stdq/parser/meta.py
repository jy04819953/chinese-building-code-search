"""parser · meta —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from ..consts import _STD_CODE_RE


def _find_announcement_section(content):
    """找到公告段（前~100行，'发布/公告'到'前言'之间）。"""
    lines = content.split('\n')
    announce_start = -1
    for i, line in enumerate(lines[:100]):
        if '公告' in line or ('发布' in line and '实施' in line):
            announce_start = i
            break
    if announce_start < 0:
        announce_start = 0
    announce_end = min(100, len(lines))
    for i in range(announce_start + 1, min(announce_start + 200, len(lines))):
        line = lines[i].strip()
        if re.match(r'^#+\s*前\s*言', line) or re.match(r'^#+\s*前言', line):
            announce_end = i
            break
        if re.match(r'^#+\s*1\s*总则', line):
            announce_end = i
            break
    return '\n'.join(lines[announce_start:announce_end])

def _normalize_abolished_code(code_str):
    """规范化废止条文中出现的规范代号。"""
    s = code_str.strip()
    s = re.sub(r'\s*[（(]\d{4}年?版[）)]', '', s)
    s = re.sub(r'\s*[（(]\d{4}版[）)]', '', s)
    s = s.replace('—', '-').replace('–', '-').replace('━', '-')
    s = re.sub(r'\s+', '-', s)
    # 修复 OCR 缺连字符: GB503682005 → GB50368-2005

    if re.match(r'^[A-Z]+-\d', s):
        return s
    m = re.match(r'^([A-Z]{2,})(\d.*)$', s)
    if m:
        return f'{m.group(1)}-{m.group(2)}'
    return s

def parse_strong_from_announcement(content):
    """从公告段提取精确的强条条文号列表。
    
    返回: (is_full_mandatory: bool, strong_clause_nos: set|None)
      - (True, None) → 全文强条
      - (False, set()) → 推荐性标准，无强条
      - (False, {set}) → 部分强条，精确列表
      - (False, None) → 未识别（降级到关键词）
    """
    if re.search(r'本(?:标准|规范|规程)\s*(?:为|系)\s*全文\s*强制', content):
        return True, None
    if re.search(r'全部条文必须严格执行', content):
        return True, None
    
    announce = _find_announcement_section(content)
    if not announce:
        announce = content  # 降级搜索全文
    
    m = re.search(
        r'其中[，,]?\s*第\s*(.+?)条[（(]?款?[）)]?\s*为强制性条文',
        announce
    )
    if not m:
        m = re.search(
            r'第\s*(\d[\d.、，,\s（()）～]+)条[（(]?款?[）)]?\s*为强制性条文',
            announce
        )
    # 公告段没找到时降级搜索全文
    if not m and announce != content:
        m = re.search(
            r'其中[，,]?\s*第\s*(.+?)条[（(]?款?[）)]?\s*为强制性条文',
            content
        )
        if not m:
            m = re.search(
                r'第\s*(\d[\d.、，,\s（()）～]+)条[（(]?款?[）)]?\s*为强制性条文',
                content
            )
    
    # 格式3: "其中，4.0.1、4.0.2、4.0.4、4.0.6为强制性条文"（无"第""条"）
    if not m:
        m = re.search(
            r'其中[，,]?\s*(\d[\d.、，,\s]+)为强制性条文',
            content
        )
    
    if not m:
        if '强制性条文' not in content:
            return False, set()
        return False, None
    
    clause_nos = set()
    for cm in re.finditer(r'(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)', m.group(1)):
        clause_nos.add(cm.group(1))
    return False, clause_nos

def parse_abolished_from_announcement(content, current_std_code=None):
    """从公告段提取废止条文信息。
    
    返回: list[dict]，每个 dict:
      - target_std: 被废止的规范代号
      - target_name: 被废止的规范名称
      - clause_nos: 被废止的条文号列表（整本废止时为 None）
      - abolish_type: 'whole' 或 'clauses'
    """
    announce_text = _find_announcement_section(content)
    if not announce_text:
        return []
    # 清理HTML标签（<sup><sub>等），防止干扰正则匹配
    announce_text = re.sub(r'<[^>]+>', '', announce_text)
    
    results = []
    seen = set()
    
    # 整本废止: 找"同时废止"前面的所有《名称》代号对
    for cancel_match in re.finditer(r'同时废止', announce_text):
        before = announce_text[max(0, cancel_match.start() - 600):cancel_match.start()]
        # 匹配 《名称》代号（代码可选在括号里）
        std_pair_re = re.compile(
            r'[《「]([^》」]+)[》」]\s*(?:[（(]\s*)?' + _STD_CODE_RE + r'(?:\s*[）)])?'
        )
        for sm in std_pair_re.finditer(before):
            target_name = sm.group(1).strip()
            target_code = _normalize_abolished_code(sm.group(2))
            if current_std_code and target_code == current_std_code:
                continue
            key = (target_code, None)
            if key not in seen:
                seen.add(key)
                results.append({
                    'target_std': target_code,
                    'target_name': target_name,
                    'clause_nos': None,
                    'abolish_type': 'whole'
                })
    
    # 部分条文废止 (编号列表)
    list_start = None
    m_a = re.search(r'同时废止下列[^\n]*[：:。.]', announce_text)
    if m_a:
        list_start = m_a.end()
    if list_start is None:
        m_b = re.search(r'和下列[^\n]*同时废止[^\n]*[。.]', announce_text)
        if m_b:
            list_start = m_b.end()
    # 兜底：找"废止"标题行，从下一行开始解析编号列表（适配GB 55015/55019等无引导词格式）
    if list_start is None:
        m_c = re.search(r'#+\s*废止[^\n]*\n', announce_text)
        if m_c:
            list_start = m_c.end()
    
    if list_start is not None:
        remaining = announce_text[list_start:]
        item_re = re.compile(
            r'(?:[一二三四五六七八九十]+|\d+)[、.]\s*'
            r'[《「]([^》」]+)[》」]'
            r'\s*(?:[（(]\s*)?' + _STD_CODE_RE + r'(?:\s*[）)])?' +
            r'\s*(?:[（(]\d{4}年?版[）)])?\s*第?\s*([\d.、，,\s（()）～A-Za-z-]+?)(?:条[（(]?款?[）)]?)'
        )
        for im in item_re.finditer(remaining):
            clause_str = im.group(3)
            clause_nos = []
            for cm in re.finditer(r'(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)', clause_str):
                clause_nos.append(cm.group(1))
            if clause_nos:
                target_code = _normalize_abolished_code(im.group(2))
                target_name = im.group(1).strip()
                key = (target_code, tuple(clause_nos))
                if key not in seen:
                    seen.add(key)
                    results.append({
                        'target_std': target_code,
                        'target_name': target_name,
                        'clause_nos': clause_nos,
                        'abolish_type': 'clauses'
                    })
    
    return results

def detect_all_strong(content):
    if re.search(r'本(?:标准|规范|规程)\s*(?:为|系)\s*全文\s*强制', content):
        return True
    if re.search(r'全部条文必须严格执行', content):
        return True
    return False

def is_strict_clause(text):
    """含严格表述（必须/严禁/不得/不应）的条文，不是强条但需注意"""
    return any(kw in text for kw in ['必须', '严禁', '不得', '不应'])
