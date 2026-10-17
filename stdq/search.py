"""检索：召回 + 排序。

主路径（不需要 daemon，CLI 直连也走这条）：
  jieba 拆词 + LIKE 召回  →  同义词表组内互扩  →  章节标题扩展词召回
  →  embedding 余弦  →  章节命中加权  →  BM25 词级融合  →  单本规范上限

依赖分级（缺了只是能力弱一点，不是不能用）：
  · 只有 jieba            → LIKE 召回 + 排序（可用）
  · + numpy/模型/向量文件  → 再加余弦语义排序、章节扩展词、章节加权
"""
from __future__ import annotations
import os, re, math, json, sqlite3, sys
from typing import Optional

try:
    import numpy as np
except ImportError:  # 语义排序是可选的；没有 numpy 时用关键词 + BM25 排序
    np = None

from .consts import DB_PATH, STANDARDS_DIR, THEME_BONUS, THEME_KEYWORDS, SOCK_PATH
from .db import get_db
from .catalog import _classify_std

# —— 懒加载的全局状态（首次检索时准备一次）——
_model = None
_embeddings = None
_ids = None
_synonym_groups = None
chapter_titles_meta = None
chapter_title_embs = None
chapter_title_lookup = None
_bm25_N = 0
_bm25_avgdl = 1.0
_bm25_doc_tf = None
_bm25_doc_len = None
_bm25_df = None
_loaded = {"model": False, "bm25": False, "chapter": False}


_synonym_groups = None

chapter_titles_meta = None

chapter_title_embs = None


_bm25_N = 0

_bm25_avgdl = 1.0

_bm25_doc_tf = None



_BM25_BETA = 0.30

CHAPTER_TITLE_TOPN = 10

_bm25_doc_len = None

_bm25_df = None

chapter_title_lookup = None


def do_search(model, embeddings, ids, query, limit=10, std_code=None, type_filter=None):
    """主路径搜索(立 + 章节加权)

    排序:embedding 余弦相似度 + 章节命中加分
    - 召回:LIKE 关键词匹配 + jieba 拆词(纯筛选用,不参与排序)
    - 排序:`model.encode(query)` 算向量,跟候选 embedding 算余弦距离
    - **章节命中加分**:子条文所属章节标题含查询关键词 → sims + 0.3
      (clause_chapters 表在 cmd_rebuild / backfill_chapters 填好)
    - 单本规范上限:PER_CAP=8(防霸屏)

    已知取舍:
    - 短查询的 top1 可能被专业规范抢(语义近 ≠ 想要的主题);
      用 --type 限定建筑类型可缓解。

    参数边界(L42-45):limit ≤0 回退 10,>200 cap 200。
    """
    import jieba
    # 参数边界校验(防 limit=0/-1 返回异常)
    if limit is None or limit <= 0:
        limit = 10
    if limit > 200:
        limit = 200

    # 注意:类型必选提示由 CLI 端打印(daemon stderr 不发回 socket)

    # B7:连接泄漏修复——std_db 走 with get_db() 自动 close,
    # 异常路径(model.encode / np.dot 抛错)也不会泄漏连接。
    with get_db() as std_db:
        std_c = std_db.cursor()

        # 构建适用类型/规范过滤条件
        type_stds = None
        type_in_sql, type_in_params = '1=1', ()
        if type_filter:
            # applicable_type 在 clauses 表里,不在 standards 表里
            # NULL 已回填成'通用',SQL 收紧成精确匹配(不再 OR IS NULL)
            std_c.execute('SELECT DISTINCT std_code FROM clauses WHERE applicable_type = ?',
                           (type_filter,))
            type_stds = list({r[0] for r in std_c.fetchall()})
            type_in_sql, type_in_params = _in_clause('std_code', type_stds)
            # type_stds 为空 → 直接返回空结果(防 IN () 语法错误 + 省 LIKE 召回)
            if not type_stds:
                return []

        # 1. LIKE 召回候选(关键词命中)
        words = [w.strip() for w in jieba.cut(query) if w.strip() and len(w.strip()) > 1]
        if not words:
            words = [query]
        # 同义词表扩展
        # 只进召回词集,不进排序;组内互扩、组间隔离。
        # 解决"强电间→变电所"词面盲区(语义排序兜不住没进池的条文)。
        # 扩展词命中但排序低的自然沉底,不改变输出噪声水平。
        words = _expand_synonyms(words)

        # 整体短语命中
        # inner_limit 取大值,确保所有相关条文都被召回
        #   小 LIMIT 会在单本规范过滤后把物理位置靠后的条文截掉(命中多的规范反被挤出去),
        inner_limit = 5000
        candidates = set()
        # IN 子句走 _in_clause 工具函数(占位符数 == params 数,空集合已短路)
        base_or = '(content LIKE ? OR note LIKE ?)'
        std_c.execute(f'SELECT id FROM clauses WHERE {base_or} AND {type_in_sql} LIMIT {inner_limit}',
                      (f'%{query}%', f'%{query}%', *type_in_params))
        for r in std_c.fetchall():
            candidates.add(r[0])
        # 拆词命中
        for w in words:
            std_c.execute(f'SELECT id FROM clauses WHERE {base_or} AND {type_in_sql} LIMIT {inner_limit}',
                          (f'%{w}%', f'%{w}%', *type_in_params))
            for r in std_c.fetchall():
                candidates.add(r[0])

        # 章节标题扩展查询词(bge 召回到 top 10 章节标题,送进 LIKE 召回)
        # 解决 jieba 拆词拆出"做法"等通用词,召不到"卷材、涂膜、防水层"建筑专业术语的问题
        expanded_terms = _expand_query_via_chapters(model, query) if model is not None else []
        for term in expanded_terms:
            # C6 回退:改回整段章节标题 LIKE(约定)。
            # 原 C6 拆词版(先按连接词拆段再 jieba 拆词,每个 ≥2 字词单独 LIKE)
            # 把候选池从 ~200 条撑到 ~6600 条(80 本规范全卷入),噪声爆炸。
            # 整段 LIKE 实测召回少(8/30 查询扩展词零新增),但扩展词只是辅助通道,
            # jieba 拆词 + semantic 兜底 + 章节命中加权仍兜底,故按用户意愿回退。
            std_c.execute(f'SELECT id FROM clauses WHERE {base_or} AND {type_in_sql} LIMIT {inner_limit}',
                          (f'%{term}%', f'%{term}%', *type_in_params))
            for r in std_c.fetchall():
                candidates.add(r[0])

        # 单本规范过滤(std_code 参数)
        if std_code:
            # 只保留该规范的 id
            std_c.execute('SELECT id FROM clauses WHERE std_code LIKE ?', (f'%{std_code}%',))
            allowed_ids = {r[0] for r in std_c.fetchall()}
            candidates &= allowed_ids

        if not candidates:
            return []

        # 2. 对候选条文,用 embedding 相似度排序
        cand_list = list(candidates)
        # 建立 clause_id -> embedding index
        id_to_idx = {int(ids[i]): i for i in range(len(ids))} if ids is not None else {}
        cand_with_emb = [(cid, id_to_idx[cid]) for cid in cand_list if cid in id_to_idx]

        # semantic 兜底(方案 3)
        # 对所有有 embedding 的 clauses 算余弦,召 top 30 加入 candidates
        # 解决 LIKE 拆词拆碎 / 章节扩展词召不回具体条文的问题(口语 query)
        # 单线程内顺序跑,不发起新 socket 请求(不并发，避免并发写坏索引)
        SEMANTIC_FALLBACK_TOP = 30
        try:
            q_vec_sf = model.encode([query], normalize_embeddings=True, show_progress_bar=False)
            all_sims = np.dot(embeddings, q_vec_sf.T).flatten()
            sf_top_idx = np.argsort(all_sims)[::-1][:SEMANTIC_FALLBACK_TOP]
            sf_ids = {int(ids[i]) for i in sf_top_idx}
            added = 0
            for sid in sf_ids:
                if sid not in candidates:
                    candidates.add(sid)
                    added += 1
            if added > 0:
                # 静默:不在 stdout 打(daemon stderr 不发回 socket)
                pass
            # 同步 candidates (cand_list 还没生成,后面会再生成)
        except Exception:
            # semantic 兜底失败不致命,降级到现有 LIKE + jieba 召回
            pass

        # 重建 cand_list(已包含 semantic 兜底新加的)
        cand_list = list(candidates)
        # semantic 兜底加回的 id 可能混入其它规范,若指定了 std_code 需重新过滤
        if std_code:
            std_c.execute('SELECT id FROM clauses WHERE std_code LIKE ?', (f'%{std_code}%',))
            _allowed = {r[0] for r in std_c.fetchall()}
            cand_list = [c for c in cand_list if c in _allowed]
        cand_with_emb = [(cid, id_to_idx[cid]) for cid in cand_list if cid in id_to_idx]

        if not cand_with_emb:
            # 没有向量可用（未装语义依赖 / 未生成向量文件）→ 用 BM25 词级分排序。
            # 这里刻意不用 numpy：这条路径正是「没装语义依赖」时的路径。
            if _bm25_doc_tf is not None and words:
                _scored = sorted(((_bm25_score(words, cid), cid) for cid in cand_list), reverse=True)
                sorted_candidates = [cid for _, cid in _scored]
            else:
                sorted_candidates = cand_list[:limit * 3]
        else:
            # 复用 q_vec(如果上面 semantic 兜底算了就复用,否则重新算)
            try:
                q_vec = q_vec_sf  # type: ignore
            except NameError:
                q_vec = model.encode([query], normalize_embeddings=True)
            cand_ids = [c[0] for c in cand_with_emb]
            cand_idxs = [c[1] for c in cand_with_emb]
            cand_embs = embeddings[cand_idxs]
            if cand_embs.size == 0:
                return []          # 库里还没有向量（或候选都不在向量表里）
            sims = np.dot(cand_embs, q_vec.T).flatten()
            # 章节命中加权(BGE 章节 sims)——子条文所属章节标题与查询 sim 加权
            # 传 q_vec 给 _apply_chapter_boost,避免重复 model.encode
            _apply_chapter_boost(std_c, cand_ids, query, words, sims, q_vec=q_vec)
            # 方案 A — BM25 词级分加权融合
            # 补纯 embedding 对数字/编号/罕见术语不敏感("疏散距离 25m" vs 30m)
            # final = sims + BETA * bm25_norm,其中 bm25_norm 归一化到 [0,1]
            if _bm25_doc_tf is not None and words:
                bm25_scores = np.array([_bm25_score(words, cid) for cid in cand_ids])
                bm25_max = bm25_scores.max() if len(bm25_scores) else 0.0
                if bm25_max > 0:
                    sims = sims + _BM25_BETA * (bm25_scores / bm25_max)
            # 按相似度降序
            order = np.argsort(sims)[::-1]
            sorted_candidates = [cand_ids[i] for i in order]  # 保留全部候选,PER_CAP 在第3步过滤

        # 3. 取条文详情(单本规范上限防霸屏)
        PER_CAP = 8  # 单本规范最多 8 条
        from collections import defaultdict
        # 整本废止过滤——被整本废止且替代新版在库的旧版,正常搜索屏蔽
        _abw = _abolished_whole_in_library(std_c)
        std_count = defaultdict(int)
        results = []
        for clause_id in sorted_candidates:
            std_c.execute('SELECT std_code, clause_no, content, has_note, note, is_strong, is_strict FROM clauses WHERE id=?', (clause_id,))
            row = std_c.fetchone()
            if row:
                if std_code and std_code.lower() not in row[0].lower():
                    continue
                sc = row[0]
                # 整本废止过滤(标准号不显式点名旧版时屏蔽)
                if sc in _abw and not _query_explicitly_targets(query, std_code, sc):
                    continue
                if std_count[sc] >= PER_CAP:
                    continue  # 该规范已满，跳过
                results.append([row[0], row[1], row[2], row[3], row[4], row[5], row[6]])
                std_count[sc] += 1
                if len(results) >= limit:
                    break

    
        return results


def _apply_chapter_boost(std_c, cand_ids, query, words, sims, q_vec=None):
    """章节命中加权（BGE 章节 sims，替代字符串包含）

    解决字符串包含的"致命盲区":
      - 查询"屋面防水层做法" jieba 拆 ['屋面', '防水层', '做法']
      - 章节"防水工程施工" → '防水层' in '防水工程施工' = False(差一个"层"字)
      - 字符串包含漏判,但 bge sim 算出来 0.82(主题相关)
      - 改用 bge sims 判定:章节标题与查询 sim > 0.6 时加分 = sim × 0.15
      - 绝对不扣分(约定"召回池宁可噪,不可缺")

    输入:
      - std_c:DB cursor
      - cand_ids:候选条文 id 列表
      - query:原查询
      - words:jieba 拆词(保留作为 fallback)
      - sims:numpy 数组(就地修改)
      - q_vec:查询 bge 向量(优先用,不再重复 model.encode)
    - 副作用:就地修改 sims
    - 数据源:chapter_title_embs(daemon 启动时预编码)
    - 降级:q_vec 为 None 或 chapter_title_embs 未加载 → 走字符串包含 fallback
    """
    if not cand_ids or not query:
        return

    # 1. 拿候选的 (std_code, clause_no)
    placeholders = ','.join('?' * len(cand_ids))
    std_c.execute(f'SELECT id, std_code, clause_no FROM clauses WHERE id IN ({placeholders})',
                  cand_ids)
    id_to_meta = {r[0]: (r[1], r[2]) for r in std_c.fetchall()}

    # 2. 派生每条候选的"父章节"
    parent_lookup = {}  # cid -> parent_clause_no
    parent_set = set()
    for cid, (sc, cn) in id_to_meta.items():
        parts = cn.split('.')
        if len(parts) >= 2:
            parent = '.'.join(parts[:-1])
        else:
            parent = cn
        parent_lookup[cid] = parent
        parent_set.add((sc, parent))

    if not parent_set:
        return

    # 3. 主路径:用 BGE 章节 sims 算加权
    if q_vec is not None and chapter_title_embs is not None and chapter_title_lookup is not None:
        # 用 numpy 一次性算所有 parent 的 sim,而不是逐个 encode
        # 找每个 (sc, parent) 在 chapter_title_lookup 里的 idx
        parent_sims = {}  # (sc, parent) -> sim
        q_norm = q_vec.flatten()
        for sc, parent in parent_set:
            key = (sc, parent)
            idx = chapter_title_lookup.get(key)
            if idx is not None:
                title_emb = chapter_title_embs[idx]
                parent_sims[key] = float(np.dot(title_emb, q_norm))
        # 对每条候选,根据 parent sim 加分
        CHAPTER_BOOST_THRESHOLD = 0.6  # 弱相关起步
        CHAPTER_BOOST_WEIGHT = 0.15     # 加权系数
        for i, cid in enumerate(cand_ids):
            parent = parent_lookup.get(cid)
            if parent is None:
                continue
            sc = id_to_meta[cid][0]
            sim = parent_sims.get((sc, parent), 0.0)
            if sim > CHAPTER_BOOST_THRESHOLD:
                sims[i] += sim * CHAPTER_BOOST_WEIGHT
        return

    # 4. Fallback:字符串包含判定(老逻辑,保留防御)
    # 用 SQL 一次查所有父章节的标题
    BATCH = 200
    chapter_titles = {}  # (std_code, clause_no) -> title
    parent_list = list(parent_set)
    for i in range(0, len(parent_list), BATCH):
        batch = parent_list[i:i + BATCH]
        params = []
        for sc, cn in batch:
            params.extend([sc, cn])
        std_c.execute(f'SELECT std_code, clause_no, title FROM clause_chapters '
                      f'WHERE (std_code, clause_no) IN ({",".join(["(?, ?)"] * len(batch))})',
                      params)
        for r in std_c.fetchall():
            chapter_titles[(r[0], r[1])] = r[2]
    for i, cid in enumerate(cand_ids):
        if cid not in id_to_meta:
            continue
        sc, cn = id_to_meta[cid]
        title = chapter_titles.get((sc, parent_lookup[cid]), '')
        if not title:
            continue
        if query in title:
            sims[i] += 0.3
            continue
        hit_count = sum(1 for w in words if w in title)
        if hit_count >= 2:
            sims[i] += 0.25
        elif hit_count >= 1:
            sims[i] += 0.15


def _bm25_score(words, cid, k1=1.5, b=0.75):
    """单条 BM25 分(标准 BM25 公式)

    对候选条文 cid 按查询词列表算 BM25 分。索引未构建或词未命中返回 0。
    - 罕见词(数字/编号/专业术语)df 小 → idf 大 → 权重高
    - 常见词("的/应/和")df 大 → idf≈0 → 基本不贡献
    调用方已在 do_search 融合处归一化(bm25_norm = score / max)。
    """
    if _bm25_doc_tf is None or not words:
        return 0.0
    tf = _bm25_doc_tf.get(cid)
    if not tf:
        return 0.0
    dl = _bm25_doc_len.get(cid, 0)
    if dl <= 0:
        return 0.0
    s = 0.0
    for w in words:
        f = tf.get(w, 0)
        if f == 0:
            continue
        dfw = _bm25_df.get(w, 0)
        idf = math.log(1 + (_bm25_N - dfw + 0.5) / (dfw + 0.5))
        s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / _bm25_avgdl))
    return s


def _expand_query_via_chapters(model, query):
    """章节标题扩展查询词

    用 bge 算查询和所有章节标题的余弦相似度,取 top CHAPTER_TITLE_TOPN。
    返回章节标题列表(供 LIKE 召回用)。

    空查询或未加载章节标题 → 返回 []。
    """
    if chapter_title_embs is None or not query:
        return []
    q_vec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)
    sims = np.dot(chapter_title_embs, q_vec.T).flatten()
    top_idxs = np.argsort(sims)[::-1][:CHAPTER_TITLE_TOPN]
    result = [chapter_titles_meta[i][2] for i in top_idxs]
    return result


def load_bm25_index():
    """BM25 词级排序索引(daemon 启动时调用一次,方案 A)

    从规范库读全部 (id, content),jieba 分词后统计:
    - df: term -> 文档频率(全库)
    - doc_tf: cid -> {term: tf}
    - doc_len: cid -> 词数
    供条文检索排序阶段算 BM25 分,与 embedding sims 加权融合。

    ID 空间(扩展规范库):
    - 规范库 clauses.id 正数(18768 条)
    构建 BM25 时用同一 ID 空间,`_bm25_score` 直接按调用方传入的候选 id 打分。

    实测(N=20568):构建 ~9s,单查询全库打分 0.015s(候选池内更快)。
    失败不致命:融合处 _bm25_score 返回 0,等效回退纯 embedding。
    """
    global _bm25_df, _bm25_doc_tf, _bm25_doc_len, _bm25_N, _bm25_avgdl
    import jieba
    doc_tf = {}
    doc_len = {}
    df = {}
    total_len = 0
    n = 0

    def _feed(cid, content):
        """单条 feed:jieba 分词 → 更新 doc_tf / doc_len / df"""
        nonlocal n, total_len
        toks = [w.strip() for w in jieba.cut(content or '') if w.strip() and len(w.strip()) > 1]
        tf = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        doc_tf[cid] = tf
        doc_len[cid] = len(toks)
        total_len += len(toks)
        for w in tf:
            df[w] = df.get(w, 0) + 1
        n += 1

    # 1. 规范库
    with get_db() as db:
        c = db.cursor()
        c.execute('SELECT id, content FROM clauses')
        for cid, content in c.fetchall():
            _feed(cid, content)
    # （只索引规范条文的 content）

    _bm25_df = df
    _bm25_doc_tf = doc_tf
    _bm25_doc_len = doc_len
    _bm25_N = n
    _bm25_avgdl = total_len / max(n, 1)
    # flush=True:daemon 把 stderr 重定向到文件后默认块缓冲,不 flush 会吞日志
    print(f'BM25 索引构建完成: 规范库共 {n} 条文, 词典 {len(df)} 词, avgdl={_bm25_avgdl:.1f}', file=sys.stderr, flush=True)


def load_chapter_embeddings(model):
    """章节标题预编码(daemon 启动时调用一次)

    从 clause_chapters 表读所有章节标题,bge 编码后存内存。
    供 do_search 召回阶段算 top 章节标题作为扩展查询词。

    输出:写到全局变量 chapter_title_embs / chapter_titles_meta / chapter_title_lookup
    """
    global chapter_title_embs, chapter_titles_meta, chapter_title_lookup
    with get_db() as db:
        c = db.cursor()
        c.execute('SELECT std_code, clause_no, title FROM clause_chapters ORDER BY id')
        rows = c.fetchall()
    chapter_titles_meta = [(r[0], r[1], r[2]) for r in rows]
    titles = [r[2] for r in rows]
    print(f'章节标题预编码: {len(titles)} 条', file=sys.stderr)
    chapter_title_embs = model.encode(titles, normalize_embeddings=True,
                                      show_progress_bar=False,
                                      batch_size=64)
    # 建 (std_code, clause_no) -> idx 索引,加速 _apply_chapter_boost_semantic 查章节 embedding
    chapter_title_lookup = {(r[0], r[1]): i for i, r in enumerate(rows)}
    print(f'章节标题预编码完成: shape={chapter_title_embs.shape}', file=sys.stderr)


def load_synonyms():
    """读取同义词表(STANDARDS_DIR/synonyms.txt)

    每行一组(英文逗号分隔),# 开头为注释。构建 word -> 同组词 的 dict。
    失败不致命:返回空 dict(等效无扩展,召回行为与改前一致)。
    """
    global _synonym_groups
    _synonym_groups = {}
    path = os.path.join(STANDARDS_DIR, 'synonyms.txt')
    try:
        with open(path, encoding='utf-8') as f:
            group_count = 0
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                group = [w.strip() for w in line.split(',') if w.strip()]
                if len(group) >= 2:
                    group_count += 1
                    for w in group:
                        _synonym_groups.setdefault(w, group)
        print(f'同义词表加载完成: {group_count} 组, {len(_synonym_groups)} 个词', file=sys.stderr, flush=True)
    except FileNotFoundError:
        print('同义词表未找到(synonyms.txt),跳过:无同义词扩展', file=sys.stderr, flush=True)
    except Exception as e:
        print(f'同义词表读取失败: {e},跳过:无同义词扩展', file=sys.stderr, flush=True)


def _in_clause(column, values):
    """生成 `column IN (?, ?, ...)` 的安全片段 + 参数 tuple

    防止两类问题:
    1. values 为空时直接返回 ('1=0', ()) 让外层短路(等价于永不命中,不会触发 IN () 语法错误)
    2. 占位符统一参数化绑定,杜绝把 values 内容拼到 SQL 字符串里(避免未来误改引入注入)

    用法:`sql, params = _in_clause('std_code', list(type_stds)); sql = f'... WHERE {sql} ...'`
    """
    if not values:
        # 空集合短路:外层 WHERE 会加上 AND (1=0) 让候选集为空,do_search 在 L116 之后 return []
        return '1=0', ()
    placeholders = ','.join(['?'] * len(values))
    return f'{column} IN ({placeholders})', tuple(values)

# ===================== 同义词表与废止关系辅助 =====================

def _load_synonym_groups():
    """读取同义词表(STANDARDS_DIR/synonyms.txt),返回 word -> 同组词 dict

    失败不致命:返回空 dict(等效无扩展,召回行为与改前一致)。
    """
    groups = {}
    path = os.path.join(STANDARDS_DIR, 'synonyms.txt')
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                group = [w.strip() for w in line.split(',') if w.strip()]
                if len(group) >= 2:
                    for w in group:
                        groups.setdefault(w, group)
    except Exception:
        pass
    return groups


def _expand_synonyms(words: list[str]) -> list[str]:
    """同义词表组内互扩(立,与 daemon 端 do_search 行为一致)

    把查询词集里的词,按同义词表扩展:任一词命中 → 同组词全加进来。
    只进召回词集,不进排序;组内互扩、组间隔离。
    原词保留,不替换用户的数字/限定词。
    """
    groups = _load_synonym_groups()
    if not groups or not words:
        return words
    out = list(words)
    seen = set(words)
    for w in words:
        group = groups.get(w)
        if group:
            for g in group:
                if g not in seen:
                    seen.add(g)
                    out.append(g)
    return out


def _abolished_whole_in_library(std_c):
    """返回 {target_std: source_std}:被整本废止且替代新版也在库的映射。

    立:正常搜索应屏蔽"被整本废止 + 替代新版在库"的旧版,
    除非搜索显式点名该旧版。
    """
    try:
        inlib = {r[0] for r in std_c.execute('SELECT code FROM standards').fetchall()}
        mapping = {}
        for src, tgt in std_c.execute(
                "SELECT source_std, target_std FROM abolished WHERE abolish_type='whole'").fetchall():
            if tgt in inlib and src in inlib:
                mapping[tgt] = src
        return mapping
    except Exception:
        return {}


def _query_explicitly_targets(query, std_code, target_std):
    """判定这次搜索是否显式点名了被废旧版(专门搜这个版本 → 放行不过滤)。"""
    try:
        if std_code:
            q_n = std_code.replace('-', '').replace(' ', '').lower()
            t_n = target_std.replace('-', '').replace(' ', '').lower()
            if q_n and t_n in q_n:
                return True
        if not query:
            return False
        q = query.replace(' ', '')
        full = target_std.replace('-', '').replace(' ', '').lower()
        if full and full in q.lower():
            return True
        year = target_std.split('-')[-1]
        if len(year) == 4 and year.isdigit() and year in q:
            return True
        return False
    except Exception:
        return False


# ===================== 门面：CLI 直接调用（无需 daemon）=====================

def _ensure_model():
    """按需加载语义模型与向量文件；任何一步不可用就返回 None（降级为关键词检索）。"""
    global _model, _embeddings, _ids
    if _loaded["model"]:
        return _model, _embeddings, _ids
    _loaded["model"] = True          # 只试一次，失败不反复重试
    if np is None:
        return None, None, None
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("BAAI/bge-base-zh-v1.5",
                                     cache_folder=os.path.expanduser("~/.cache/huggingface"))
        emb_dir = os.path.join(STANDARDS_DIR, "embeddings")
        _ids = np.load(os.path.join(emb_dir, "ids.npy"))
        _embeddings = np.load(os.path.join(emb_dir, "embeddings.npy"))
    except Exception as e:
        print(f"  [stdq] 语义检索不可用（{type(e).__name__}: {str(e)[:60]}），改用关键词 + BM25 排序",
              file=sys.stderr)
        _model = _embeddings = _ids = None
    return _model, _embeddings, _ids


def search(query: str, limit: int = 30, std: Optional[str] = None,
           type_filter: Optional[str] = None) -> list:
    """检索入口：CLI 与 daemon 走的是同一套逻辑与排序，结果一致。

    返回 row 列表（std_code, clause_no, content, has_note, note, is_strong, is_strict, ...）。
    """
    model, embeddings, ids = _ensure_model()
    try:
        load_synonyms()
        load_bm25_index()
        if model is not None:
            load_chapter_embeddings(model)
    except Exception as e:
        print(f"  [stdq] 索引准备告警: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
    return do_search(model, embeddings, ids, query, limit=limit,
                     std_code=std, type_filter=type_filter)

# ===================== daemon 客户端（装了 daemon 就走它，图个快）=====================

def _recv_framed(sock):
    """按 8 字节长度头收完整 JSON（避免 TCP/Unix socket 粘包）。"""
    hdr = b""
    while len(hdr) < 8:
        chunk = sock.recv(8 - len(hdr))
        if not chunk:
            raise ConnectionError("daemon 提前断开")
        hdr += chunk
    n = int.from_bytes(hdr, "big")
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("daemon 提前断开")
        buf += chunk
    return json.loads(buf.decode("utf-8"))


def _send_framed(sock, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(len(body).to_bytes(8, "big") + body)


def search_via_daemon(query, limit=30, std=None, type_filter=None, timeout=30):
    """走常驻进程查询；未运行/超时/协议错 → 返回 None（由调用方回退本地）。"""
    import socket as _socket
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCK_PATH)
            _send_framed(s, {"action": "search", "query": query, "limit": limit,
                             "std": std, "type": type_filter})
            resp = _recv_framed(s)
        return resp.get("results") if resp.get("ok") else None
    except Exception:
        return None


def search_auto(query, limit=30, std=None, type_filter=None, use_daemon=True):
    """CLI 的检索入口：优先走 daemon（快），不可用就本地算（慢，但同一套逻辑、结果一致）。"""
    if use_daemon:
        rows = search_via_daemon(query, limit=limit, std=std, type_filter=type_filter)
        if rows is not None:
            return rows, True
    return search(query, limit=limit, std=std, type_filter=type_filter), False
