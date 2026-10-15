# stdq — 建筑规范条文级检索

把**中文规范 Markdown** 建成条文级可检索库：jieba 分词 + LIKE 召回 + 章节标题扩展词 + BM25 词级融合 + bge 语义排序。
输出带**条文号、强条标记、条文说明**，供人查、也便于接给 LLM 当检索层。

> 仓库内 `examples/sample-standards/` 是三本**完全虚构**的示例规范，用来演示流程；不含任何真实规范原文。

## 它解决什么

| 直接问 LLM | 用 stdq |
|---|---|
| 规范原文几十万字塞不进上下文 | 只把命中的那几条条文取出来 |
| "疏散距离"、"卷材防水层"这类专业词，关键词搜不到 | 同义词表 + 章节标题扩展词 + 语义兜底一起召回 |
| 不知道条文号、不知道哪条是强条 | 结果直接带 `§4.1.3`、`🔴强条`、`📖有说明` |
| 条文说明和正文混在一起 | 正文/说明分表存，可分别取用 |

## 快速开始

```bash
pip install -e .                       # 只装基础（关键词检索，够用）

# 用自带虚构样例建库
export STDQ_DATA_DIR=~/.stdq
mkdir -p "$STDQ_DATA_DIR/standards"
cp examples/sample-standards/*.md "$STDQ_DATA_DIR/standards/"
stdq rebuild                           # 扫 MD → 解析 → 入库

stdq list                              # 看库里有哪些规范
stdq search "疏散安全出口"              # 查
stdq clause SAMPLE-10001-2026 3.1.1    # 取某条原文（含条文说明）
stdq strong SAMPLE-10001-2026          # 只看强条
```

## 让它更快（推荐：起常驻进程）

检索要用语义模型（约 1 GB）。**模型每次现场加载要分钟级**，所以推荐让它常驻：

```bash
pip install -e ".[embed]"     # 语义检索依赖（numpy + sentence-transformers）
stdq embed-build             # 为库里条文生成向量
stdq daemon start -d         # 后台起常驻进程（模型只加载一次）
stdq daemon status           # pid / ready / socket 三态
stdq daemon stop
```

之后的查询就是**亚秒级**。要开机自启就用 `deploy/stdq-daemon.service`（systemd 模板，里面标了要改哪几处）。

**不用常驻进程也能查**，走的时候结果完全一致——召回与排序逻辑都在库里（`stdq/search.py`），常驻进程只负责"别反复加载模型"这一件事。区别只有速度：不起它，每次查询现场加载模型。

> 首次启动后等就绪再查：`stdq daemon status` 显示 `可连接`，或数据目录里出现 `stdq_daemon.ready`。

## 依赖分级

| 装了什么 | 能用到什么 |
|---|---|
| 只装 `jieba` | LIKE 召回 + 词级排序（可用） |
| 再装 `numpy` | 语义兜底召回、章节命中加权 |
| 再装 `sentence-transformers` 并跑过 `embed-build` | 完整：bge 余弦排序 + 章节标题扩展词 + BM25 融合 |

缺哪一层只是**能力弱一点，不会不能用**——缺依赖时会在 stderr 说一句然后降级，不报错退出。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/query-guide.md`](docs/query-guide.md) | **查询手册**：命令速查、七步工作流、检索是怎么排出来的、输出规范、踩坑清单、故障排查 |
| [`docs/ingest-sop.md`](docs/ingest-sop.md) | **入库手册**：从 PDF 到可检索库的逐项清单、MD 格式契约、跳号修复、完成标准 |
| [`stdq/tools/README.md`](stdq/tools/README.md) | 22 个配套工具清单与推荐使用顺序 |
| [`docs/architecture.md`](docs/architecture.md) | 分层、模块职责、数据流、几个设计决定 |
| [`docs/data-and-licensing.md`](docs/data-and-licensing.md) | 不含规范原文的说明、贡献要求、许可 |

## 测试

```bash
python -m unittest discover -s tests -t .     # 27 个用例，约 1 分钟
```

覆盖解析（条文 / 说明 / 目次 / 强条）、建库（条数 / 幂等 / 章节目录 / 空库）、检索（召回 / 过滤 / 降级）、命令行行为、常驻进程起停与回退。全部用 `examples/` 里的虚构样例，不碰你的数据目录。

## 出问题时

`tools/` 下是入库前后用的质检与修复脚本（对账、条文号连续性、OCR 字符体检、各类 MD 修复）。**转换质量决定检索质量**，拿到一批转换稿建议按 `tools/README.md` 里的顺序过一遍。

## 目录

```
stdq/            引擎（parser 解析 / db 建库 / search 检索 / cli 命令 / daemon 常驻）
stdq/tools/      质检与修复脚本（不在日常查询路径）
examples/        三本虚构样例规范
deploy/          systemd 单元模板
tests/           27 个测试用例
```

## 建自己的库（用你手上的规范）

1. **准备 Markdown**：每本规范一个 `.md`。文件名建议 `规范号 规范名称.md`（如 `GB 5xxxx-20xx 某某规范名称.md`），程序据此提取规范号与名称。
2. **条文格式**：`X.Y.Z` 形式的条文号开头，正文跟在后面；条文说明单独成节（`条文说明`），程序会与正文分表关联。
3. **入库**：

```bash
stdq rebuild        # 建库（幂等，可反复跑）
stdq profession set <规范号> <主题>    # 标专业主题（结构/给排水/暖通/电气/节能…）
stdq embed-build    # 语义检索就绪（可选，但推荐）
```

4. **自查**：

```bash
stdq list           # 条数是否合理
stdq info <规范号>  # 单本详情
stdq abolished      # 废止条文（老规范被新版替代的）
```

**转换质量决定检索质量**。若你的 MD 来自 PDF 转换，建议先检查：条文号是否连续、有没有被表格切断、章节标题是否被误当条文。见下面「出问题时」一节。

## 命令一览

| 命令 | 作用 |
|---|---|
| `rebuild` | 建库（扫 `$STDQ_DATA_DIR/standards/*.md`） |
| `list` / `info <号>` | 在库清单 / 单本详情 |
| `search <词>` | 检索（默认走常驻进程，没有就本地算） |
| `clause <号> <条文号>` | 取条文原文（`--no-note` 去说明） |
| `strong [号]` | 强条清单 |
| `term <号> [关键词]` | 术语查询 |
| `appendix <号>` | 附录条目 |
| `catalog` | 重新生成在库清单 |
| `term <号> <术语>` | 术语查询 |
| `appendix <号>` | 附录 |
| `catalog` | 生成在库清单 Markdown |
| `abolished` | 废止条文 |
| `profession set/list` | 专业主题标注 |
| `embed-build` | 生成条文向量 |
| `daemon start/stop/status` | 常驻进程（推荐） |

## 常见问题

**Q：必须装常驻进程吗？**
不必须。装了快（亚秒），不装慢（每次现场加载模型），**结果一致**。

**Q：语义依赖装不上 / 装完跑不动？**
不装也能查——LIKE 召回 + BM25 排序照常工作，只是没有语义排序和章节加权。

**Q：我可以把规范原文放进这个仓库吗？**
别。规范文本有著作权，本仓库只提供**引擎**与**虚构样例**。你自己的规范放在自己的数据目录里。

**Q：换了机器，库要重建吗？**
要重新 `rebuild`（SQLite 单文件可直接拷，但向量文件与解释器/模型版本相关，建议重建后重新 `embed-build`）。

## 许可

Apache-2.0，见 `LICENSE` 与 `NOTICE`。
