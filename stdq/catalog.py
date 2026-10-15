"""catalog —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time
from .consts import CATALOG_PATH, STANDARDS_DIR, THEME_NAMES, _TYPE_MAP_PATH
from datetime import datetime




def extract_std_code(filename):
    """从文件名提取规范代码"""
    # 找年份（最后一个19XX或20XX）
    year_matches = list(re.finditer(r'((?:19|20)\d{2})', filename))
    if not year_matches:
        return None
    year_match = year_matches[0]  # 取第一个年份（原始规范年份，修订年份在后面）
    year = year_match.group(1)
    before_year = filename[:year_match.start()]
    code_parts = re.findall(r'[A-Z]+|\d+', before_year)
    if code_parts:
        return '-'.join(code_parts) + '-' + year
    return None

def _normalize_std_code(code):
    """标准化规范代号：GB55038-2025 → GB-55038-2025
    
    数据库中的格式为 XX-XXXX-XXXX，但用户输入常省略前缀后的连字符。
    支持：GB55038-2025, GBT50353-2013, JGJ100-2015, DG-TJ08-205-2024 等
    """
    import re
    # 已经包含正确连字符的，直接返回
    if re.match(r'^[A-Z]+-\d', code):
        return code
    # 通用：字母+T+数字 → 字母-T-数字（T是标准类型标记）
    # 如 GBT50353 → GB-T-50353, JGJT67 → JGJ-T-67
    # 注意：T后跟字母时不拆分（如 DGTJ → 不拆，TJ是一个整体）
    m = re.match(r'^([A-Z]{2,})T(\d.*)$', code)
    if m:
        return f'{m.group(1)}-T-{m.group(2)}'
    # GB55038-2025 → GB-55038-2025
    m = re.match(r'^([A-Z]+)(\d[-\d]*\d{4})$', code)
    if m:
        return f'{m.group(1)}-{m.group(2)}'
    return code

def _load_type_map():
    """读取类型映射;文件缺失或格式错 → 返回空映射并提示(不中断)。"""
    empty = {'standards': {}, 'chapters': {}}
    if not os.path.exists(_TYPE_MAP_PATH):
        return empty
    try:
        with open(_TYPE_MAP_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return {'standards': d.get('standards') or {}, 'chapters': d.get('chapters') or {}}
    except Exception as e:
        print(f'  [type-map] 读取 {_TYPE_MAP_PATH} 失败,全部按"通用"处理: {e}', file=sys.stderr)
        return empty

def _classify_std(code, clause_no=None):
    """根据规范代号(可选条文号)判断适用建筑类型。

    数据来自外置的 type_map.json(见上);未配置时一律返回"通用"。
    standards 表命中规范级类型;chapters 表可做章节级细分(先试两级如 3.2,再试一级如 3)。
    返回:住宅 / 公建 / 工业 / 民用 / 通用
    """
    t = _TYPE_MAP['standards'].get(code)
    if t:
        return t
    if clause_no:
        ch = _TYPE_MAP['chapters'].get(code)
        if ch:
            parts = clause_no.split('.')
            for depth in (2, 1):
                if len(parts) >= depth:
                    hit = ch.get('.'.join(parts[:depth]))
                    if hit:
                        return hit
    return '通用'

def _collect_catalog_data():
    from .db import get_db  # 延迟导入：db 反向依赖本模块，避免循环
    """从规范库收集在库清单数据,返回字典(供 _write_catalog 渲染)。"""
    data = {'standards': []}

    # 规范库
    try:
        c = get_db().cursor()
        c.execute("SELECT code, name, updated_at FROM standards ORDER BY code")
        # 读取废止关系,给被整本废止且替代新版在库的旧版标注
        # 同时给替代新版标注"废止了旧版"(仅当旧版也在库)
        _ab_map_old = {}   # target_std -> source_std(被废旧版),仅替代新版在库
        _ab_map_new = {}   # source_std -> [target_std...](替代者废止了哪些),仅旧版在库
        try:
            _cs = get_db().cursor()
            _codes = {r[0] for r in _cs.execute('SELECT code FROM standards').fetchall()}
            for src, tgt in _cs.execute(
                    "SELECT source_std, target_std FROM abolished WHERE abolish_type='whole'").fetchall():
                if tgt in _codes and src in _codes:
                    _ab_map_old.setdefault(tgt, src)
                    _ab_map_new.setdefault(src, []).append(tgt)
        except Exception:
            pass
        rows = []
        for code, name, updated_at in c.fetchall():
            rows.append({
                'code': code,
                'name': name or '-',
                'type': _classify_std(code),
                'date': (updated_at or '')[:10] or '-',
                'abolished_by': _ab_map_old.get(code),   # 被谁整本废止
                'replaces': _ab_map_new.get(code, []),   # 整本废止了谁
            })
        c.connection.close()
        data['standards'] = rows
    except Exception as e:
        print(f'  [catalog] 读规范库失败:{e}', file=sys.stderr)

    # （只有规范库）
    return data


def _write_catalog():
    """根据 data 字典渲染 catalog.md(整文件覆盖,确保 4 库同步)。"""
    data = _collect_catalog_data()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')

    lines = [
        '# 规范库在库清单',
        '',
        '> **自动维护** · 每次 rebuild 后自动重写',
        f'> **生成时间**:{ts}',
        '> **本表位置**:独立文件,规范查询通用',
        '',
        '查询用法:`grep -i "<规范号>" catalog.md` 看是否在库',
        '',
        '---',
        '',
    ]

    # 规范库
    rows = data['standards']
    lines.append('## ▸ 规范库(standards.db)')
    lines.append('')
    lines.append(f'共 **{len(rows)}** 本。')
    lines.append('')
    lines.append('> 废止列说明:旧版被新版整本替代时,标 `🚫被《新版》替代`;新版行标 `整本替代《旧版》`。')
    lines.append('')
    # 构建 code -> name 映射(用于废止列显示全称)
    _name_by_code = {r['code']: r['name'] for r in rows}
    lines.append('| 标准号 | 全称 | 类型 | 废止状态 | 入库日期 |')
    lines.append('|--------|------|------|----------|----------|')
    for r in rows:
        abol = ''
        if r.get('abolished_by'):
            src = r['abolished_by']
            src_name = _name_by_code.get(src, src)
            abol = f'🚫被《{src_name}》替代'
        elif r.get('replaces'):
            repl = '、'.join(_name_by_code.get(t, t) for t in r['replaces'])
            abol = f'整本替代《{repl}》'
        lines.append(f'| `{r["code"]}` | {r["name"]} | {r["type"]} | {abol} | {r["date"]} |')
    lines.append('')

    lines += [
        '',
        '---',
        '',
        '## 维护说明',
        '',
        '- 触发点：`stdq rebuild` 末尾自动重写',
        '- 重写策略：整文件覆盖',
        '- 类型字段：按 `type_map.json`（可选，见 examples/）判定，未配置时统一为「通用」',
        '- 查询用法：`grep -i <标准号> $STDQ_DATA_DIR/catalog.md`',
    ]

    try:
        with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        print(f'  [catalog] ✅ 已重写 {CATALOG_PATH}', file=sys.stderr)
    except Exception as e:
        print(f'  [catalog] ⚠️ 写 catalog.md 失败:{e}', file=sys.stderr)

def refresh_catalog():
    """对外暴露的入口:被 rebuild 调用。"""
    _write_catalog()

def _ask_profession_interactive(std_code, name, matched_themes=None):
    """新增:入库时主动问主题分配(回滚静默兜底)。

    - 有 stdin(CLI 终端)→ 交互式弹问
    - 无 stdin(脚本/管道/IM 客户端等非交互环境)→ 自动用通用(general)

    任何入口(CLI / 编程调用 / 非交互环境)走 cmd_rebuild 都会调到本函数;
    非交互环境下 stdin 不可用,自动回退 'general' 兜底——后续可手动
    用 `stdq profession set <code> <theme>` 改。
    """
    options = list(THEME_NAMES.keys())
    default_idx = options.index('general') + 1  # C2 修复:原写死 9=通用,13 主题后 9 是 fire
    prompt = (
        f"\n[入库询问] 规范 {std_code} ({name})\n"
        f"该规范分到哪个主题?\n"
    )
    for i, code in enumerate(options, 1):
        prompt += f"  {i}. {THEME_NAMES[code]} ({code})\n"
    prompt += f"请输入 1-{len(options)} (默认 {default_idx}=通用): "

    try:
        # 检查 stdin 是否可用
        if not sys.stdin.isatty():
            # 非交互环境(批处理/管道/IM 客户端):回退通用
            print(f"[入库询问] stdin 不可用,自动用通用(general)兜底,后续可 `stdq profession set {std_code} <theme>` 改", file=sys.stderr)
            return 'general'

        ans = input(prompt).strip()
        if not ans:
            ans = str(default_idx)
        idx = int(ans) - 1
        if 0 <= idx < len(options):
            return options[idx]
        return 'general'
    except (EOFError, KeyboardInterrupt, ValueError):
        return 'general'


# 模块加载完成后初始化（_load_type_map 在本文件内定义）
_TYPE_MAP = _load_type_map()
