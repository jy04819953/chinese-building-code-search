"""parser · term —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from ..consts import _GLUED_TERM_RE, _TERM_HEAD_RE, _TERM_LINE_RE, _TERM_NUM_ONLY_RE


from .common import is_doc_tail_head


def _looks_like_term_standard(content, filename=''):
    """判断 MD 是否为术语标准(消防词汇/术语标准)。

    优先用文件名(最可靠): 含 '消防词汇' 或 '术语标准' 直接判定。
    文件名不可用时(纯内容调用)退化为内容特征:
    ① 含 '术语和定义'/'通用术语'/'消防词汇' 章节标题;
    ② 存在 '编号单独行 + ##术语行' 组合(5907 主格式) 或
       '编号+中文+英文' 同行格式(50504 主格式) ≥ 5 处。
    """
    if filename and ('消防词汇' in filename or '术语标准' in filename):
        return True
    # 文件名已给出但不是术语标准 → 直接 False(文件名是唯一可靠判据)
    if filename:
        return False
    # 无文件名(纯内容调用,测试/调试场景)才用内容兜底,阈值取高防误伤
    if not any(k in content for k in ('术语和定义', '术语与定义', '通用术语', '专用术语', '消防词汇')):
        return False
    lines = content.split('\n')
    num_only = 0
    term_line = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if _TERM_LINE_RE.match(s):
            term_line += 1
        if _TERM_NUM_ONLY_RE.match(s):
            num_only += 1
            # 编号单独行后(可隔空行)跟 ##术语行 → 术语标准特征
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _TERM_HEAD_RE.match(lines[j].strip()):
                num_only += 10
    return (term_line >= 5) or (num_only >= 5)

def extract_term_clauses(content):
    """从术语标准 MD 中提取术语条目。

    返回 [(clause_no, content_text), ...]，content_text = "术语 英文\\n定义"。

    支持的格式:
    A. ## 编号+术语+英文 同行:  ## 2.16火灾预防 fireprevention
    B. 编号单独行 + ## 术语行:  ## 2.1.1.2\\n## 火灾探测器 firedetector
    C. 无 ## 前缀同行:         2.3.12用地红线图 map of red line...
    D. 4级编号:                ## 2.1.1.1火灾报警触发器件 firealarmtriggerpart

    定义行: 术语行之后直到下一个术语行/编号行/章节标题的所有非空行合并。
    """
    lines = content.split('\n')
    terms = []
    i = 0
    n = len(lines)

    # 找正文起点: 跳过封面/前言/目次,定位到"X 术语和定义"章节
    body_start = 0
    for idx, l in enumerate(lines):
        if re.match(r'^#+\s*\d+\s*[术语通]', l.strip()) and '术语' in l:
            body_start = idx
            break
    i = body_start

    def _is_terminal(t):
        """判断一行是否是术语边界(新术语行/编号行/章节标题)"""
        return bool(_TERM_LINE_RE.match(t) or _GLUED_TERM_RE.match(t)
                    or _TERM_NUM_ONLY_RE.match(t)
                    or _TERM_HEAD_RE.match(t)
                    or re.match(r'^#+\s*\d+\s*[术语通]', t))

    while i < n:
        s = lines[i].strip()
        # 标准尾部整块（用词说明/引用标准名录/参考文献）→ 停止（后面的参考文献正文
        #   行不含 # 前缀，老逻辑会把它并进上一条术语定义）
        if is_doc_tail_head(s):
            break
        m = _TERM_LINE_RE.match(s)
        if not m:
            m = _GLUED_TERM_RE.match(s)
        if m:
            num, term, eng = m.group(1), m.group(2), m.group(3).strip()
            # 收集定义行: 术语行后到下一个边界
            defs = []
            j = i + 1
            while j < n:
                t = lines[j].strip()
                if not t:
                    j += 1
                    continue
                if _is_terminal(t):
                    break
                # 修: 章节标题行不算定义。老逻辑只把"章级术语标题"当边界，
                #   节标题(如 '## 2.1.2火灾报警控制装置')被并进上一条术语定义
                #   （实测 GBT-5907-5-2015 §2.1.1.20 尾部黏 '## 2.1.2火灾报警控制装置'，全库 61 条）
                if t.startswith('#'):
                    break
                defs.append(t)
                j += 1
            full = term + ' ' + eng
            if defs:
                full += '\n' + ' '.join(defs)
            terms.append((num, full))
            i = j
            continue
        # 编号单独行: 下一行(或隔空行)是 ## 术语行
        mn = _TERM_NUM_ONLY_RE.match(s)
        if mn:
            num = mn.group(1)
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                t = lines[j].strip()
                mh = _TERM_HEAD_RE.match(t)
                if mh:
                    term, eng = mh.group(1), mh.group(2).strip()
                    defs = []
                    k = j + 1
                    while k < n:
                        tt = lines[k].strip()
                        if not tt:
                            k += 1
                            continue
                        if _is_terminal(tt):
                            break
                        if tt.startswith('#'):      # 章节标题行不算定义（同上）
                            break
                        defs.append(tt)
                        k += 1
                    full = term + ' ' + eng
                    if defs:
                        full += '\n' + ' '.join(defs)
                    terms.append((num, full))
                    i = k
                    continue
            i = j
            continue
        i += 1

    # 去重(与 extract_clauses 行为一致)
    seen = set()
    unique = []
    for no, text in terms:
        if no not in seen:
            seen.add(no)
            unique.append((no, text))
    return unique
