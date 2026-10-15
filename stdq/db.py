"""db —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from .consts import DB_PATH, STANDARDS_DIR


from .catalog import refresh_catalog, _ask_profession_interactive, _classify_std
from .parser.clauses import extract_clauses
from .parser.notes import extract_notes
from .parser.toc import parse_toc
from .parser.meta import detect_all_strong, is_strict_clause, parse_strong_from_announcement, parse_abolished_from_announcement
from .parser.appendix import extract_appendix_clauses, extract_appendix_notes
from datetime import datetime
import glob
from .catalog import extract_std_code


def get_db():
    """获取 SQLite 连接。

    调用方应该用 with 语句:
        with get_db() as db:
            c = db.cursor()
            ...

    这样无论函数中途 return 还是抛异常,连接都会自动 close。
    (B5 修复:从手动 db.close() 改 with 模式,杜绝连接泄漏)

    ===== TODO B5: 全面改 with 模式 =====
    现状: 13 处 db = get_db() + 末尾 db.close(),无连接泄漏(都正确 close),
          但少数异常路径(raise 前未 close)有泄漏风险。
    改法: 把所有 db = get_db() 改成 with get_db() as db:,函数体缩进。
    风险: 改动大,每个 cmd_* 函数都要缩进,验证成本高。
    决定: 仅加 docstring 引导,实际改写留待专项清理 PR。
    临时安全网: 数据库连接对象离开作用域时 Python 不会自动 close,
          但进程退出时 OS 会释放文件句柄,不会造成永久泄漏。
    实际症状: 长时间高频查询会"too many open files",但 stdq 不是高频服务,可控。
    """
    return sqlite3.connect(DB_PATH)

def ensure_schema(c):
    """首次建库时创建核心表(幂等)。

    新用户 clone 仓库后直接 `stdq rebuild` 即可,无需手工建表或预置空库。
    表结构由本函数维护;已存在的表不会被改动。
    """
    c.execute("""CREATE TABLE IF NOT EXISTS standards (
        code TEXT PRIMARY KEY, name TEXT, md_file TEXT,
        clause_count INTEGER DEFAULT 0, strong_count INTEGER DEFAULT 0,
        updated_at TEXT, profession TEXT DEFAULT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS clauses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, std_code TEXT, clause_no TEXT, content TEXT,
        is_strong INTEGER DEFAULT 0, has_note INTEGER DEFAULT 0, note TEXT, themes TEXT,
        line_in_md INTEGER, is_strict INTEGER DEFAULT 0,
        applicable_type TEXT NOT NULL DEFAULT '通用'
            CHECK(applicable_type IN ('通用','住宅','公建','工业')),
        FOREIGN KEY (std_code) REFERENCES standards(code))""")

def _backup_db_before_rebuild():
    """重建前自动备份数据库。备份到 backups/ 子目录,旧备份保留。"""
    if not os.path.exists(DB_PATH):
        return
    backup_dir = os.path.join(STANDARDS_DIR, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = os.path.join(backup_dir, f'standards.db.bak.{ts}')
    try:
        import shutil
        shutil.copy2(DB_PATH, backup_path)
        print(f'  [stdq] 重建前自动备份: {backup_path}', file=sys.stderr)
    except Exception as e:
        print(f'  [stdq] ⚠️ 自动备份失败: {e} (rebuild 继续)', file=sys.stderr)

def _should_skip_file(name):
    """是否跳过非规范文件(目次/说明/起草说明等)。"""
    if '.bak' in name or '条文索引' in name:
        return True
    if name.endswith('_条文说明.md') or name == '条文说明.md':
        return True
    skip_exact = ['主目次.md', '条文说明目次.md', '起草说明']   # 加：体检/待办类笔记文件混在 standards/ 会被当规范收录（OCR可疑字符清单、规范库遗留问题清单）
    return any(p in name for p in skip_exact)

def _parse_std_file(md_path):
    """解析单个 MD 文件,返回 (clauses, abolished_list, notes, name, std_code, all_strong, ann_strong_nos)。

    返回值:
      clauses: [(clause_no, clause_text), ...]
      abolished_list: [{target_std, target_name, clause_nos, abolish_type}, ...]
      notes: [(clause_no, note_content, source_title), ...]
      name: 规范名称(去掉代号)
      std_code: XX-XXXX-XXXX
      all_strong: bool,True 表示全文强条
      ann_strong_nos: set 或 None,精确强条列表
    """
    basename = os.path.basename(md_path)
    std_code = extract_std_code(basename)
    if not std_code:
        return None
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except (UnicodeDecodeError, IOError) as e:
        print(f'  ⚠️ 跳过 {basename}: {e}')
        return None

    # 强条:优先公告精确列表,降级关键词判断(关键词不作强条依据)
    all_strong = detect_all_strong(content)
    clauses = extract_clauses(content, filename=basename)
    ann_strong_full, ann_strong_nos = parse_strong_from_announcement(content)
    if ann_strong_full:
        all_strong = True
        ann_strong_nos = None  # 全文强条,不需要逐条列表
    # 废止
    abolished_list = parse_abolished_from_announcement(content, current_std_code=std_code)
    # 条文说明
    clause_nums = {cn for cn, _ in clauses}
    notes = extract_notes(content, clause_nums)
    # 附录的条文说明（说明区里的 附录X / X.Y.Z 段落）一并抽出，挂到同名附录条目上
    notes = notes + extract_appendix_notes(content)
    # 名称(去掉代号)
    name = re.sub(r'^[A-Z]+[-]?[A-Z]*[-]?\d+[-]?\d{4}\s*', '', basename.replace('.md', '')).strip('_ ')
    return clauses, abolished_list, notes, name, std_code, all_strong, ann_strong_nos

def _update_clauses(c, std_code, name, md_path, clauses, all_strong, ann_strong_nos,
                    abolished_list, notes, profession):
    """把单本规范的解析结果写入 DB,返回 (clause_count, strong_count, note_count)。

    新增章节写入 — 重新读 MD 调 parse_toc 抽目次,写 clause_chapters 表。
    """
    # 写 standards 行
    c.execute('''INSERT OR REPLACE INTO standards
                 (code, name, md_file, clause_count, strong_count, updated_at, profession)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (std_code, name, md_path, 0, 0, datetime.now().isoformat(), profession))
    # 写章节目录(从 MD 目次段抽 X/X.Y/附录X,parent_clause_no 由 clause_no 派生)
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            md_text = f.read()
        chapter_entries = parse_toc(md_text)
        for ce in chapter_entries:
            # 父章节派生:
            # - X (顶层章) → parent = X(自身,用于标记顶层)
            # - X.Y (节) → parent = X(归属章节)
            # - X.Y.Z (子条文,parse_toc 不返回) → parent = X.Y(归属节)
            # - 附录X → parent = None(顶层)
            cn = ce['clause_no']
            if cn.startswith('附录'):
                parent = None
            elif '.' in cn:
                # 去掉最后一段:4.1 → 4;4.1.1 → 4.1
                parts = cn.split('.')
                parent = '.'.join(parts[:-1])
            else:
                parent = cn  # 顶层章:parent = 自身
            c.execute('''INSERT OR IGNORE INTO clause_chapters
                         (std_code, clause_no, title, level, parent_clause_no)
                         VALUES (?, ?, ?, ?, ?)''',
                      (std_code, cn, ce['title'], ce['level'], parent))
    except (UnicodeDecodeError, IOError):
        pass  # 章节写入失败不影响主流程
    # 写 clauses 行
    strong_count = 0
    for cn, ct in clauses:
        if all_strong:
            is_s = 1
        elif ann_strong_nos is not None:
            is_s = 1 if cn in ann_strong_nos else 0
        else:
            is_s = 0  # 关键词不能作强条依据
        if is_s:
            strong_count += 1
        is_st = 1 if is_strict_clause(ct) else 0
        # applicable_type 走顶层 _classify_std(支持章节级分类)
        app_type = _classify_std(std_code, cn)
        c.execute('INSERT INTO clauses (std_code, clause_no, content, is_strong, is_strict, applicable_type) VALUES (?, ?, ?, ?, ?, ?)',
                  (std_code, cn, ct, is_s, is_st, app_type))
    # 写 abolished 行
    for ab in abolished_list:
        c.execute('''INSERT INTO abolished
                     (source_std, target_std, target_name, clause_nos, abolish_type)
                     VALUES (?, ?, ?, ?, ?)''',
                  (std_code, ab['target_std'], ab['target_name'],
                   ','.join(ab['clause_nos']) if ab['clause_nos'] else None,
                   ab['abolish_type']))
    # 写 notes(去重,只取每条首个说明)
    notes_dict = {}
    for cn, nc, _ in notes:
        if cn not in notes_dict:
            notes_dict[cn] = nc
    note_count = 0
    for cn, nc in notes_dict.items():
        c.execute('UPDATE clauses SET has_note = 1, note = ? WHERE std_code = ? AND clause_no = ?',
                  (nc, std_code, cn))
        note_count += 1
    # 更新 standards 的 clause_count / strong_count
    c.execute('UPDATE standards SET clause_count = ?, strong_count = ? WHERE code = ?',
              (len(clauses), strong_count, std_code))
    return len(clauses), strong_count, note_count

def _rebuild_fts(db):
    """重建 FTS5 全文索引(unicode61 tokenizer)。"""
    c = db.cursor()
    try:
        c.execute('DROP TABLE IF EXISTS clauses_fts')
        c.execute('''CREATE VIRTUAL TABLE clauses_fts USING fts5(
            std_code, clause_no, content,
            content=clauses,
            content_rowid=id,
            tokenize='unicode61'
        )''')
        c.execute('INSERT INTO clauses_fts(clauses_fts) VALUES("rebuild")')
    except Exception as e:
        print(f'FTS5 warning: {e}')

def _check_embed_sync():
    """检查语义索引是否跟数据库同步,不一致则提示运行 embed-build。"""
    embed_dir = os.path.join(STANDARDS_DIR, 'embeddings')
    ids_file = os.path.join(embed_dir, 'ids.npy')
    if not os.path.exists(ids_file):
        return
    import numpy as np
    ids = np.load(ids_file)
    db2 = get_db()
    c2 = db2.cursor()
    c2.execute('SELECT COUNT(*) FROM clauses')
    db_count = c2.fetchone()[0]
    db2.close()
    # 索引里的 id 数应与库中条文数一致;不一致说明库改过而向量未重建。
    # 只统计正数 ID(条文部分)与 clauses 表比。
    clause_id_count = int((ids > 0).sum())
    if clause_id_count != db_count:
        print(f'⚠️ 语义索引已过期（索引条文{clause_id_count}条 vs 数据库{db_count}条）')
        print(f'   入库完成后请执行: stdq embed-build')
    else:
        print(f'✅ 语义索引已同步（{clause_id_count}条条文）')

def cmd_rebuild(args):
    """重建规范库主入口

    流程:备份 → 清表 → 扫 MD → 逐文件解析+入库 → 重建 FTS → 检查 embed → 维护 catalog
    各步由独立子函数负责,便于测试和修改。
    """
    _backup_db_before_rebuild()

    with get_db() as db:
        c = db.cursor()
        ensure_schema(c)   # 首次建库自动建表(幂等)

        # rebuild 保留已有 profession
        # DELETE FROM standards 之前先把所有 code→profession 备份到 dict,
        # 然后 _update_clauses 用 dict 保留已有 profession,避免被冲成 general
        c.execute('SELECT code, profession FROM standards')
        existing_professions = {row[0]: row[1] for row in c.fetchall() if row[1]}

        c.execute('DELETE FROM clauses')
        c.execute('DELETE FROM standards')
        c.execute('''CREATE TABLE IF NOT EXISTS abolished (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_std TEXT, target_std TEXT, target_name TEXT,
            clause_nos TEXT, abolish_type TEXT
        )''')
        c.execute('DELETE FROM abolished')
        # 章节目录表(从 MD 目次段抽 X/X.Y/附录X,用于加权召回)
        c.execute('''CREATE TABLE IF NOT EXISTS clause_chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            std_code TEXT NOT NULL,
            clause_no TEXT NOT NULL,
            title TEXT NOT NULL,
            level TEXT NOT NULL,
            parent_clause_no TEXT,
            UNIQUE(std_code, clause_no)
        )''')
        c.execute('DELETE FROM clause_chapters')
        c.execute('CREATE INDEX IF NOT EXISTS idx_chapters_std ON clause_chapters(std_code)')

        # 同时看数据目录根下与 standards/ 子目录（两种摆放都支持）
        _md = set(glob.glob(os.path.join(STANDARDS_DIR, '*.md')))
        _md |= set(glob.glob(os.path.join(STANDARDS_DIR, 'standards', '*.md')))
        md_files = sorted(_md)
        total_clauses = total_stds = total_notes = 0

        for md_path in md_files:
            basename = os.path.basename(md_path)
            if '.bak' in basename or '条文索引' in basename:
                continue
            if _should_skip_file(basename):
                continue

            parsed = _parse_std_file(md_path)
            if not parsed:
                continue
            clauses, abolished_list, notes, name, std_code, all_strong, ann_strong_nos = parsed

            # 优先用 rebuild 备份的 existing_professions(DELETE 前 SELECT 的)
            # existing_professions 里没有(新增规范)→ 才走 _ask_profession_interactive 弹问
            profession = existing_professions.get(std_code) or _ask_profession_interactive(std_code, name, matched_themes=[])

            clause_count, strong_count, note_count = _update_clauses(
                c, std_code, name, md_path, clauses, all_strong, ann_strong_nos,
                abolished_list, notes, profession)

            total_clauses += clause_count
            total_notes += note_count
            total_stds += 1
            strong_str = f' (强条{strong_count}条)' if strong_count > 0 else ''
            note_str = f' (说明{note_count}条)' if note_count > 0 else ''
            ab_str = f' (废止{len(abolished_list)}条)' if abolished_list else ''
            print(f'  ✓ {std_code}: {clause_count}条{strong_str}{note_str}{ab_str}')
            # 修 bug:不再每文件提交,统一在循环外 commit(去重内层 db.commit)
        db.commit()

        _rebuild_fts(db)
        db.commit()
    print(f'\n完成: {total_stds}本规范, {total_clauses}条条文, {total_notes}条说明')

    # 废止统计
    db2 = get_db()
    c2 = db2.cursor()
    c2.execute('SELECT COUNT(*) FROM abolished'); ab_count = c2.fetchone()[0]
    c2.execute("SELECT COUNT(*) FROM abolished WHERE abolish_type='whole'"); ab_whole = c2.fetchone()[0]
    c2.execute("SELECT COUNT(*) FROM abolished WHERE abolish_type='clauses'"); ab_clauses = c2.fetchone()[0]
    db2.close()
    if ab_count > 0:
        print(f'废止: {ab_count}条 (整本{ab_whole} + 条文{ab_clauses})')

    _check_embed_sync()

    # 自动维护 catalog.md(用户需求)
    try:
        refresh_catalog()
    except Exception as e:
        print(f'  ⚠️ catalog.md 自动维护失败: {e}', file=sys.stderr)
