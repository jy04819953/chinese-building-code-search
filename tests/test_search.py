"""检索：召回、过滤、强条、单条取值；无向量时也要能用（BM25 降级）。"""
import unittest

from stdq.search import search
from tests.conftest import DataDirMixin


class TestSearch(DataDirMixin):
    def test_keyword_recall(self):
        rows = search("疏散安全出口", limit=5)
        self.assertTrue(rows, "应召回结果")
        self.assertTrue(any("3.1.1" == r[1] for r in rows), f"§3.1.1 应在结果里，实际 {[r[1] for r in rows]}")

    def test_result_shape(self):
        rows = search("安全出口", limit=3)
        for r in rows:
            self.assertGreaterEqual(len(r), 7, "结果应含 规范号/条文号/正文/说明标记/说明/强条/严格 七列")
            self.assertTrue(r[0].startswith("SAMPLE-"), "规范号来自样例库")

    def test_std_filter(self):
        rows = search("防火", limit=20, std="SAMPLE-D-30001-2026")
        for r in rows:
            self.assertIn("SAMPLE-D-30001-2026", r[0])

    def test_no_match_returns_empty(self):
        self.assertEqual(search("完全不存在的不相关词汇xyzzy"), [])

    def test_limit_respected(self):
        self.assertLessEqual(len(search("演示", limit=2)), 2)
