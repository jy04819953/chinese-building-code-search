"""parser · appendix —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from ..consts import APX_DUP_DROPPED, RE_APX_CL, RE_APX_CL_RUN, RE_APX_FIG, RE_APX_GLUED, RE_APX_HEAD, RE_APX_SEC, RE_APX_TBL, _APX_NUM


from .common import is_doc_tail_head
from .notes import is_notes_marker, _notes_start_index


def find_appendix_start(lines):
    """返回"真附录标题"的行号；找不到返回 None。

    ⚠️ 不能见到 `^附录[A-Z]` 就当附录标题——正文里到处是"应符合附录A的规定"这类引用，
    误判会把正文后半截整段截掉（一次能丢上百条）。
    判据（补强）：
      ① 只在本规范**条文说明区之前**找（说明区里也会重复列附录标题）；
      ② 该行须像标题：不含 `。`、不含"的规定/的要求"、长度 ≤ 60；
      ③ 往后 40 行内要有附录子条目(A.1.1)/节标题(A.1)/`表A.x`/`图A.x`/`<table>`/符号行(含 `——`)——
         前四类管一般附录，`图A.x` 管"附录整块是图"，`——` 管"附录A 符号"这类符号表。
    """
    notes_at = _notes_start_index(lines)
    for idx, raw in enumerate(lines):
        if notes_at is not None and idx >= notes_at:
            break
        s = raw.strip()
        if '…' in s or not RE_APX_HEAD.match(s):
            continue
        if '。' in s or '的规定' in s or '的要求' in s or len(s) > 60:
            continue
        for j in range(idx + 1, min(idx + 40, len(lines))):
            sj = lines[j].strip()
            if (RE_APX_CL.match(sj) or RE_APX_SEC.match(sj) or RE_APX_TBL.match(sj)
                    or RE_APX_FIG.match(sj) or '——' in sj or sj.startswith('<table>')):
                return idx
    return None

def extract_appendix_clauses(content):
    """抽附录条目（附录X 章级 + X.Y.Z 条级）。

    OCR 空格变体一并容错：`A. 1.1` / `A.1. 1` / `A . 1.1` 都还原成 `A.1.1`。
    返回 [(clause_no, content), ...]；节级 X.Y 与表格标题不作为独立条目。
    """
    lines = split_glued_apx_numbers(content.split('\n'))
    start = find_appendix_start(lines)
    if start is None:
        return []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if is_notes_marker(lines[j]):
            end = j
            break

    entries, cur = [], None
    for raw in lines[start:end]:
        s = raw.strip()
        # 修: 附录里也有"标题式条目"（'## D.2.2 …'）与纯标题行（'## 本标准用词说明'、
        #   '## D.2受压构件'）。老逻辑只把非标题行并进条目正文 ⇒ 标题行被当成正文黏在附录条目尾部
        #   （实测：附录条目尾部会黏 '## 本标准用词说明' 这类整块结尾，成批出现）。
        #   现改为：先去 '#' 前缀再按条目规则匹配；不是条目/章节的纯标题行一律跳过。
        is_head = s.startswith('#')
        if is_head:
            # 标准尾部整块（用词说明/引用标准名录/参考文献）→ 停止收集
            if is_doc_tail_head(s):
                break
            s = re.sub(r'^#{1,6}\s*', '', s)
            if not (RE_APX_HEAD.match(s) or RE_APX_CL.match(s) or RE_APX_SEC.match(s)):
                continue
        mh = RE_APX_HEAD.match(s)
        if mh:
            cur = {'no': f'附录{mh.group(1)}', 'parts': [mh.group(2).strip()]}
            entries.append(cur)
            continue
        mc = RE_APX_CL.match(s)
        if mc:
            cur = {'no': f'{mc.group(1)}.{mc.group(2)}.{mc.group(3)}',
                   'parts': [(mc.group(4) or '').strip()]}
            entries.append(cur)
            continue
        if RE_APX_SEC.match(s):
            cur = None          # 节级标题：只作分隔，不建条目
            continue
        if cur is not None and s:
            cur['parts'].append(s)

    out = []
    for e in entries:
        body = '\n'.join(p for p in e['parts'] if p).strip()
        if body:
            out.append((e['no'], body))
    # 同号去重：附录区里同号出现两次的两类原因——
    #   ① 条文号被拆行，下半句以同号开头（如 `E.6.1 防火设计时…应按表` + `E.6.1 的规定取值。`）→ 短续行接回上一条；
    #   ② 说明区标记没被识别，说明里的同号条目混进来（DG 系列）→ 长段直接丢弃。
    dedup, seen = [], {}
    for no, body in out:
        if no not in seen:
            seen[no] = len(dedup)
            dedup.append((no, body))
            continue
        idx = seen[no]
        prev = dedup[idx][1]
        # 带表格 / 以"表"开头的重复 = 该条的续表或拆行后的表体 → 必须并回，不能丢
        data = ('<table>' in body) or body.lstrip().startswith('表')
        if body and body not in prev and (data or len(body) < 30):
            dedup[idx] = (no, prev + ('' if len(body) < 30 else '\n') + body)
        else:
            APX_DUP_DROPPED.append((no, body[:40]))  # 说明区混入的长段：丢弃并留痕
    return dedup

def extract_appendix_notes(content):
    """抽「附录的条文说明」：说明区里的 `附录X` 块 + `X.Y.Z` 段落 → [(clause_no, note, clause_no), ...]。

    结构与正文附录一致（附录标题 + 编号段落），所以按同一套编号规则切分；
    没有编号的段落（如"为方便设计，本规范附录A给出了…"）挂到所属 `附录X` 上。
    """
    lines = split_glued_apx_numbers(content.split('\n'))
    start = find_appendix_start(lines)
    if start is None:
        return []
    note_start = None
    for j in range(start, len(lines)):
        if is_notes_marker(lines[j]):
            note_start = j
            break
    if note_start is None:
        return []

    out, buf, cur_nos, cur_title, in_apx = [], [], [], '', False

    def flush():
        if cur_nos and buf:
            t = '\n'.join(x for x in buf if x).strip()
            if len(t) >= 5 and t != cur_title:      # 只有标题、没有说明正文的不算说明
                for cn in cur_nos:                  # 一条说明覆盖多个编号时（"B.0.4、B.0.5…"）各挂一份
                    # 返回值格式与 extract_notes 一致：(条号, 说明, 条号)
                    out.append((cn, t, cn))

    for raw in lines[note_start:]:
        s = raw.strip()
        if '…' in s:              # 说明区的附录目录行，跳过（否则会把目录当说明）
            continue
        if in_apx and (re.match(r'^#{0,4}\s*\d{1,2}\s*[^\d]', s)
                       or re.match(r'^\d{1,2}\.\d{1,2}', s)):
            flush()               # 说明区进入正文章节 → 附录段结束
            buf, cur_nos, in_apx = [], [], False
            continue
        mh = RE_APX_HEAD.match(s)
        if mh:
            flush()
            cur_title = mh.group(2).strip()
            buf, cur_nos, in_apx = [cur_title], [f'附录{mh.group(1)}'], True
            continue
        if not in_apx:
            continue
        mr = RE_APX_CL_RUN.match(s)
        if mr:
            flush()
            buf = [(mr.group(2) or '').strip()]
            cur_title = ''
            cur_nos = _apx_spec_numbers(mr.group(1))
            continue
        if RE_APX_SEC.match(s):
            flush()               # 节标题：只断开当前条，仍在同一附录内
            buf, cur_nos, cur_title = [], [], ''
            continue
        if cur_nos and s:
            buf.append(s)
    flush()
    return out

def _apx_spec_numbers(spec):
    """编号串 → 条号列表（范围符展开成逐个条号）。"""
    clean = lambda t: re.sub(r'\s+', '', t).replace('．', '.')
    ms = list(re.finditer(_APX_NUM, spec))
    nums = []
    for j, m in enumerate(ms):
        cur = clean(m.group())
        if not nums:
            nums.append(cur)
            continue
        gap = clean(spec[ms[j - 1].end():m.start()])
        if gap in ('～', '~', '\\~', '—', '–', '-', '至'):
            pa, pb = nums[-1].split('.'), cur.split('.')
            if len(pa) == 3 and len(pb) == 3 and pa[0] == pb[0] and pa[1] == pb[1] and 0 <= int(pb[2]) - int(pa[2]) <= 30:
                nums.extend(f'{pa[0]}.{pa[1]}.{k}' for k in range(int(pa[2]) + 1, int(pb[2]) + 1))
            else:
                nums.append(cur)          # 跨段范围（如 A.1.2～B.2.3）不逐个展开，只挂终点
        else:
            nums.append(cur)
    return nums

def split_glued_apx_numbers(lines):
    """把"上句末尾粘着下一条编号"的行拆成两行，返回新行列表。"""
    out = []
    for l in lines:
        m = RE_APX_GLUED.search(l)
        while m:
            out.append(l[:m.start() + 1])
            l = l[m.start() + 1:].lstrip()
            m = RE_APX_GLUED.search(l)
        out.append(l)
    return out
