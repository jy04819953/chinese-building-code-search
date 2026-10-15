"""parser · common —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from ..consts import RE_DOC_TAIL_HEAD


def is_valid_clause_content(text):
    if not text or len(text.strip()) < 3:
        return False
    t = text.strip()
    # 以顿号/逗号开头 → 条文号引用列表的延续
    if t.startswith('、') or t.startswith('，') or t.startswith(','):
        return False
    # 纯数字、标点、空格 → 无效
    if re.match(r'^[\s\d\.…·⭐、，,;；:：()（）\[\]【】]+$', t):
        return False
    # TOC目录条目：含省略号（……/··/…）或「· + 页码」点线
    # 修: 原为 `'·' in t` → 正文含间隔号必被整条丢弃。
    # 而 `·` 是单位写法的一部分(W/(m²·K) / kV·A / L/(人·d) / L/(m²·min)),
    # 会导致一批正文被误杀(判据需按规范体例调节)。
    # 已在 `_is_toc_line` 用同一思路修过(只认「· + 数字」),此处漏改,本次补齐。
    # 比 `_is_toc_line` 的 `·\s*\d` 更严:锚定行尾且兼容 `· (74)` 这种带括号页码的目录写法。
    if '……' in t or '··' in t or '…' in t or re.search(r'·\s*\(?\d+\)?\s*$', t):
        return False
    if re.search(r'\.\s*\(\d+\)', t):
        return False
    # 末尾有数字且内容较短 → 可能是页码（如"套内前厅7"）
    if re.search(r'\d$', t) and len(t) < 15:
        # 检查是否没有标点符号（正常条文应该有标点）
        if not re.search(r'[，。,.;：:、]', t):
            return False
    # 条文号列表模式：至少3个 "X.X.X" 被顿号/逗号分隔
    if len(re.findall(r'\d{1,2}\.\d{1,2}', t)) >= 3:
        # 如果中文字符占比极低，判定为条文号引用列表
        # 修: 阈值 0.2 → 0.05。原阈值把"正文以公式为主"的条文误杀——
#   实测：有一批条文的正文大面积是 LaTeX 公式、中文占比极低(约 0.09~0.18),
        #   被整条丢弃。而真正该拦的"废止条文清单"(如 "A、5.4.4、5.4.4A、5.5.8…")中文占比 **0.0**,
        #   0.05 能干净分开两者(实测全库仅 12 行落在这个判定里,两组无重叠)。
        cn_chars = len(re.findall(r'[\u4e00-\u9fff]', t))
        total = len(t)
        if total > 0 and cn_chars / total < 0.05:
            return False
    return True

def _is_clause_number(line):
    """检查行是否以条文号 X.X.X 开头。

    要求条文号后紧跟中文字符或空格，以区分条文号和编号子条款（如 '1楼梯'）。
    条文号格式: X.X.X内容 或 X.X.X.X内容（三级条文号）
    子条款格式: 1内容, 2内容（单个数字后直接跟中文）

    修: 允许数字编号后接数字(如"87型雨水斗"),正则原本要求紧跟中文/#
    会导致子条款"5.2.37 87型..."里的"5.2.37"被误判为续行,实际它就是新的条文号。
    """
    s = line.strip()
    # 匹配 X.X.X 后面紧跟中文字符/数字/字母/#的情况
    return bool(re.match(r'^(?:#{1,6}\s*)?(?:第\s*)?\d{1,2}\.\d{1,2}(?:\.\d{1,2})?(?:\s*条)?\s*[\u4e00-\u9fff#0-9a-zA-Z]', s))

def is_doc_tail_head(line):
    """是否"标准尾部整块"的标题行（用词说明/引用标准名录/参考文献）。"""
    return bool(RE_DOC_TAIL_HEAD.match(line or ''))

def expand_clause_range(range_str):
    """展开条文范围，如 '4.1.4～4.1.6' → ['4.1.4', '4.1.5', '4.1.6']"""
    # 处理 ～ 或 ~ 分隔的范围
    range_str = range_str.replace('～', '~').replace('—', '~').replace('–', '~')
    
    if '~' in range_str:
        parts = range_str.split('~')
        if len(parts) == 2:
            start = parts[0].strip()
            end = parts[1].strip()
            
            # 解析起止条文号
            start_match = re.match(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?', start)
            end_match = re.match(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?', end)
            
            if start_match and end_match:
                s1, s2 = int(start_match.group(1)), int(start_match.group(2))
                s3 = int(start_match.group(3)) if start_match.group(3) else None
                e1, e2 = int(end_match.group(1)), int(end_match.group(2))
                e3 = int(end_match.group(3)) if end_match.group(3) else None
                
                result = []
                # 简单情况：只最后一级不同
                if s1 == e1 and s2 == e2:
                    if s3 is not None and e3 is not None:
                        for i in range(s3, e3 + 1):
                            result.append(f"{s1}.{s2}.{i}")
                    elif s3 is None and e3 is None:
                        result.append(f"{s1}.{s2}")
                # 二级范围
                elif s1 == e1:
                    for i in range(s2, e2 + 1):
                        if s3 is not None and i == s2:
                            for j in range(s3, 10):  # 从s3到9
                                result.append(f"{s1}.{i}.{j}")
                        elif e3 is not None and i == e2:
                            for j in range(0, e3 + 1):
                                result.append(f"{s1}.{i}.{j}")
                        else:
                            result.append(f"{s1}.{i}")
                # 一级范围（罕见）
                elif s1 == e1:
                    for i in range(s2, e2 + 1):
                        result.append(f"{s1}.{i}")
                
                return result
    
    # 单一条文号
    return [range_str.strip()]
