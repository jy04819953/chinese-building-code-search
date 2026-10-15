"""cli —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from .consts import STANDARDS_DIR, THEME_NAMES


from .db import get_db, cmd_rebuild
from .search import search_auto
from .catalog import _normalize_std_code
from .parser.tables import _format_clause_content


def cmd_compare(args):
    import jieba
    results = {}
    for std in args.standards:
        std = _normalize_std_code(std)  # GB55038-2025 → GB-55038-2025
        if getattr(args, 'semantic', False):
            # 语义搜索：在指定规范内找最相关的条文
            sem_results, _ = search_auto(args.query, limit=10, std=std)
            results[std] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in sem_results]
        else:
            # 改 with:db 在 branch 结束时自动 close,无需手动 db.close()
            with get_db() as db:
                c = db.cursor()
                # 策略1：精确短语匹配
                c.execute('''SELECT std_code, clause_no, content, has_note, note FROM clauses
                             WHERE std_code LIKE ? AND (content LIKE ? OR note LIKE ?) LIMIT 10''',
                          (f'%{std}%', f'%{args.query}%', f'%{args.query}%'))
                results[std] = c.fetchall()

                # 策略2：jieba分词后AND匹配
                if not results[std]:
                    words = [w.strip() for w in jieba.cut(args.query) if w.strip() and len(w.strip()) > 1]
                    if words:
                        conditions = ' AND '.join(['(content LIKE ? OR note LIKE ?)'] * len(words))
                        params = []
                        for w in words:
                            params.extend([f'%{w}%', f'%{w}%'])
                        c.execute(f'''SELECT std_code, clause_no, content, has_note, note FROM clauses
                                      WHERE std_code LIKE ? AND {conditions} LIMIT 10''',
                                  (f'%{std}%', *params))
                        results[std] = c.fetchall()

                # LIKE无结果时降级到语义搜索(已在 with 外调用,无需 db)
                if not results[std]:
                    sem_results, _ = search_auto(args.query, limit=10, std=std)
                    results[std] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in sem_results]
    return results

def cmd_strong(args):
    with get_db() as db:
        c = db.cursor()
        std = _normalize_std_code(args.std)
        c.execute('''SELECT std_code, clause_no, content FROM clauses
                     WHERE std_code LIKE ? AND is_strong = 1''',
                  (f'%{std}%',))
        results = c.fetchall()
    return results

def cmd_abolished(args):
    """查询废止条文信息。支持两个方向查询。"""
    with get_db() as db:
        c = db.cursor()
        std = _normalize_std_code(args.std)
    
        # 查询该规范废止了哪些（source_std 方向）
        c.execute('''SELECT source_std, target_std, target_name, clause_nos, abolish_type
                     FROM abolished WHERE source_std LIKE ?''',
                  (f'%{std}%',))
        results = c.fetchall()
    
        # 也查询哪些规范废止了该规范的条文（target_std 方向）
        c.execute('''SELECT source_std, target_std, target_name, clause_nos, abolish_type
                     FROM abolished WHERE target_std LIKE ?''',
                  (f'%{std}%',))
        results += c.fetchall()
    
    return results

def cmd_clause(args):
    """取单条原文（连带该条的说明）。"""
    with get_db() as db:
        c = db.cursor()
        std = _normalize_std_code(args.std)
        c.execute('''SELECT std_code, clause_no, content, has_note, note FROM clauses
                     WHERE std_code LIKE ? AND clause_no = ?''',
                  (f'%{std}%', args.clause))
        return c.fetchall()


def cmd_catalog(args):
    """重新生成在库清单（catalog.md）。"""
    from .catalog import refresh_catalog
    refresh_catalog()
    print('✅ 已重写在库清单')
    return 0


def cmd_term(args):
    """术语查询：查某本规范的术语条目（术语标准或规范里的术语章）。"""
    with get_db() as db:
        c = db.cursor()
        std = _normalize_std_code(args.std)
        sql = 'SELECT clause_no, content FROM clauses WHERE std_code LIKE ?'
        params = [f'%{std}%']
        if getattr(args, 'keyword', None):
            sql += ' AND content LIKE ?'
            params.append(f'%{args.keyword}%')
        sql += ' ORDER BY clause_no LIMIT 60'
        c.execute(sql, params)
        return c.fetchall()


def cmd_appendix(args):
    """查看某本规范的附录条目（附录X / A.1 这类编号）。"""
    with get_db() as db:
        c = db.cursor()
        std = _normalize_std_code(args.std)
        c.execute('''SELECT clause_no, content FROM clauses
                     WHERE std_code LIKE ? AND (clause_no LIKE '附录%' OR clause_no GLOB '[A-Z]*')
                     ORDER BY clause_no LIMIT 80''', (f'%{std}%',))
        return c.fetchall()

def cmd_info(args):
    with get_db() as db:
        c = db.cursor()
        std = _normalize_std_code(args.std)
        c.execute('''SELECT code, name, md_file, clause_count, strong_count
                     FROM standards WHERE code LIKE ?''',
                  (f'%{std}%',))
        info = c.fetchone()
    if info:
        print(f'《{info[1]}》{info[0]}')
        print(f'  条文: {info[3]}条 | 强条: {info[4]}条')
        print(f'  文件: {info[2]}')
    else:
        print(f'❌ 未找到规范: {args.std}')
    return info

def cmd_list(args):
    with get_db() as db:
        c = db.cursor()
        c.execute('SELECT code, name, clause_count, strong_count FROM standards ORDER BY code')
        results = c.fetchall()
    for r in results:
        print(f'  ✓ {r[0]}: {r[1]} ({r[2]}条, 强条{r[3]}条)')
    print(f'\n完成: {len(results)}本规范')
    return results

def cmd_embed_build(args):
    embed_dir = os.path.join(STANDARDS_DIR, 'embeddings')
    os.makedirs(embed_dir, exist_ok=True)

    # 改 with:用 stdq 封装的 contextmanager,确保连接 close
    with get_db() as db:
        c = db.cursor()
        c.execute('SELECT id, std_code, clause_no, content, note FROM clauses')
        rows = c.fetchall()

    print(f'从数据库加载 {len(rows)} 条条文')
    
    try:
        from sentence_transformers import SentenceTransformer
        try:
            import numpy as np
        except ImportError:  # 语义检索是可选功能；未安装时关键词检索照常工作
            np = None
        
        print('加载embedding模型...')
        model = SentenceTransformer('BAAI/bge-base-zh-v1.5', cache_folder=os.path.expanduser('~/.cache/huggingface'))
        
        all_rows = rows
        texts = [f"{r[1]} §{r[2]} {r[3]} {r[4] or ''}" for r in all_rows]
        ids = [r[0] for r in all_rows]
        
        print(f'编码 {len(texts)} 条条文...')
        embeddings = model.encode(texts, batch_size=64, show_progress_bar=True)
        
        np.save(os.path.join(embed_dir, 'ids.npy'), np.array(ids))
        np.save(os.path.join(embed_dir, 'embeddings.npy'), embeddings)
        
        print(f'索引已保存: {embed_dir}')
        print(f'完成! 索引 {len(ids)} 条（条文 {len(rows)}）')
    except Exception as e:
        print(f'Error: {e}')

def cmd_embed_info(args):
    embed_dir = os.path.join(STANDARDS_DIR, 'embeddings')
    if os.path.exists(embed_dir):
        files = os.listdir(embed_dir)
        print(f'向量索引目录: {embed_dir}')
        for f in files:
            size = os.path.getsize(os.path.join(embed_dir, f))
            print(f'  {f}: {size/1024/1024:.1f}MB')
    else:
        print('向量索引不存在')

def main():
    parser = argparse.ArgumentParser(description='规范全文检索工具')
    sub = parser.add_subparsers(dest='command')
    
    p_search = sub.add_parser('search')
    p_search.add_argument('query')
    p_search.add_argument('--std')
    p_search.add_argument('--limit', type=int, default=30)  # 默认 30(与 extras 80/20 联动)
    p_search.add_argument('--semantic', action='store_true')
    p_search.add_argument('--no-daemon', action='store_true', help='跳过守护进程，直接加载模型')
    p_search.add_argument('--type', choices=['住宅','公建','工业','通用'], help='按建筑类型过滤（仅返回适用于该类型的条文);"通用"等价于不传(=不限类型)')
    # 召回范围开关
    p_search.add_argument('--standard', action='store_true', help='只召规范条文(标准库)')
    p_search.add_argument('--table-style', choices=['markdown','compact','raw'], default='markdown',
                          help='含表格的条文输出风格（默认 markdown，适合能渲染 Markdown 的客户端；compact 适合纯文本终端长表折叠）')

    p_compare = sub.add_parser('compare')
    p_compare.add_argument('query')
    p_compare.add_argument('standards', nargs='+')
    p_compare.add_argument('--semantic', action='store_true')
    p_compare.add_argument('--table-style', choices=['markdown','compact','raw'], default='markdown',
                          help='含表格的条文输出风格')

    p_strong = sub.add_parser('strong')
    p_strong.add_argument('std')
    p_strong.add_argument('--table-style', choices=['markdown','compact','raw'], default='markdown',
                          help='含表格的条文输出风格')
    
    p_abolished = sub.add_parser('abolished')
    p_abolished.add_argument('std')
    
    p_clause = sub.add_parser('clause')
    p_clause.add_argument('--no-note', action='store_true', dest='no_note', help='不显示该条的条文说明')
    p_clause.add_argument('std')
    p_clause.add_argument('clause')
    
    p_info = sub.add_parser('info')
    p_info.add_argument('std')
    
    p_rebuild = sub.add_parser('rebuild')
    p_rebuild.add_argument('--std')

    # 新增: profession 主题管理子命令
    p_profession = sub.add_parser('profession', help='查看/设置规范的 profession 主题')
    p_prof_sub = p_profession.add_subparsers(dest='prof_command')
    p_prof_list = p_prof_sub.add_parser('list', help='列出所有规范的 profession 分布')
    p_prof_set = p_prof_sub.add_parser('set', help='设置某本规范的 profession')
    p_prof_set.add_argument('std_code', help='规范代号(可带或不带末尾年号)')
    p_prof_set.add_argument('theme', help='主题代码(见 THEME_NAMES)')

    p_daemon = sub.add_parser('daemon', help='常驻进程：让模型留在内存里，查询变快（推荐）')
    p_daemon_sub = p_daemon.add_subparsers(dest='daemon_command')
    p_daemon_sub.add_parser('start', help='启动（前台；加 -d 后台）').add_argument('-d', '--daemonize', action='store_true')
    p_daemon_sub.add_parser('stop', help='停止')
    p_daemon_sub.add_parser('status', help='查看状态')

    sub.add_parser('list')
    sub.add_parser('catalog', help='重新生成在库清单（catalog.md）')
    p_term = sub.add_parser('term', help='术语查询（术语标准或术语章）')
    p_term.add_argument('std')
    p_term.add_argument('keyword', nargs='?', help='可选：按关键词过滤')
    p_apx = sub.add_parser('appendix', help='查看某本规范的附录条目')
    p_apx.add_argument('std')
    sub.add_parser('embed-build')
    sub.add_parser('embed-info')

    args = parser.parse_args()

    # 设置守护进程开关
    if getattr(args, 'no_daemon', False):
        _daemon_no = True
    
    # 加载规范全名映射
    std_name_map = {}
    try:
        # 改 with:连接异常时自动 close,杜绝泄漏
        with get_db() as _db:
            _cur = _db.cursor()
            _cur.execute('SELECT code, name FROM standards')
            std_name_map = {row[0]: row[1] for row in _cur.fetchall()}
    except:
        pass
    
    def fmt_std(code):
        name = std_name_map.get(code, code)
        return f'《{name}》{code}' if name != code else code
    
    if args.command == 'rebuild':
        cmd_rebuild(args)
    elif args.command == 'search':
        # 标准规范搜索（需要 jieba）
        try:
            # cmd_search() 是死代码(main L2799-2813 走 daemon-first),已删
            # 预加载：哪些规范是全文强条
            full_strong_stds = set()
            try:
                _db2 = get_db()
                _cur2 = _db2.cursor()
                _cur2.execute('SELECT code FROM standards WHERE strong_count = clause_count AND strong_count > 0')
                full_strong_stds = {row[0] for row in _cur2.fetchall()}
                _db2.close()
                # Load废止 data for search annotation
                try:
                    _db3 = get_db()
                    _cur3 = _db3.cursor()
                    _cur3.execute('SELECT target_std, source_std FROM abolished WHERE abolish_type="whole"')
                    _abolished_whole = {row[0]: row[1] for row in _cur3.fetchall()}
                    _cur3.execute('SELECT target_std, clause_nos, source_std FROM abolished WHERE abolish_type="clauses" AND clause_nos IS NOT NULL')
                    _abolished_clauses = {}
                    for row in _cur3.fetchall():
                        target, nos_str, source = row
                        if target not in _abolished_clauses:
                            _abolished_clauses[target] = {}
                        for no in nos_str.split(','):
                            no = no.strip()
                            if no:
                                _abolished_clauses[target][no] = source
                    _db3.close()
                except Exception as e:
                    print(f'  [stdq] 加载废止表(逐条)失败: {e}', file=sys.stderr)
                    _abolished_whole = {}
                    _abolished_clauses = {}
            except Exception as e:
                print(f'  [stdq] 加载废止表(整本)失败: {e}', file=sys.stderr)
                pass

            # _half_limit 直接用 args.limit(不除 2)
            #   除 2 会让下半限与 --limit 失去对应关系
            _style = getattr(args, 'table_style', 'markdown')
            _half_limit = args.limit

            # 走 daemon(LIKE 召回 + embedding 排序 + PER_CAP 防霸屏)
            try:
                group_strong, group_other = [], []
                # 类型必选提示(在调用 daemon 前打印,因为 daemon stderr 不发回 CLI)
                if not getattr(args, 'type', None):
                    print('  [stdq] 提示:未指定建筑类型(--type),返回全部类型结果。建议下次指定: --type 住宅/公建/工业')
                # "通用"=不限类型,传给 daemon 时当 None 处理
                type_filter = getattr(args, 'type', None)
                if type_filter == '通用':
                    type_filter = None
                scope = 'standard'

                _rows, _via_daemon = search_auto(
                    args.query,
                    limit=getattr(args, 'limit', 30),
                    std=getattr(args, 'std', None),
                    type_filter=type_filter,
                )
                std_results = [list(r) for r in _rows]
                if not _via_daemon:
                    print('  [提示] daemon 未运行，本次本地计算（结果一致，只是慢；装 daemon 见 README）', file=sys.stderr)
            except Exception as e:
                print(f'  ⚠️ 检索出错: {e}', file=sys.stderr)
                std_results = []

            # 格式化输出函数
            def _format_group(results, group_name, max_items=None):
                if not results:
                    return
                print(f'\n  ── {group_name} ──')
                if max_items is not None:
                    results = results[:max_items]
                for r in results:
                    std_code, clause_no, content, has_note, note = r[0], r[1], r[2], r[3], r[4]
                    is_s = r[5] if len(r) > 5 else 0
                    is_st = r[6] if len(r) > 6 else 0
                    markers = []
                    if is_s:
                        markers.append('🔴强条')
                    if is_st and not is_s:
                        markers.append('⚠️严格')
                    if std_code in full_strong_stds:
                        markers.append('📌全文强条')
                    if has_note:
                        markers.append('📖有说明')
                    if std_code in _abolished_whole:
                        markers.append(f'🚫已废止(by {_abolished_whole[std_code]})')
                    elif std_code in _abolished_clauses and clause_no in _abolished_clauses[std_code]:
                        markers.append(f'🚫已废止(by {_abolished_clauses[std_code][clause_no]})')
                    marker_str = ' '.join(markers)
                    print(f'  {fmt_std(std_code)} §{clause_no} {marker_str}')
                    for line in _format_clause_content(content, max_len=0, style=_style).split('\n'):
                        print(f'  {line}')
                    if has_note and note:
                        note_preview = _format_clause_content(note, max_len=0, style=_style)
                        for nline in note_preview.split('\n'):
                            print(f'  📝 说明: {nline}' if nline == note_preview.split('\n')[0] else f'    {nline}')

            if std_results:
                _format_group(std_results, '规范条文')

        except ModuleNotFoundError:
            print('  ⚠️ jieba 未安装，规范库搜索不可用')
        except Exception as e:
            print(f'  ⚠️ 规范库搜索出错: {e}')
    elif args.command == 'compare':
        results = cmd_compare(args)
        for std, clauses in results.items():
            print(f'\n=== {fmt_std(std)} ===')
            for r in clauses:
                std_code, clause_no, content, has_note, note = r[0], r[1], r[2], r[3], r[4]
                is_s = r[5] if len(r) > 5 else 0
                marker = ' 🔴' if is_s else ''
                _style = getattr(args, 'table_style', 'markdown')
                formatted = _format_clause_content(content, max_len=0, style=_style)
                if '\n' not in formatted:
                    # 单行内容：紧凑显示在 §X.Y.Z 后面
                    print(f'  §{clause_no}{marker}: {formatted}')
                else:
                    # 含表格等多行内容：换行展示，第一行接条款号
                    lines = formatted.split('\n')
                    print(f'  §{clause_no}{marker}: {lines[0]}')
                    for ln in lines[1:]:
                        print(f'    {ln}')
                if has_note and note:
                    note_preview = _format_clause_content(note, max_len=0, style=_style)
                    for nline in note_preview.split('\n'):
                        print(f'    📝 说明: {nline}' if nline == note_preview.split('\n')[0] else f'      {nline}')
    elif args.command == 'strong':
        results = cmd_strong(args)
        if not results:
            print(f'❌ 未找到规范 {args.std} 的强条')
        for r in results:
            print(f'  {fmt_std(r[0])} §{r[1]}')
            _style = getattr(args, 'table_style', 'markdown')
            for line in _format_clause_content(r[2], max_len=0, style=_style).split('\n'):
                print(f'  {line}')
    elif args.command == 'abolished':
        results = cmd_abolished(args)
        if not results:
            print(f'❌ 未找到规范 {args.std} 的废止信息')
        for r in results:
            source, target, name, nos, ab_type = r
            if ab_type == 'whole':
                print(f'  整本废止: 《{name}》{target}')
                print(f'    (由 {fmt_std(source)} 废止)')
            else:
                nos_list = nos.split(',') if nos else []
                preview = ', '.join(nos_list[:10]) + ('...' if len(nos_list) > 10 else '')
                print(f'  条文废止: 《{name}》{target} 第{preview}条')
                print(f'    (由 {fmt_std(source)} 废止)')
    elif args.command == 'clause':
        results = cmd_clause(args)
        if not results:
            print(f'❌ 未找到条文 {args.std} §{args.clause}')
        for r in results:
            print(f'  {fmt_std(r[0])} §{r[1]}')
            print(f'  {r[2]}')
            if not getattr(args, 'no_note', False) and len(r) > 4 and r[4]:
                print(f'  📝 说明: {r[4]}')
    elif args.command == 'catalog':
        cmd_catalog(args)
    elif args.command == 'term':
        rows = cmd_term(args)
        if not rows:
            print(f'❌ {args.std} 里没有匹配的术语条目')
        else:
            print(f'── {fmt_std(args.std)} 术语条目（{len(rows)} 条）──')
            for no, content in rows:
                print(f'  §{no}  {content[:110]}')
    elif args.command == 'appendix':
        rows = cmd_appendix(args)
        if not rows:
            print(f'❌ {args.std} 里没有附录条目')
        else:
            print(f'── {fmt_std(args.std)} 附录（{len(rows)} 条）──')
            for no, content in rows:
                print(f'  §{no}  {content[:110]}')
    elif args.command == 'info':
        cmd_info(args)
    elif args.command == 'list':
        cmd_list(args)
    elif args.command == 'embed-build':
        cmd_embed_build(args)
    elif args.command == 'embed-info':
        cmd_embed_info(args)
    elif args.command == 'daemon':
        from .daemon import cmd_start, cmd_stop, cmd_status
        cmd = getattr(args, 'daemon_command', None)
        if cmd == 'start':
            return cmd_start(args)
        if cmd == 'stop':
            return cmd_stop(args)
        return cmd_status(args)
    elif args.command == 'profession':
        # 新增: profession 主题管理
        if args.prof_command == 'list':
            with get_db() as db:
                cur = db.cursor()
                cur.execute("""SELECT COALESCE(profession, '(未分配)') as prof, COUNT(*)
                              FROM standards GROUP BY profession ORDER BY COUNT(*) DESC""")
                print("【库内规范 profession 主题分布】")
                for prof, n in cur.fetchall():
                    print(f"  {prof:18s} {n:3d} 本")
        elif args.prof_command == 'set':
            import re as _re
            m = _re.match(r"^([A-Z]{2,3}[-A-Z0-9]+?)(?:-\d{4})?$", args.std_code)
            base = m.group(1) if m else args.std_code
            if args.theme not in THEME_NAMES:
                print(f"❌ 主题代码无效: {args.theme}")
                print(f"   可选: {list(THEME_NAMES.keys())}")
            else:
                with get_db() as db:
                    cur = db.cursor()
                    # 匹配可能带或不带末尾年号
                    cur.execute("UPDATE standards SET profession = ? WHERE code LIKE ?",
                                (args.theme, base + '%'))
                    n = cur.rowcount
                if n > 0:
                    print(f"✅ 已设置 {n} 本 {base}* 规范的主题为 '{args.theme}' ({THEME_NAMES[args.theme]})")
                else:
                    print(f"⚠️ 未找到 {base}* 规范,请确认代号正确")
    else:
        parser.print_help()
