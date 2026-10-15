# stdq — clause-level search for Chinese building codes

Turn **Chinese building-code Markdown** into a clause-level searchable index:
jieba tokenisation + LIKE recall + chapter-title query expansion + BM25 fusion + bge semantic ranking.

Every hit carries its **clause number, mandatory-clause flag (强条) and explanatory note** — readable by people, and usable as a retrieval layer under an LLM.

> `examples/sample-standards/` contains three **entirely fictional** sample standards used to demonstrate the pipeline. No real code text ships with this repository.

## What it solves

| Asking an LLM directly | With stdq |
|---|---|
| A code is hundreds of thousands of characters — it will not fit in context | Only the matching clauses are retrieved |
| Domain terms like「卷材防水层」are missed by naive keyword search | Synonym table + chapter-title expansion + semantic fallback recall them |
| You don't know the clause number, or which clauses are mandatory | Results carry `§4.1.3`, `🔴mandatory`, `📖has note` |
| Clause text and its explanatory notes are tangled together | Stored separately, retrieved separately |

## Quick start

```bash
pip install -e .                       # base install: keyword search only

export STDQ_DATA_DIR=~/.stdq
mkdir -p "$STDQ_DATA_DIR/standards"
cp examples/sample-standards/*.md "$STDQ_DATA_DIR/standards/"
stdq rebuild                           # scan MD → parse → load into SQLite

stdq list                              # what is in the index
stdq search "疏散安全出口"              # search
stdq clause SAMPLE-10001-2026 3.1.1    # fetch one clause (with its note)
stdq strong SAMPLE-10001-2026          # mandatory clauses only
```

Queries are usually Chinese (that is the corpus language); the engine, flags and output format are language-agnostic.

## Make it fast (recommended: run the daemon)

Searching needs an embedding model (~1 GB). **Loading it on every query takes minutes**, so keep it resident:

```bash
pip install -e ".[embed]"     # numpy + sentence-transformers
stdq embed-build             # compute clause embeddings
stdq daemon start -d         # start resident process (model loaded once)
stdq daemon status           # pid / ready / socket
stdq daemon stop
```

After that queries are **sub-second**. For boot persistence use `deploy/stdq-daemon.service` (a systemd template with the three places to edit marked).

**The daemon is optional.** Results are identical either way — all recall and ranking logic lives in the library (`stdq/search.py`); the daemon only keeps the model in memory. Without it, each query loads the model first.

> After starting, wait until `stdq daemon status` reports the socket connectable (or `stdq_daemon.ready` appears in the data directory) before querying.

## Dependency tiers

| Installed | You get |
|---|---|
| `jieba` only | LIKE recall + lexical ranking — usable |
| `+ numpy` | semantic fallback recall, chapter-hit boost |
| `+ sentence-transformers` and a completed `embed-build` | full: bge cosine ranking, chapter-title expansion, BM25 fusion |

Missing a tier makes the engine weaker, never unusable — it prints one line to stderr and degrades instead of failing.

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
| `term <code> <term>` | Term lookup |
| `appendix <code>` | Appendices |
| `catalog` | Write the index inventory as Markdown |
| `abolished` | Superseded clauses |
| `profession set/list` | Discipline tagging |
| `embed-build` | Compute clause embeddings |
| `daemon start/stop/status` | Resident process (recommended) |

## FAQ

**Do I have to run the daemon?**
No. With it, queries are sub-second; without it, each query loads the model first. **Results are the same.**

**The semantic dependencies won't install / don't work?**
The engine still runs — LIKE recall and BM25 ranking work without them; you only lose semantic ranking and the chapter boost.

**Can I commit code text into this repository?**
Don't. Code text is copyrighted; this repository ships the **engine** and **fictional samples** only. Keep your own codes in your own data directory.

**Moving to another machine — do I rebuild?**
Yes. The SQLite file is portable, but the embedding file depends on the interpreter and model version, so rebuild and re-run `embed-build`.

## License

Apache-2.0 — see `LICENSE` and `NOTICE`.
