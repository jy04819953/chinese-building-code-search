"""parser · clauses —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time

from .common import is_valid_clause_content, _is_clause_number
from .term import _looks_like_term_standard, extract_term_clauses
from .appendix import find_appendix_start, extract_appendix_clauses
from ..parser.common import is_doc_tail_head


def find_body_start(lines):
    """找规范正文起点：跳过封面/公告/前言/目次等非正文段。

    立：修公告段里"X.Y.Z条为强制性条文"被误识别为条文的 bug。
    返回应该开始扫描的 lines 索引（0-based）。

    识别模式（按优先级）：
    1. '# N 总 则' / '## N 总则' 类（中文规范最常见）
    2. '# General provisions' / '## 1 General'（英文规范）
    3. 兜底：第一个 '# 数字 ' 开头的一级标题（应对非典型命名）
    """
    # 模式1:总则(中文,带或不带空格)
    p1 = re.compile(r'^#{1,6}\s*第?[一二三四五六七八九十0-9]+\s*[.\s、]?\s*总\s*则')
    # 模式2:英文 General
    p2 = re.compile(r'^#{1,6}\s*\d*\s*General\s*[Pp]rovisions?', re.IGNORECASE)
    # 模式3:第一个 "# 数字 " 或 "# 数字\u3000"(全角空格) 或 "## 1" 开头
    p3 = re.compile(r'^#{1,6}\s*[0-9]+[\s\u3000]')

    # 模式1 优先
    for i, line in enumerate(lines):
        if p1.match(line.strip()):
            return i
    # 模式2
    for i, line in enumerate(lines):
        if p2.match(line.strip()):
            return i
    # 模式3 兜底
    for i, line in enumerate(lines):
        if p3.match(line.strip()):
            return i
    # 完全找不到,从头开始
    return 0

def extract_clauses(content, filename=''):
    """从MD内容中提取条文，支持多种格式。

    增强：当条文内容以'下列规定'等引导语结尾时，
    自动收集后续的编号子条款（1, 2, 3...）直到下一个条文号。

    开头跳过封面/公告/前言/目次(find_body_start),
    防止公告段里"X.Y.Z条为强制性条文"被误识别为条文正文。

    术语标准(消防词汇/术语标准)走专用解析——格式为
    "编号+术语+英文+定义",与常规规范"编号+条文内容"完全不同。
    filename 用于文件名判定(最可靠),内容判定作退化路径。

    附录(附录A / A.1.1 / 表A.0.1)走 extract_appendix_clauses ——
    正文循环遇到附录标题即停,附录条目单独解析后并入返回值。
    """
    if _looks_like_term_standard(content, filename):
        return extract_term_clauses(content)

    clauses = []

    lines = content.split('\n')
    apx_start = find_appendix_start(lines)
    body_start = find_body_start(lines)
    i = body_start
    while i < len(lines):
        line = lines[i].strip()
        
        # 遇到条文说明标记就停止（只提取正文，不提取说明部分）
        if re.match(r'^#\s*(条文说明|起草说明|编制说明|制订说明|附[：:]?\s*(条文说明|起草说明))', line):
            break

        # 标准尾部整块（用词说明/引用标准名录/参考文献）→ 停止（不属任何条文）
        if is_doc_tail_head(line):
            break

        # 到附录区就停 —— 附录走 extract_appendix_clauses 专用解析。
        # （apx_start 由 find_appendix_start 判"真附录标题"，避免正文里的"见附录A"误触发）
        if apx_start is not None and i >= apx_start:
            break
        
        clause_no = None
        rest = None
        
        # 格式1: X.X.X[.X] 内容 (同行，支持2-5级编号)
        m = re.match(r'^(?:#{1,6}\s*)?(?:第\s*)?(\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3})(?:\s*条)?\s*(.*)', line)
        if m:
            clause_no = m.group(1)
            rest = m.group(2).strip()
            # 去掉行尾的 ⭐☆ 标记（格式1误捕获）
            rest = re.sub(r'[⭐☆]+\s*$', '', rest).strip()
            # 过滤不合法的条文号（0.X.X 不是有效条文号）
            if clause_no.startswith('0.'):
                clause_no = None
                rest = None
            # 如果匹配的是两编号（如"2.1"），检查行里是否有三编号或四编号条文
            # 两编号通常是章标题，真正的条文号是三编号或四编号（如2.1.1或2.1.1.1）
            elif '.' not in clause_no[clause_no.find('.')+1:] or clause_no.count('.') == 1:
                # 是两编号，检查行里有没有紧跟的三编号或四编号
                three_level = re.search(r'\d{1,2}\.\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s*[\u4e00-\u9fff]', line)
                if three_level:
                    clause_no = None  # 放弃格式1，让格式4处理
        
        # 格式2: ### X.X.X[.X] ⭐ (标题格式，内容在下一行，支持2-4级编号)
        if not clause_no:
            m2 = re.match(r'^#{1,3}\s+(\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3})\s*(?:⭐)?\s*$', line)
            if m2:
                clause_no = m2.group(1)
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line and not next_line.startswith('#') and not next_line.startswith('>') and not next_line.startswith('|') and is_valid_clause_content(next_line):
                        rest = next_line
                    else:
                        rest = ''
        
        # 格式3: # X章名X.X.X[.X]内容 (章标题嵌入条文号，支持2-4级编号)
        if not clause_no:
            m3 = re.match(r'^#{1,6}\s+\d+\s*[\u4e00-\u9fff]+(\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3})(.*)', line)
            if m3:
                clause_no = m3.group(1)
                rest = m3.group(2).strip()
        
        # 格式4: 章标题/节标题与条文号合并（OCR把标题和第一条挤在一行）
                # 支持3-4级条文号（避免匹配章标题 X.X）
                # 修: 增加"数字前必须接特定标点"的约束,避免"按本标准第4.7.12条的规定"这种引用被误识别为条文号
                # 排除列表: "第" 引用/ "按" 引用/ "见" 引用/ "本标准" / "本规范" 等
                if not clause_no and len(line) > 20:
                    if '<table>' not in line:
                        # 含$$的行：去掉公式内容后再匹配（公式后面的条文号可以提取）
                        line_for_match = re.sub(r'\$\$.*?\$\$', '', line) if '$$' in line else line
                        # 数字前的字符必须为"标题/条文边界"标志(冒号/分号/句号/中文标点/+章节符)
                        # 而非"第/按/见/符合本标准"这种引用词
                        m4 = re.search(r'([：:;；。、，/+＋＝=\s#\d一二三四五六七八九十])\s*(\d{1,2}\.\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s+([\u4e00-\u9fff])', line_for_match)
                        if m4:
                            pre_char = m4.group(1).strip() if m4.group(1) else ''
                            # 排除"第X.Y.Z条"模式(pre_char 为"第"或整行为引用句)
                            if pre_char not in ('第', '见', '按', '本') and not re.search(r'(?:第|按|见)\s*' + re.escape(m4.group(2)), line_for_match[:m4.start(2)]):
                                clause_no = m4.group(2)
                                rest = line_for_match[m4.end(2):].strip()
                                if len(rest) < 5 or not re.search(r'[\u4e00-\u9fff]', rest[:30]):
                                    clause_no = None
        
        if clause_no:
            # 确定初始content
            if rest and is_valid_clause_content(rest):
                current_content = rest
            elif i + 1 < len(lines) and not rest:
                # 格式1：同行无内容，看下一行
                next_line = lines[i + 1].strip()
                if next_line and not next_line.startswith('>') and not next_line.startswith('|') and not next_line.startswith('**') and not next_line.startswith('#') and is_valid_clause_content(next_line):
                    current_content = next_line
                else:
                    current_content = ''
            else:
                current_content = ''

            # 修: 术语章(第2章)的"编号+术语名"行,定义另起一段。
            # 原逻辑只在"同行无内容"时才看下一行,而术语行同行就有术语名 → 后面的定义被跳过,
            # 会导致大量术语定义丢失(只存词名,释义没进库)。
            # 术语章内补收紧跟的纯文本段(跳空行,遇到下一条文号/标题/表格/图停)。
            if clause_no.startswith('2.') and current_content and not re.search(r'[。；;：:]\s*$', current_content):
                _j = i + 1
                _extra = []
                while _j < len(lines):
                    _jl = lines[_j].strip()
                    if not _jl:
                        _j += 1
                        continue
                    if (_jl.startswith('#') or _jl.startswith('>')
                            or _jl.startswith('|') or _jl.startswith('<table>') or _jl.startswith('!')
                            or _jl.startswith('注：') or _jl.startswith('注:')
                            or re.match(r'^\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3}(?:\s|[\u4e00-\u9fff])', _jl)):
                        break
                    _extra.append(_jl)
                    _j += 1
                if _extra:
                    current_content = current_content + '\n' + '\n'.join(_extra)

            # 检查是否需要收集子条款（内容末尾含"下列/以下/如下"引导语）
            # 覆盖：下列规定/要求/条件/标准/条款/原则/公式/方法/内容/措施/范围/参数/控制/步骤 等
            if current_content and re.search(r'(?:下列|以下|如下)\s*(?:规定|要求|条件|标准|条款|原则|公式|方法|计算|确定|设计|构造|做法|措施|指标|参数|限值|控制|内容|范围|方式|程序|步骤|分类|等级|规模|用途|功能|性能|值|之一)', current_content):
                # 确定起始收集位置
                if current_content == rest and rest:
                    # 内容来自同行，从下一行开始收集
                    collect_start = i + 1
                elif current_content and current_content != rest:
                    # 内容来自下一行，从再下一行开始收集
                    collect_start = i + 2
                else:
                    collect_start = i + 1
                
                subitems, lines_consumed = _collect_subitems(lines, collect_start)
                if subitems:
                    # 去掉每个子条款末尾的标点，统一用分号连接
                    cleaned = [s.rstrip('；;。,，') for s in subitems]
                    current_content = current_content.rstrip('：:') + ' ' + '；'.join(cleaned)
                    # 更新i到子条款结束位置（用实际消耗行数，非子条款数量）
                    i = collect_start + lines_consumed - 1
            
            # 收集紧跟的**显示公式块**（`$$` 独占一行成对出现）+「式中：」符号说明。
            #   老行为：这两类毫无收集路径（只有子条款/表格/术语章定义有）⇒ 整块落空，
            #   条文写着"应按下式确定："却没有公式（实测：独立公式块只有一部分进了库，
            #   实测：这类编号在正文里出现但库里没有的，会成批存在）。
            #   收集边界：下一个条文号 / `#` 标题 / 表格行 / `注：` / 图 —— 遇到就停。
            if current_content:
                _j, _parts, _started = i + 1, [], False
                while _j < len(lines):
                    _jl = lines[_j].strip()
                    if not _jl:
                        _j += 1
                        continue
                    if (re.match(r'^(?:#{1,6}\s+)?(?:第\s*)?\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3}', _jl)
                            or _jl.startswith('#') or _jl.startswith('<table>') or _jl.startswith('表 ')
                            or _jl.startswith('续表') or _jl.startswith('注：') or _jl.startswith('注:')
                            or _jl.startswith('图') or _jl.startswith('![')):
                        break
                    if _jl.startswith('$$'):
                        # 三种形态：① 一整行就是 `$$`（块级，收尾行也是 `$$`）
                        #            ② 单行成对 `$$公式$$`
                        #            ③ `$$` 开头但**行内**就收尾（`$$…$(m³)$$H\cdot$——…`，DG-TJ 系列大量）
                        _close_in_line = _jl.find('$$', 2)
                        if _close_in_line != -1:
                            _parts.append(_jl)
                            _j, _started = _j + 1, True
                            continue
                        _k = _j + 1
                        while _k < len(lines) and _k - _j <= 40 and '$$' not in lines[_k]:
                            _k += 1
                        if _k >= len(lines) or _k - _j > 40:
                            break       # 块没收尾/离太远 → 不收集（防吞下整章）
                        _parts.append('\n'.join(lines[_j:_k + 1]))
                        _j, _started = _k + 1, True
                        continue
                    if _started and (_jl.startswith('式中') or _jl.startswith('$')):
                        _parts.append(_jl)
                        _j += 1
                        continue
                    break
                if _parts:
                    current_content = current_content.rstrip() + '\n' + '\n'.join(_parts)
                    i = _j - 1

            # 收集紧跟的<table>块和"续表"行（跨页表格支持）
            if current_content:
                j = i + 1
                table_parts = []
                while j < len(lines):
                    jline = lines[j].strip()
                    # 遇到下一个条文号、注释标记、章标题就停
                    if jline and (
                        re.match(r'^(?:#{1,6}\s+)?(?:第\s*)?\d{1,2}\.\d{1,2}(?:\.\d{1,2}){0,3}', jline) or
                        jline.startswith('注：') or jline.startswith('注:') or
                        jline.startswith('## ') or jline.startswith('# ')
                    ):
                        break
                    # 收集<table>块和"续表"行
                    if '<table>' in jline or jline.startswith('续表') or jline.startswith('表 '):
                        table_parts.append(jline)
                    j += 1
                if table_parts:
                    current_content = current_content + '\n' + '\n'.join(table_parts)
                clauses.append((clause_no, current_content))
        
        i += 1
    
    # 附录条目并入（新增：附录A / A.1.1 型条目）
    clauses.extend(extract_appendix_clauses(content))

    # 去重
    seen = set()
    unique = []
    for no, text in clauses:
        if no not in seen:
            seen.add(no)
            unique.append((no, text))
    return unique

def _collect_subitems(lines, start_idx):
    """从start_idx开始收集子条款内容，直到遇到下一个条文号或空行+条文号。
    
    支持续行合并：如果一个子条款被换行拆分（如 "不应小于\n\n0.80m"），
    会自动合并为一个子条款。
    """
    subitems = []
    j = start_idx
    while j < len(lines):
        line = lines[j].strip()
        # 遇到下一个条文号，停止
        if line and _is_clause_number(line):
            break
        # 标准尾部整块（用词说明/引用标准名录/参考文献）→ 停止收集
        if is_doc_tail_head(line):
            break
        # 遇到章节标题（# 开头但不是 ### X.X.X），停止
        if line.startswith('# ') and not line.startswith('##'):
            if not re.match(r'^#{1,3}\s+\d{1,2}\.\d{1,2}', line):
                break
        # 空行：看后面是否紧跟条文号，如果是则停止
        if not line:
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines) and _is_clause_number(lines[k]):
                break
            j += 1
            continue
        # 跳过纯标记行
        if line.startswith('>') or line.startswith('**注') or line.startswith('注：'):
            j += 1
            continue
        # 修: Markdown 标题行不并入正文
        #   老逻辑只拦 h1('# ' 且非 '##')，'## X 章名' 被当续行并进上一条条文（实测
        #   例：…无障碍停车位。## 9 文化、纪念建筑（实测尾部粘标题会成批出现）。
        #   同理 '## / # X.X.X 正文' 这类"标题式条文"本应自己成条，别并进上一条（否则该条文本
        #   被复制到前一条）。
        #   跳过即可：主循环会从这一行接着解析成独立条文。
        if line.startswith('#'):
            j += 1
            continue
        
        # 判断是否为新子条款（以数字+中文开头，数字与中文之间允许有空格/全角空格）
        # 修:部分规范大量使用"1 通气管..."格式(数字+空格+中文),
        # 老正则 r'^\d+[\u4e00-\u9fff]' 漏识别为续行,导致所有"1 2 3"子条款被合并成"1通2在3在"
        is_new_subitem = bool(re.match(r'^\d+[\s\u3000]*[\u4e00-\u9fff#0-9a-zA-Z]', line))
        
        if is_new_subitem or not subitems:
            subitems.append(line)
        else:
            # 续行：合并到前一个子条款
            subitems[-1] += line
        
        j += 1
    return subitems, j - start_idx
