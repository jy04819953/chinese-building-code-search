# tools/ — 配套工具

按需运行的运维/质检脚本，不属于日常查询路径。统一用法：`python -m stdq.tools.<名字> [参数]`。带 argparse 的可加 `--help` 看用法；
少数只接文件路径的脚本（`detect_clause_drop` / `fix_concat_headers` / `mineru_postprocess` / `postprocess_md`）**直接读源码顶部的用法说明**——给它们 `--help` 会被当成文件路径。

> 先预演、看清差异、再写盘：改 MD 的脚本默认 dry-run，只有 `--apply` 才落盘；落盘前自动备份。

## 入库前排查

| 工具 | 作用 | 常用参数 |
|---|---|---|
| `detect_clause_drop` | 入库前跑一次，列出可能被 stdq rebuild 吞掉的条文号 | — |

## 入库后对账 / 体检

| 工具 | 作用 | 常用参数 |
|---|---|---|
| `verify_md_vs_db` | 入库对账：把 MD 重解析结果和库里逐条比，揪出漏抽/截断/多余 | `--json --limit --postprocess --quiet` |
| `stdq_check` | stdq check — 条文号连续性检查（带跳号自动分类） | `--json --no-classify --only-real` |
| `apx_crosscheck` | 全库附录抽取稳定性交叉验证： | — |
| `scan_ocr_anomalies` | OCR 字符体检（入库硬闸门）——扫中文规范 MD 与库内文本里的假名 + 表头/符号变体。 | — |

## MD 修复（先预演，加 --apply 才写盘）

| 工具 | 作用 | 常用参数 |
|---|---|---|
| `fix_truncated_clauses` | 全库修复"空行断句"截断条文（共性 bug）。 | — |
| `fix_glued_clause_lines` | 「条号粘在上一句末尾 → 整条被漏掉」的 MD 拆行修复（先验证后写盘）。 | — |
| `md_join_split_clauses` | 把 MD 里"被拆成多行"的条文正文并回一行（治 rebuild 会退化的根） | `--apply --min-gap --std` |
| `fix_concat_headers` | 修复MD文件中章节标题和条文号粘连的问题 | — |
| `heading_glue_fix` | 把「下一章标题 ## X 章名 被粘进上一条条文正文」修掉 | `--apply` |
| `fix_table_swallow` | 修「表被吞」：MD 里表号被拆行（"…应符合表" + "5.4.1的规定。"）， | — |
| `trim_doc_tail` | 剪掉条文内容里粘着的「标准尾部整块」（用词说明 / 引用标准名录 / 参考文献）。 | — |
| `apply_glued_fix` | 配合 fix_glued_clause_lines.py：「条号粘在上一句末尾」修完 MD 后，把库与向量同步过去。 | — |
| `embed_patch` | 定点刷新语义向量（不跑全量 embed-build） | `--clauses --dry-run --ids --std` |

## 附录补库 / 章节目录

| 工具 | 作用 | 常用参数 |
|---|---|---|
| `backfill_appendix` | 附录条目「定点补库」：只 INSERT 新增的附录条目，不动现有条文、不 rebuild。 | — |
| `backfill_apx_sections` | 补附录「节级」(X.N，如 D.1 一般规定) 到章节目录 clause_chapters，父 = 附录X。 | — |
| `splice_appendix` | 把"缺的附录"从局部转换出来的 MD 拼回规范主 MD。 | — |
| `backfill_chapters` | 一次性回填脚本:给 standards.db 里所有已有规范补全 clause_chapters 表。 | `--apply --dry-run --fix-orphans --purge` |

## 转换后处理（PDF→MD 之后）

| 工具 | 作用 | 常用参数 |
|---|---|---|
| `mineru_postprocess` | MinerU 转换后的 MD 后处理脚本 | — |
| `postprocess_md` | RapidDoc MD 后处理脚本 | — |

## 修复影响面复核

| 工具 | 作用 | 常用参数 |
|---|---|---|
| `review_classify` | 对 heading_glue_fix.py 的 review 清单做安全定性： | — |
| `three_way_heading_check` | 三向对比定位「标题粘连」修复的影响面：旧解析 vs 新解析 vs 库 | — |
