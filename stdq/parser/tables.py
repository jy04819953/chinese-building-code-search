"""parser · tables —— 公开版（逻辑搬运自生产实现）"""
from __future__ import annotations
import os, re, sys, json, sqlite3, argparse, subprocess, time

from .common import is_valid_clause_content


def _format_clause_content(content, max_len=0, is_note=False, style='markdown'):
    """格式化条文内容用于打印输出（第四次修订：默认全文输出）。

    默认全文输出：调用方多为 Agent / 批量脚本，截断会让下游拿到不完整条文。
    显式传 max_len>0 仍可截断（表格回退/调试场景）。

    style 取值：
        'markdown'（默认）：含 <table> 时输出 markdown 表格源码（前后散文保留）。
                          — 在支持 Markdown 渲染的客户端里最清爽：表格可读
        'compact'：表格折叠成"分类: 值 / 值"对齐纯文本。
                    — CLI/TUI 端在 markdown 源码显得冗长时的替代风格
        'raw'：原样返回，<table> 不动。
                    — 极端调试场景
    纯文字条文保持 max_len 截断行为（max_len=0 表示不截断）
    """
    if not content:
        return ''

    # === 路径 1：含表格 → 按 style 分流 ===
    if '<table>' in content:
        if style == 'raw':
            # 调试场景：原样返回（max_len 对 <table> 仍生效）
            if max_len > 0 and len(content) > max_len:
                return content[:max_len] + '...'
            return content
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, 'html.parser')
            # 收集 (kind, data) 元组：kind 是 'text' / 'table'
            parts = []
            pos = 0
            for tbl in soup.find_all('table'):
                tbl_str = str(tbl)
                idx = content.find(tbl_str, pos)
                if idx < 0:
                    idx = content.find('<table>', pos)
                    tbl_str = content[idx:content.find('</table>', idx) + len('</table>')]
                pre = content[pos:idx].rstrip()
                if pre:
                    parts.append(('text', pre))
                parts.append(('table', tbl_str))
                end_idx = content.find('</table>', idx)
                if end_idx >= 0:
                    pos = end_idx + len('</table>')
            tail = content[pos:].strip()
            if tail:
                parts.append(('text', tail))

            lines = []
            for kind, data in parts:
                if kind == 'table':
                    tbl_soup = BeautifulSoup(data, 'html.parser')
                    tbl = tbl_soup.find('table')
                    if tbl:
                        if style == 'compact':
                            compact_str = _html_table_to_compact(tbl)
                            if compact_str:
                                lines.append(compact_str)
                        else:  # markdown
                            md = _html_table_to_markdown(tbl)
                            if md:
                                lines.append(md)
                else:
                    lines.append(data.rstrip())
            out = '\n'.join(s for s in lines if s)
            return out
        except Exception as e:
            # 转 table 失败时回退到原行为（硬截断）
            print(f'  [stdq] 表格转换失败,回退硬截断: {e}', file=sys.stderr)
            if len(content) > max_len:
                return content[:max_len] + '...'
            return content

    # === 路径 2：纯文字 → 兼容原截断行为 ===
    if max_len > 0 and len(content) > max_len:
        return content[:max_len] + '...'
    return content

def _html_table_to_markdown(table):
    """把单个 <table> HTML 转成 markdown 表格。

    处理 rowspan / colspan：
    - colspan：合并单元格
    - rowspan：上方单元格的 markdown 单元格内容复制到下面 N-1 行（保证 markdown 表格渲染时形状一致）

    注：markdown 表格原生不支持 rowspan，所以降级为"复制占位"。视觉稍冗余，但所有数值/文字一字不丢。
    """
    grid, max_col, n_rows = _html_table_to_grid(table)
    if not grid:
        return ''

    # 输出 markdown
    lines = []
    for r_idx in range(n_rows):
        row_cells = grid.get(r_idx, {})
        cells_md = []
        for c in range(max_col + 1):
            if c in row_cells:
                text, is_cont = row_cells[c]
                if is_cont and not text:
                    cells_md.append('   ↑')
                elif is_cont:
                    cells_md.append(f'   ↑{text}')
                else:
                    cells_md.append(text or '   ')
            else:
                cells_md.append('   ')
        lines.append('| ' + ' | '.join(cells_md) + ' |')
        # 分隔行只放在表头下方
        if r_idx == 0:
            lines.append('|' + '|'.join(['---'] * (max_col + 1)) + '|')

    return '\n'.join(lines)

def _html_table_to_compact(table):
    """把单个 <table> HTML 转成对齐的纯文本（CLI/TUI 端友好）。

    策略：把 rowspan 折叠成"主名: 副项1 / 副项2"格式。
    例：原 markdown 是 5 行（含 4 个 ↑ 瓦屋面 占位），本函数输出类似：
       瓦屋面: 块瓦≥30 / 波形瓦≥20 / 沥青瓦≥20 / 金属瓦≥20
    """
    grid, max_col, n_rows = _html_table_to_grid(table)
    if not grid or n_rows == 0:
        return ''

    # 第一行通常作为表头（标题行）
    header_row = grid.get(0, {})
    header_cells = []
    for c in range(max_col + 1):
        if c in header_row:
            text, _ = header_row[c]
            header_cells.append(text or '')
        else:
            header_cells.append('')

    # 剩下的行作为数据：尝试合并 rowspan 主列相同的连续行为"主列: 子项1 / 子项2"
    # 这里采用更通用策略：每行输出"列1: 列2: ..."形式，跳过空/重复
    # 简化：连续 rowspan 的同主列归到一行，后面跟着非空子列用" / "连接
    output_lines = []
    last_main = None
    current_group = []  # 当前主列下挂的子串

    def flush_group():
        nonlocal last_main, current_group
        if not current_group and last_main is not None:
            output_lines.append(f'  {last_main}')
        elif current_group and last_main is not None:
            output_lines.append(f'  {last_main}: ' + ' / '.join(current_group))
        elif current_group:
            output_lines.append('  ' + ' / '.join(current_group))
        last_main = None
        current_group = []

    for r_idx in range(1, n_rows):
        row = grid.get(r_idx, {})
        # 第一列作为主名
        first = row.get(0, ('', False))
        main_text, is_cont_main = first[0], first[1]
        if not main_text or is_cont_main:
            # 延续上一主名
            if last_main is None:
                continue
            # 收集本行其余非空列
            sub_items = []
            for c in range(1, max_col + 1):
                if c in row:
                    txt, is_c = row[c]
                    if txt and not is_c:
                        sub_items.append(txt)
                    elif txt and is_c:
                        # 折叠的子项，跳过（rowspan 占位）
                        pass
                else:
                    pass
            if sub_items:
                current_group.extend(sub_items)
            continue

        # 新主名：先 flush 上一组
        flush_group()
        last_main = main_text
        # 收集本行其余非空列
        sub_items = []
        for c in range(1, max_col + 1):
            if c in row:
                txt, is_c = row[c]
                if txt and not is_c:
                    sub_items.append(txt)
        current_group = sub_items

    flush_group()
    return '\n'.join(output_lines)

def _html_table_to_grid(table):
    """共享：把 <table> 解析成二维 grid。返回 (grid, max_col, n_rows)。

    grid[r_idx][c_idx] = (text, is_continuation)
        is_continuation=True 表示该格被上方 rowspan 占位（内容已复制填充）
    """
    from bs4 import BeautifulSoup
    if not table:
        return {}, 0, 0

    rows = []
    for tr in table.find_all('tr'):
        cells = []
        for td in tr.find_all(['td', 'th']):
            text = td.get_text(' ', strip=True).replace('\n', ' ')
            cells.append({
                'text': text,
                'colspan': int(td.get('colspan', 1) or 1),
                'rowspan': int(td.get('rowspan', 1) or 1),
            })
        rows.append(cells)

    if not rows:
        return {}, 0, 0

    grid = {}
    max_col = 0
    for r_idx, cells in enumerate(rows):
        cur_col = 0
        grid.setdefault(r_idx, {})
        for cell in cells:
            while cur_col in grid[r_idx]:
                cur_col += 1
            colspan = cell['colspan']
            rowspan = cell['rowspan']
            text = cell['text']
            grid[r_idx][cur_col] = (text, False)
            for c in range(1, colspan):
                grid[r_idx][cur_col + c] = ('', False)
            for r in range(1, rowspan):
                nr = r_idx + r
                grid.setdefault(nr, {})
                for c in range(colspan):
                    grid[nr][cur_col + c] = (text, True)
            cur_col += colspan
        max_col = max(max_col, cur_col - 1)

    n_rows = max(grid.keys()) + 1 if grid else 0
    return grid, max_col, n_rows
