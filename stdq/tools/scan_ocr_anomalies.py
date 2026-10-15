#!/usr/bin/env python3
"""OCR 字符体检（入库硬闸门）——扫中文规范 MD 与库内文本里的假名 + 表头/符号变体。

闸门规则（命中任一「硬失败」= 入库未完成，退出码 1）：
  * 假名（平/片假名）—— 中文规范里 100% 是 OCR 错
  * 表号被拆行（`…应符合表` + `5.4.1的规定。`）—— 解析器会把表号续行当新条文开头，
    紧随的**整张表连同该条正文尾部一起不入库**（实测全库 30 条中招）
  * 尾部整块粘进条文（content 含 用词说明 / 引用标准名录 / 参考文献）—— 该块不属任何条文；
    解析器 起遇到即停，命中即说明库里残留历史脏数据或解析回归
  * LaTeX 乱码宏 A 类（`\mathfrak`/`\sharp`/`\curlywedge`/`\yen`/`\not\equiv`/`\gtrsim`/`\Xi` 等 12 个）
    —— 中文规范里语法上就不该出现，与假名同级（立，对照表见 SKILL §2k-2）
告警（不拦入库）：
  * LaTeX 乱码宏 B 类（`\pmb`/`\mathbb`/`\varrho`/`\varpi`/`\Re`/`\infty`）—— 可能有正常用法，须人判
  * 表头/符号变体（同一格出现多种写法，需人判）
  * 表格数字断裂（1,2,3,␀,5 类，多为一格数字被吐掉）

> 库内扫描覆盖 `clauses.content`、`clauses.note`、`clause_chapters.title` 三个字段
> （note = 条文说明区，补：实测说明区乱码密度不低于正文，此前是盲区）。

用法:
  python3 scan_ocr_anomalies.py GB-55037-2022            # 扫该规范（MD + DB 两字段）
  python3 scan_ocr_anomalies.py "/path/xxx.md"          # 只扫这份 MD（rebuild 前用）
  python3 scan_ocr_anomalies.py --all                   # 全库
  python3 scan_ocr_anomalies.py --all --db-only         # 只扫库
  python3 scan_ocr_anomalies.py GB-50019-2015 --json    # 机器可读

退出码: 0 = 无硬失败（可能含变体/断裂/B 类告警）；1 = 有硬失败（假名 / 表号拆行 / 尾部块粘正文 / LaTeX 乱码宏 A 类）；2 = 用法/环境错误

> 存量口径：全库跑 `--all` 时 A 类命中数是**待办存量**（不是新引入的错，一次实测 1012 处）；
> 入库**一本新规范**时，该本 A 类必须为 0 才算过闸。
"""
import json
import os
import re
import sqlite3
import sys
from ..consts import STANDARDS_DIR

STDQ_DATA = os.environ.get("STDQ_DATA_DIR", STANDARDS_DIR)
DB = os.path.join(STDQ_DATA, "standards.db")

KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
# 已知 OCR 误读 -> 正确字符（仅作提示，需人工核对；勿自动替换）
HINTS = [
    ("エ", "工（如 エ业建筑 → 工业建筑）"),
    ("の", "、或；或——（符号列举 / 句末标点，看上下文）"),
    ("へ", "（（表头左括号）"),
    ("ト", "公式符号（必须核 PDF，勿猜）"),
]
CJK = re.compile(r"[\u4e00-\u9fa5]")
TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
# 表格变体判据: 同格的汉字骨架一致,但有的写法混进了**孤立**字母/~
# （典型：表头括号被读成 C/J/L/~；地名后跟数据数字不算——“错”须两侧都是汉字或括号/或行尾）
# 排除括号包着的单位（如 (m) / (mm)）：那类差异是正常写法，不是 OCR 错
SUSPECT = re.compile(r"(?<=[\u4e00-\u9fa5）)])[A-Za-z~](?=[\u4e00-\u9fa5（）]|$)")


def scan_text(text, label, out, kana_hits):
    for m in KANA.finditer(text):
        s, e = max(0, m.start() - 30), m.end() + 30
        kana_hits.append({"where": label, "char": m.group(0), "ctx": text[s:e].replace("\n", " ")})
    return out


def variant_hits(md_text, label):
    """同一张表里，短单元格（汉字骨架 ≥3）有 ≥2 种写法、且其中一种混了 ASCII → 告警。"""
    groups = {}
    for m in TD.finditer(md_text):
        raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if not (3 <= len(raw) <= 14):
            continue
        skeleton = "".join(CJK.findall(raw))
        if len(skeleton) < 3:
            continue
        groups.setdefault(skeleton, set()).add(raw)
    out = []
    for skeleton, raws in groups.items():
        if len(raws) < 2:
            continue
        # 只认“孤立字母/~”造成的差异，不把数字差异当地名类表格的正常变化
        if not any(SUSPECT.search(r) for r in raws):
            continue
        out.append({"where": label, "skeleton": skeleton, "variants": sorted(raws)[:6]})
    return out


def seq_hits(md_text, label):
    """表格行内“数字序列断裂”告警：1,2,3,[空],5 —— 典型是一个格子的数字被 OCR 吐掉。
    （GB/T 31433-2015 实测：表 11/13/14/19 的分级行丢了好几个数字）"""
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", md_text, re.S):
        cells = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        run = []
        for c in cells + ["x"]:
            v = int(c) if re.fullmatch(r"\d{1,2}", c) else (None if c == "" else -1)
            if v is not None and v >= 0:
                run.append(v)
            else:
                run = []
            if len(run) >= 3:
                known = [x for x in run if x is not None]
                if known == list(range(1, len(known) + 1)) and None in run:
                    out.append({"where": label, "row": " | ".join(x or "␀" for x in cells)[:120]})
                    run = []
    return out


# 表号被拆行: 上一行以「表」结尾 + 下一非空行是该表号的短续行（"5.4.1的规定。"）。
# 后果：解析器把续行当新条文开头 ⇒ 紧随的整张表连同该条正文尾部一起不入库。
TBL_PREV = re.compile(r"表\s*$")
TBL_NEXT = re.compile(r"^\s*#{0,6}\s*(\d{1,2}\.\d{1,2}(?:-\d+)?(?:\.\d{1,2})?)\s*([^\s\d.．].{0,24})$")
TBL_WORD = re.compile(r"的(?:规定|要求|数值)|采用|确定")
# 标准尾部整块（用词说明/引用标准名录/参考文献）不属任何条文 —— 库里 content 仍含这些词
# = 历史脏数据或解析回归（解析器 起遇到这类标题行即停）。
TAIL_GLUE = re.compile(r"用词说明|引用标准名录|参考文献")

# ===== LaTeX 宏层面的「符号失真」扫描（立；对照表见 SKILL §2k-2）=====
# A 类（硬失败）：中文规范里语法上就不该出现的宏 —— 与假名同级，命中即拦闸门。
# B 类（告警不拦）：有可能是正常用法（如积分上限 `^\infty`、`\mathbb{R}`），必须人判。
# 为什么能自动拦：这些宏在中文规范里没有任何正当用途，出现即代表转换把字符/汉字/公式吃成了宏。
# 为什么 B 类只能告警：`\infty`/`\mathbb`/`\pmb` 在数学上合法，无法与误识区分。
LATEX_HARD = [
    ("\\mathfrak", "哥特体：实为 ε/p/n/c 等普通字母被字体错认"),
    ("\\sharp", "音乐符号：实为 ≥/℃/「或」「时」等被吞"),
    ("\\curlywedge", "实为「人」（单位 L/(人·d) 类）"),
    ("\\boxtimes", "乱码段：实为公式内容被整块吞掉"),
    ("\\yen", "金额格式：实为整条公式被误认（如 $$\\yen 123,456,7$$）"),
    ("\\ddag", "双剑号：乱码段"),
    ("\\dag", "剑号：乱码段（含 \\dagger）"),
    ("\\varnothing", "空集符号：实为其它字符"),
    ("\\not\\equiv", "乱码段"),
    ("\\gtrsim", "≳：实为 ≥"),
    ("\\gtrless", "≷：实为 ≥"),
    ("\\Xi", "大写 Ξ：多为汉字/章节标题乱码（如「Ⅳ 施工」）"),
]
LATEX_SOFT = [
    ("\\pmb", "粗体强调：多为乱码符号，也可能正常"),
    ("\\mathbb", "黑板体：实为汉字/单位被误识（\\mathbb{H}/\\mathbb{K}/\\mathbb{W}），也可能正常"),
    ("\\varrho", "ϱ：实为 ρ（同式别处多写作 \\rho → 交叉证据）"),
    ("\\varpi", "ϖ：实为 w / CU 等"),
    ("\\Re", "ℜ：实为 <、「按」等"),
    ("\\infty", "∞：实为 w / f 被误识；但积分上限 ^\\infty 属正常"),
]
_LATEX_HARD_RE = [(re.compile(re.escape(k)), k, w) for k, w in LATEX_HARD]
_LATEX_SOFT_RE = [(re.compile(re.escape(k)), k, w) for k, w in LATEX_SOFT]


def latex_hits(text, label, ctxlen=40):
    """扫 LaTeX 乱码宏 → (hard, soft)。text 可为 MD 全文、或库内 content/note/title。"""
    hard, soft = [], []
    for rx, k, why in _LATEX_HARD_RE:
        for m in rx.finditer(text):
            s, e = max(0, m.start() - ctxlen), m.end() + ctxlen
            hard.append({"where": label, "macro": k, "why": why,
                         "ctx": text[s:e].replace("\n", " ")})
    for rx, k, why in _LATEX_SOFT_RE:
        for m in rx.finditer(text):
            s, e = max(0, m.start() - ctxlen), m.end() + ctxlen
            soft.append({"where": label, "macro": k, "why": why,
                         "ctx": text[s:e].replace("\n", " ")})
    return hard, soft


def _load_stdq():
    """加载同目录下的 stdq 解析器（判定“续行文字/公式块到底有没有进解析结果”）。找不到返回 None。"""
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stdq.py")
    if not os.path.exists(p):
        return None
    spec = importlib.util.spec_from_file_location("stdq_scan", p)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception:
        return None
    return m


def tbl_split_hits(md_text, label):
    """表号被拆行。**只在“续行文字确实没进解析结果”时才报**（避免把解析器已正常处理的行也报出来）：
    上一行以「表」结尾 + 下一非空行是短表号续行 → 用解析器跑一遍，若该续行文字在解析出的任何条文里
    都找不到，则真正丢内容（这类命中必硬失败）。"""
    lines = md_text.split("\n")
    cands = []
    for i, line in enumerate(lines):
        if not TBL_PREV.search(line.rstrip()):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        m = TBL_NEXT.match(lines[j].strip())
        if m and TBL_WORD.search(m.group(2)):
            cands.append((i, j))
    if not cands:
        return []
    stdq = _load_stdq()
    parsed = None
    if stdq is not None:
        try:
            parsed = ["".join(str(c).split()) for _no, c in stdq.extract_clauses(md_text, label)]
        except Exception:
            parsed = None
    out = []
    for i, j in cands:
        cont = "".join(lines[j].strip().lstrip("#").split())
        if parsed is not None and any(cont and cont in p for p in parsed):
            continue          # 续行文字已在解析结果里 ⇒ 解析器处理好了，不报
        out.append({"where": f"{label}:{i + 1}↔{j + 1}",
                    "ctx": f"…{lines[i].strip()[-28:]}  ⟂  {lines[j].strip()[:40]}"})
    return out


def glued_clause_hits(md_text, label):
    """硬失败：正文区里**条号粘在上一句末尾**、解析器没抽成条文（整条内容会因此丢掉或被算进上一条）。

    典型：`…宜设置纵向受拉钢筋。10.3.9厂房柱间支撑的设置和构造，应符合下列要求：`
    解析器只认行首条号（或行中条号后带空格），粘住的这种既不在行首也没空格 ⇒ 整条漏入库
    （全库实测 12 处、10 本，含 GB 50016 §5.1.9、GB/T 50378 §4.2.9；修法=MD 里把
    条号拆到自己一行，见 `fix_glued_clause_lines.py`）。
    判据收紧到"编号前必须是句读标点（。；;，,、：:）)）"，避免表格数字/引用误报。
    """
    stdq = _load_stdq()
    if stdq is None:
        return []
    lines = md_text.split("\n")
    try:
        parsed = set()
        for k, _c in stdq.extract_clauses(md_text, label):
            parsed.add(k)
    except Exception:
        return []
    bs = stdq.find_body_start(lines) if hasattr(stdq, "find_body_start") else 0
    heads = [i for i, l in enumerate(lines) if i >= bs and stdq.is_doc_tail_head(l.strip())]
    notes = [i for i, l in enumerate(lines) if i >= bs and stdq.is_notes_marker(l)]
    end = min(heads + notes) if (heads or notes) else len(lines)
    # 假阳性过滤（实测踩到：附录注里的产品等级「12.5级密封材料」被当条号）：
    #   ① 编号的**上级前缀**必须在本本里真实存在（§10.3.9 → 需有 10.3.x；「12.5级」→ 本本无 12.x ⇒ 排除）
    #   ② 编号后面紧跟的不能是量纲/等级字（级/位/倍/度/年/月/日/个/种/类/层/段/%/℃…）
    prefixes = {k.rsplit(".", 1)[0] for k in parsed if "." in k}
    UNIT = "级位倍度年月日个种类层段条块片根%℃"
    out = []
    for i, l in enumerate(lines):
        if not (bs <= i < end) or re.search(r'colspan|rowspan|<td|</tr>|<table|<img|!\[', l, re.I):
            continue
        for m in re.finditer(r'[。；;]\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*(?=[\u4e00-\u9fa5])', l):
            no = m.group(1)
            if no in parsed:
                continue
            if m.end() < len(l) and l[m.end()] in UNIT:
                continue
            pref = no.rsplit(".", 1)[0]
            if pref not in prefixes and not any(k.startswith(pref + ".") for k in parsed):
                continue
            out.append({"where": f"{label}:{i + 1}", "ctx": f"粘住的条号 §{no} ← …{l[max(0, m.start() - 24):m.end() + 20]}…"})
    return out


def formula_gaps(md_text, label):
    """告警级：正文区里「解析器收不进去」的显示公式块（`$$…$$`）。

    为什么会有：公式块曾有**任何收集路径都没有**的洞（修：条文后紧跟的公式块 +
    「式中：」符号说明现在会被收进该条）。修完仍收不到的，多半是**转换器把块切坏了**
    （典型 `$$式中：$V^{-}$——设计调蓄容积$(m^{3})$$H\cdot$——…`，一行里嵌了公式和符号说明）。
    这一项只告警不拦入库（那类畸形块要人工/重转，拦了会把整本规范卡在闸门外）。
    """
    stdq = _load_stdq()
    if stdq is None:
        return []
    lines = md_text.split("\n")
    try:
        parsed = ["".join(str(c).split()) for _n, c in stdq.extract_clauses(md_text, label)]
    except Exception:
        return []
    allp = "\n".join(parsed)
    bs = stdq.find_body_start(lines) if hasattr(stdq, "find_body_start") else 0
    heads = [i for i, l in enumerate(lines) if i >= bs and stdq.is_doc_tail_head(l.strip())]
    notes = [i for i, l in enumerate(lines) if i >= bs and stdq.is_notes_marker(l)]
    end = min(heads + notes) if (heads or notes) else len(lines)
    out = []
    for i, l in enumerate(lines):
        if not (bs <= i < end):
            continue
        s = l.strip()
        if not s.startswith("$$"):
            continue
        if s.find("$$", 2) != -1:
            body = "".join(s.split())
        else:
            k = i + 1
            while k < len(lines) and k - i <= 40 and "$$" not in lines[k]:
                k += 1
            body = "".join("".join(lines[i:k + 1]).split())
        if 12 <= len(body) <= 400 and body not in allp:
            out.append({"where": f"{label}:{i + 1}", "ctx": body[:70]})
    return out


def scan_md(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    kana, var = [], []
    for i, line in enumerate(text.split("\n"), 1):
        if KANA.search(line):
            for m in KANA.finditer(line):
                s, e = max(0, m.start() - 30), m.end() + 30
                kana.append({"where": f"{os.path.basename(path)}:{i}", "char": m.group(0),
                             "ctx": line[s:e].strip()})
    var = variant_hits(text, os.path.basename(path))
    seq = seq_hits(text, os.path.basename(path))
    tsplit = tbl_split_hits(text, os.path.basename(path))
    fgap = formula_gaps(text, os.path.basename(path))
    glued = glued_clause_hits(text, os.path.basename(path))
    lh, ls = latex_hits(text, os.path.basename(path))
    return kana, var, seq, tsplit, fgap, glued, lh, ls


def db_rows(codes=None):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    where, params = "", []
    if codes:
        where = " WHERE std_code IN (%s)" % ",".join("?" * len(codes))
        params = list(codes)
    clauses = cur.execute(f"SELECT std_code, clause_no, content, note FROM clauses{where}", params).fetchall()
    # 注意：clause_chapters 的规范列名也是 std_code（不是 code），两表同一列名
    chapters = cur.execute(f"SELECT std_code, clause_no, title FROM clause_chapters{where}", params).fetchall()
    conn.close()
    return clauses, chapters


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    as_json = "--json" in flags
    if not args and "--all" not in flags:
        print(__doc__)
        return 2
    if not os.path.exists(DB):
        print(f"❌ 找不到库: {DB}（可用 STDQ_DATA_DIR 覆盖）")
        return 2

    kana, var, seq, tsplit, fgap, glue, glued = [], [], [], [], [], [], []
    latex_hard, latex_soft = [], []
    scan_md_ok, scan_db_ok = "--db-only" not in flags, "--md-only" not in flags

    # 载入 standards 映射（code -> md_file）
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    std = dict(conn.execute("SELECT code, md_file FROM standards").fetchall())
    conn.close()

    targets_md, targets_db = [], []
    if "--all" in flags:
        targets_md = list(std.values())
        targets_db = None
    else:
        for a in args:
            if os.path.exists(a):
                targets_md.append(a)
            else:
                hit = [c for c in std if c.upper() == a.upper()] or [c for c in std if a.upper() in c.upper()]
                if not hit:
                    print(f"⚠️ 既不是文件路径、也不匹配任何规范号: {a}")
                    continue
                targets_db += hit
                targets_md += [std[c] for c in hit]

    if scan_md_ok:
        for p in targets_md:
            if p and os.path.exists(p):
                k, v, q, t, f, g, lh, ls = scan_md(p)
                kana += k
                var += v
                seq += q
                tsplit += t
                fgap += f
                glued += g
                latex_hard += lh
                latex_soft += ls
    if scan_db_ok:
        codes = None if targets_db is None else targets_db
        if targets_db is None:
            clauses, chapters = db_rows(None)
            # 全库扫描只看有没有假名，变体告警只对指定规范做（全库变体量大且噪声多）
        else:
            clauses, chapters = db_rows(codes)
        for sc, cn, content, note in clauses:
            for m in KANA.finditer(content):
                s, e = max(0, m.start() - 30), m.end() + 30
                kana.append({"where": f"DB clauses {sc} §{cn}", "char": m.group(0),
                             "ctx": content[s:e].replace("\n", " ")})
            lh, ls = latex_hits(content, f"DB clauses {sc} §{cn}")
            latex_hard += lh
            latex_soft += ls
            if note:
                for m in KANA.finditer(note):
                    s, e = max(0, m.start() - 30), m.end() + 30
                    kana.append({"where": f"DB note {sc} §{cn}", "char": m.group(0),
                                 "ctx": note[s:e].replace("\n", " ")})
                lh, ls = latex_hits(note, f"DB note {sc} §{cn}")
                latex_hard += lh
                latex_soft += ls
            gm = TAIL_GLUE.search(content)
            if gm:
                gs, ge = max(0, gm.start() - 25), gm.end() + 40
                glue.append({"where": f"DB clauses {sc} §{cn}", "word": gm.group(0),
                             "ctx": content[gs:ge].replace("\n", "⏎")})
        for sc, cn, title in chapters:
            if KANA.search(title):
                kana.append({"where": f"DB clause_chapters {sc} §{cn}", "char": KANA.search(title).group(0),
                             "ctx": title})
            lh, ls = latex_hits(title, f"DB clause_chapters {sc} §{cn}")
            latex_hard += lh
            latex_soft += ls

    if as_json:
        print(json.dumps({"kana": kana, "variants": var, "seq_gaps": seq,
                          "table_split": tsplit, "glued_clause": glued,
                          "tail_glue": glue, "formula_gaps": fgap,
                          "latex_hard": latex_hard, "latex_soft": latex_soft}, ensure_ascii=False, indent=1))
    else:
        print("=" * 62)
        print(f"OCR 字符体检：假名 {len(kana)} 处（硬）；表号拆行 {len(tsplit)} 处（硬）；"
              f"条号粘在行尾 {len(glued)} 处（硬）；尾部块粘正文 {len(glue)} 条（硬）；"
              f"LaTeX 乱码宏 A 类 {len(latex_hard)} 处（硬）；"
              f"公式未入库告警 {len(fgap)} 处；"
              f"LaTeX 宏 B 类告警 {len(latex_soft)} 处；"
              f"表头/符号变体告警 {len(var)} 组；表格数字断裂告警 {len(seq)} 行")
        print("=" * 62)
        if kana:
            print("\n🔴 假名（硬闸门未过，必须修掉才能算入库完成）:")
            for h in kana:
                hint = next((f"  ← 疑似应为：{v}" for k, v in HINTS if k == h["char"]), "")
                print(f"  {h['where']}  字符「{h['char']}」{hint}")
                print(f"      …{h['ctx']}…")
        else:
            print("\n✅ 无假名")
        if tsplit:
            print(f"\n🔴 表号被拆行（硬闸门未过：紧随其后的整张表会被吞）共 {len(tsplit)} 处:")
            for t in tsplit:
                print(f"  {t['where']}  {t['ctx']}")
        if glue:
            print(f"\n🔴 尾部整块粘进条文（硬闸门未过：用词说明/引用标准名录/参考文献不属任何条文）共 {len(glue)} 条:")
            for g in glue[:30]:
                print(f"  {g['where']}  命中「{g['word']}」  …{g['ctx']}…")
        if glued:
            print(f"\n🔴 条号粘在上一句末尾（硬闸门未过：整条会漏入库/被算进上一条）共 {len(glued)} 处，最多列 20 处:")
            for g in glued[:20]:
                print(f"  {g['where']}  {g['ctx']}")
        if latex_hard:
            print(f"\n🔴 LaTeX 乱码宏 A 类（硬闸门未过：中文规范里语法上不该出现，对照表见 SKILL §2k-2）"
                  f"共 {len(latex_hard)} 处，最多列 30 处:")
            for h in latex_hard[:30]:
                print(f"  {h['where']}  {h['macro']}  ← {h['why']}")
                print(f"      …{h['ctx']}…")
        if fgap:
            print(f"\n⚠️ 公式未入库告警（正文区里解析器收不进去的显示公式块，多为转换器把块切坏）"
                  f"共 {len(fgap)} 处，最多列 15 处:")
            for g in fgap[:15]:
                print(f"  {g['where']}  {g['ctx']}")
        if latex_soft:
            print(f"\n⚠️ LaTeX 宏 B 类告警（可能有正常用法，人判；对照表见 SKILL §2k-2）"
                  f"共 {len(latex_soft)} 处，最多列 15 处:")
            for h in latex_soft[:15]:
                print(f"  {h['where']}  {h['macro']}  ← {h['why']}")
                print(f"      …{h['ctx']}…")
        if var:
            print(f"\n⚠️ 表头/符号变体告警（同一格多种写法，人判后按多数写法归一）共 {len(var)} 组，最多列 30 组:")
            for v in var[:30]:
                print(f"  {v['where']}  汉字骨架「{v['skeleton']}」→ {v['variants']}")
        if seq:
            print(f"\n⚠️ 表格数字断裂告警（1,2,3,␀,5 类，多为一格数字被吐掉）共 {len(seq)} 行，最多列 20 行:")
            for q in seq[:20]:
                print(f"  {q['where']}  {q['row']}")
        print("\n修法：假名/变体见 §2k；LaTeX 乱码宏 A/B 类 → 走 §2q 批量 SOP（子代理产清单 → 回 PDF 逐字核 → apply_symbol_fixes.py）；"
              "表号拆行 → 同目录 fix_table_swallow.py；"
              "条号粘在行尾 → 同目录 fix_glued_clause_lines.py（把条号拆到自己一行）；"
              "尾部块 → trim_doc_tail.py。公式符号必须核 PDF，禁猜。")
    return 1 if (kana or tsplit or glue or glued or latex_hard) else 0


if __name__ == "__main__":
    sys.exit(main())
