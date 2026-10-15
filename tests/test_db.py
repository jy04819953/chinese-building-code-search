"""建库：条数、幂等、章节目录、空库健壮性。"""
import os, sqlite3, tempfile, unittest

from tests.conftest import DataDirMixin, _run


class TestRebuild(DataDirMixin):
    def _count(self, table):
        c = sqlite3.connect(os.path.join(self.data, "standards.db"))
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            c.close()

    def test_three_sample_books_loaded(self):
        self.assertEqual(self._count("standards"), 3)

    def test_clauses_and_notes_loaded(self):
        self.assertGreater(self._count("clauses"), 80)
        c = sqlite3.connect(os.path.join(self.data, "standards.db"))
        try:
            n = c.execute("SELECT COUNT(*) FROM clauses WHERE note IS NOT NULL AND note != ''").fetchone()[0]
        finally:
            c.close()
        self.assertGreater(n, 0, "条文说明应存在 clauses.note 里")

    def test_chapter_titles_backfilled(self):
        """章节标题是章节扩展词与章节加权的数据源，不能为空。"""
        self.assertGreater(self._count("clause_chapters"), 20)

    def test_rebuild_is_idempotent(self):
        before = (self._count("standards"), self._count("clauses"))
        self.run_cli("rebuild")
        self.assertEqual(before, (self._count("standards"), self._count("clauses")))

    def test_empty_dir_builds_zero(self):
        empty = tempfile.mkdtemp(prefix="stdq-empty-")
        r = _run(["rebuild"], empty)
        self.assertIn("0本规范", r.stdout)

    def test_schema_tables_exist(self):
        c = sqlite3.connect(os.path.join(self.data, "standards.db"))
        try:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        for t in ("standards", "clauses", "clause_chapters", "abolished", "clauses_fts"):
            self.assertIn(t, names)
