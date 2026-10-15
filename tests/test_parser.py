"""解析层：条文 / 条文说明 / 术语 / 目次 / 强条标记。"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stdq.parser.clauses import extract_clauses          # noqa: E402
from stdq.parser.toc import parse_toc                    # noqa: E402
from stdq.parser.notes import extract_notes              # noqa: E402
from tests.conftest import SAMPLES                       # noqa: E402


def _sample(name="SAMPLE-10001-2026 示例建筑设计通用规范.md"):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as f:
        return f.read()


class TestClauses(unittest.TestCase):
    def test_extracts_numbered_clauses(self):
        got = dict(extract_clauses(_sample(), "SAMPLE-10001-2026 示例建筑设计通用规范.md"))
        self.assertIn("3.1.1", got, "应抽到 §3.1.1")
        self.assertIn("安全出口", got["3.1.1"], "条文正文应包含关键词")

    def test_clause_numbers_are_well_formed(self):
        got = dict(extract_clauses(_sample(), "SAMPLE-10001-2026 示例建筑设计通用规范.md"))
        for no in got:
            self.assertRegex(no, r"^(\d+(\.\d+)+|[A-Z]\.\d+(\.\d+)*|附录[A-Z]?)$", f"条文号格式异常: {no}")

    def test_no_duplicate_clause_numbers(self):
        items = list(extract_clauses(_sample(), "SAMPLE-10001-2026 示例建筑设计通用规范.md"))
        nos = [n for n, _ in items]
        self.assertEqual(len(nos), len(set(nos)), "条文号不应重复")


class TestToc(unittest.TestCase):
    def test_parses_chapter_entries(self):
        entries = parse_toc(_sample())
        self.assertTrue(entries, "样例含目次段，应抽出章节条目")
        nos = {e["clause_no"] for e in entries}
        self.assertIn("3.1", nos, "应抽到节级 3.1")
        for e in entries:
            self.assertIn(e["level"], ("chapter", "section", "appendix"))
            self.assertTrue(e["title"].strip(), f"{e['clause_no']} 标题不应为空")

    def test_no_toc_returns_empty(self):
        self.assertEqual(parse_toc("# 没有目次的文件\n\n正文"), [])


class TestNotes(unittest.TestCase):
    def test_notes_extracted(self):
        notes = extract_notes(_sample())
        self.assertTrue(notes, "样例含条文说明，应抽到")
        keys = [n[0] for n in notes]
        self.assertTrue(any(str(k).startswith("3.1") for k in keys), f"应含 3.1.x 说明，实际 {keys[:6]}")
