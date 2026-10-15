# -*- coding: utf-8 -*-
"""把"缺的附录"从局部转换出来的 MD 拼回规范主 MD。

用法:
  python3 splice_appendix.py <std_code> <转换出来的MD>            # dry-run，只报告
  python3 splice_appendix.py <std_code> <转换出来的MD> --apply    # 写回（自动备份）

逻辑：
  ① 主 MD 的附录区 = [附录首条, 用词说明/引用标准名录/条文说明 之前)
  ② 两边的附录块都按 `附录X` 标题切块
  ③ 按字母序合并（主 MD 已有优先；转换稿里新出现的插到正确位置）
  ④ 只重写附录区，其余不碰
"""
import os, re, sys, shutil, datetime, sqlite3
from ..consts import STANDARDS_DIR

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..parser.appendix import find_appendix_start
from ..parser.notes import is_notes_marker

D = STANDARDS_DIR
cur = sqlite3.connect(os.path.join(D, 'standards.db')).cursor()
HEAD = re.compile(r'^#{0,4}\s*附录\s*([A-Z])\s*(.*)$')
STOP = re.compile(r'^#{0,4}\s*(本[规范规程标准]*用词说明|用词说明|引用标准名录|引用标准|本规程用词说明)')


def blocks(lines):
    """按 附录X 标题切块 → [(letter, [lines])]；标题前的零碎行丢掉。"""
    out, curL, buf = [], None, []
    for l in lines:
        m = HEAD.match(l.strip())
        if m:
            if curL:
                out.append((curL, buf))
            curL, buf = m.group(1), [l]
        elif curL:
            buf.append(l)
    if curL:
        out.append((curL, buf))
    return out


def region(lines):
    s = find_appendix_start(lines)
    if s is None:
        return None, None
    e = len(lines)
    for j in range(s + 1, len(lines)):
        t = lines[j].strip()
        if is_notes_marker(t) or STOP.match(t):
            e = j
            break
    return s, e


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else None
    new_md = sys.argv[2] if len(sys.argv) > 2 else None
    APPLY = '--apply' in sys.argv
    if __name__ == '__main__' and code is None:
        print('用法: python -m stdq.tools.splice_appendix <规范号> <新附录MD> [--apply]')
        raise SystemExit(1)
    mf = cur.execute("SELECT md_file FROM standards WHERE code=?", (code,)).fetchone()[0]
    p = mf if os.path.isabs(mf) else os.path.join(D, mf)
    old = open(p, encoding='utf-8').read()
    lines = old.split('\n')
    s, e = region(lines)
    if s is None:
        print(f'!! {code}: 主 MD 里找不到附录区，终止'); sys.exit(1)
    have = blocks(lines[s:e])
    new = blocks(open(new_md, encoding='utf-8').read().split('\n'))
    haveL = [k for k, _ in have]
    newL = [k for k, _ in new]
    add = [k for k in newL if k not in haveL]
    print(f'{code}: 主 MD 附录区 {haveL}')
    print(f'    转换稿附录 {newL}  → 新增 {add}')
    if not add:
        print('    无需改动'); sys.exit(0)

    merged = dict(have)
    merged.update({k: b for k, b in new if k not in merged})          # 已有优先
    order = sorted(merged.keys())
    body = []
    for k in order:
        blk = merged[k]
        body.append('\n'.join(blk).strip('\n'))
    out_lines = lines[:s] + ['\n\n'.join(body)] + lines[e:]
    new_text = '\n'.join(out_lines)
    if not APPLY:
        print(f'    [DRY-RUN] 合并后附录 {order}；主 MD {len(old)} → {len(new_text)} 字节')
        sys.exit(0)
    bak = p + '.bak.' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-apxfill'
    shutil.copy2(p, bak)
    open(p, 'w', encoding='utf-8').write(new_text)
    print(f'    已写回（备份 {os.path.basename(bak)}）；{len(old)} → {len(new_text)} 字节；附录 {order}')



if __name__ == '__main__':
    main()