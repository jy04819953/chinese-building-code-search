# stdq — clause-level search for Chinese building codes

Turn **Chinese building-code Markdown** into a clause-level searchable index:
jieba tokenisation + LIKE recall + chapter-title query expansion + BM25 fusion + bge semantic ranking.

Every hit carries its **clause number, mandatory-clause flag (强条) and explanatory note** — readable by people, and usable as a retrieval layer under an LLM.

> `examples/sample-standards/` contains three **entirely fictional** sample standards used to demonstrate the pipeline. No real code text ships with this repository.

## What it solves

### Querying

| Asking an LLM directly | With stdq |
|---|---|
| A code is hundreds of thousands of characters — it will not fit in context | Only the matching clauses are retrieved |
| Domain terms like「卷材防水层」are missed by naive keyword search | Synonym table + chapter-title expansion + semantic fallback recall them |
| You don't know the clause number, or which clauses are mandatory | Results carry `§4.1.3`, `🔴mandatory`, `📖has note` |
| Clause text and its explanatory notes are tangled together | Stored separately, retrieved separately |

### Ingest — where the real difficulty is

Querying is largely solved (vector stores, full-text search and RAG frameworks all do it). **The hard part is turning Chinese building codes into a *correct* clause index** — conversion quality determines retrieval quality, and if ingest is wrong, no amount of search sophistication will find the clause.

Everything below was hit for real and has a tool behind it (see `stdq/tools/`):

| Common defect in converted files | If ignored | What this project does |
|---|---|---|
| Clause number glued to the end of the previous sentence | The whole clause is dropped; its text is counted into the previous one | Glue detection → line-split repair → DB + embedding sync (`fix_glued_clause_lines` → `apply_glued_fix`) |
| Clause number followed directly by numeric text (`11.1.8250m…`) | Parsed as a longer number → that clause number disappears | Gap check with automatic classification, act only on true misses (`stdq_check`) |
| Table caption split across lines | The continuation is read as a new clause → the whole table **and** the clause tail are lost | Table-swallow detection and repair (`fix_table_swallow`) |
| Clause split by blank lines | The build stops at the blank line → only the first half is stored | Truncated-clause repair (`fix_truncated_clauses`), with backup and post-check |
| Next chapter heading glued into the previous clause | Headings inside clause text, inflated counts | Heading-glue repair + three-way impact check (`heading_glue_fix` / `three_way_heading_check`) |
| Five-level clause numbers truncated to four | Numbers **overwrite each other**; totals look fine while text vanishes | Clause-by-clause reconciliation between Markdown and DB (`verify_md_vs_db`) |
| OCR confuses look-alike characters (Japanese kana inside Chinese codes) | Garbage lands in clause text **and chapter titles**, the latter feeding search ranking | OCR character screening as a **hard gate** (`scan_ocr_anomalies`), scanning both Markdown and DB |
| Terminology standards have a completely different shape | Parsed as normal codes → clause count collapses | Decide by **filename** + a dedicated extraction path (7 format variants) |
| Appendices live in their own region with letter numbering | Appendix entries are unsearchable | Appendix extraction + cross-check + back-fill without a rebuild |
| Image references no longer match the files (extension changed, folder dropped) | Every reference dangles — **while clause parsing still looks perfectly fine** | Reference count vs actual file count comparison |
| Supersession ("this edition is repealed") is natural language | The old edition is never hidden | Announcement-pattern parsing into the `abolished` table, plus a documented manual fallback |
| One file holds clause text, notes, tables, terms, appendices | Flattened into one field → notes can't be separated from text | Separate tables for clauses / notes / chapter titles |

None of these are theoretical: each one has a failure behind it, and `docs/ingest-sop.md` is the operational checklist written from them.

## Quick start

```bash
pip install -e ".[embed]"              # base + semantic deps (keyword-only: pip install -e .)

export STDQ_DATA_DIR=~/.stdq
mkdir -p "$STDQ_DATA_DIR/standards"
cp examples/sample-standards/*.md "$STDQ_DATA_DIR/standards/"

stdq rebuild                           # build the index from your Markdown
stdq embed-build                       # compute clause embeddings
stdq daemon start -d                   # start the resident process — you need this, see below

stdq list                              # what is in the index
stdq search "疏散安全出口"              # search
stdq clause SAMPLE-10001-2026 3.1.1    # fetch one clause (with its note)
stdq strong SAMPLE-10001-2026          # mandatory clauses only
```

## The resident process (you need to run it)

Searching needs an embedding model (~1 GB). **This setup needs a resident process**: the model is loaded once and every later query just computes.

It works without it, but then **every query reloads the model from scratch** — far, far slower:

| Single query | Time (measured on the 96-clause sample index) |
|---|---|
| Without the resident process | **13.3 s** (model reloaded each time) |
| With it | **0.15 s** |

(The very first load takes minutes depending on the machine; the figures above are with the model already in the OS page cache.)

So the cost of not running it is not "can I use it" but **waiting on every single query** — worse the bigger your index and the more often you query. "Type a command, get the answer" requires the resident process.

```bash
stdq daemon start -d         # start (model loaded once)
stdq daemon status           # pid / ready / socket
stdq daemon stop
```

For boot persistence use `deploy/stdq-daemon.service` (a systemd template with the three places to edit marked).

> **Retrieval quality is independent of it**: all recall and ranking logic lives in the library (`stdq/search.py`) and both paths call the same function, so results are identical. It fixes **speed** — and the speed difference is large (table above).

## Dependency tiers

| Installed | You get |
|---|---|
| `jieba` only | LIKE recall + lexical ranking — usable |
| `+ numpy` | semantic fallback recall, chapter-hit boost |
| `+ sentence-transformers` and a completed `embed-build` | full: bge cosine ranking, chapter-title expansion, BM25 fusion |

Missing a tier makes the engine weaker, never unusable — it prints one line to stderr and degrades instead of failing.

## Documentation

| Document | Contents |
|---|---|
| [`docs/query-guide.md`](docs/query-guide.md) | **Query guide**: commands, the seven-step workflow, how ranking works, output conventions, pitfalls, troubleshooting |
| [`docs/ingest-sop.md`](docs/ingest-sop.md) | **Ingest manual**: PDF → searchable index, checklist, Markdown format contract, gap repair, completion criteria |
| [`stdq/tools/README.md`](stdq/tools/README.md) | The 22 bundled tools and the order to run them in |
| [`docs/architecture.md`](docs/architecture.md) | Layers, module responsibilities, data flow, design decisions |
| [`docs/data-and-licensing.md`](docs/data-and-licensing.md) | No code text included, contribution rules, license |

## Tests

```bash
python -m unittest discover -s tests -t .     # 27 cases, ~1 minute
```

Covers parsing (clauses / notes / table of contents / mandatory flags), indexing (counts, idempotency, chapter titles, empty directory), search (recall, filtering, degraded mode), CLI behaviour, and daemon start / stop / fallback. Everything runs against the fictional samples in `examples/`.

## When something is off

`tools/` holds pre- and post-ingest QA and repair scripts (reconciliation, clause-number continuity, OCR character screening, several Markdown repairs). Run them in the order described in `tools/README.md`.

## Layout

```
stdq/            engine (parser / db / search / cli / daemon)
stdq/tools/      QA and repair scripts (not on the query path)
examples/        three fictional sample standards
deploy/          systemd unit template
tests/           27 test cases
```

## Build your own index

1. **Markdown per code**: one `.md` per standard. Name it `CODE Name.md` (e.g. `GB 5xxxx-20xx SomeCodeName.md`); the code and name are parsed from the filename.
2. **Shape of the text**: clauses start with a clause number of the form `X.Y.Z`; explanatory notes live in their own section (`条文说明`) and are linked separately.
3. **Load it**:

```bash
stdq rebuild                            # idempotent, safe to re-run
stdq profession set <code> <theme>      # tag the discipline (structure / hvac / electrical / …)
stdq embed-build                        # enables semantic ranking (optional but recommended)
```

4. **Check it**:

```bash
stdq list           # are the counts plausible?
stdq info <code>    # per-code detail
stdq abolished      # clauses superseded by newer editions
```

**Conversion quality determines retrieval quality.** If your Markdown came from PDF conversion, verify that clause numbers are continuous, that nothing was swallowed by tables, and that chapter headings were not parsed as clauses.

## Commands

| Command | Purpose |
|---|---|
| `rebuild` | Build the index from `$STDQ_DATA_DIR/standards/*.md` |
| `list` / `info <code>` | Index inventory / per-code detail |
| `search <query>` | Search (uses the daemon when running, local otherwise) |
| `clause <code> <no>` | Fetch one clause (add `--no-note` to drop the note) |
| `strong [code]` | Mandatory clauses |
| `term <code> [kw]` | Term lookup |
| `appendix <code>` | Appendix entries |
| `catalog` | Regenerate the index inventory |
| `term <code> <term>` | Term lookup |
| `appendix <code>` | Appendices |
| `catalog` | Write the index inventory as Markdown |
| `abolished` | Superseded clauses |
| `profession set/list` | Discipline tagging |
| `embed-build` | Compute clause embeddings |
| `daemon start/stop/status` | Resident process (recommended) |

## FAQ

**Can I skip the resident process?**
Yes, but every query then reloads the embedding model — **far, far slower** (measured: 13.3 s vs 0.15 s). For day-to-day use, running it is the standard setup: start it right after installing.

**The semantic dependencies won't install / don't work?**
The engine still runs — LIKE recall and BM25 ranking work without them; you only lose semantic ranking and the chapter boost.

**Can I commit code text into this repository?**
Don't. Code text is copyrighted; this repository ships the **engine** and **fictional samples** only. Keep your own codes in your own data directory.

**Moving to another machine — do I rebuild?**
Yes. The SQLite file is portable, but the embedding file depends on the interpreter and model version, so rebuild and re-run `embed-build`.

## License

Apache-2.0 — see `LICENSE` and `NOTICE`.
