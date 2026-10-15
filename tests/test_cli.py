"""命令行行为：子命令能跑、输出可判读。"""
import unittest

from tests.conftest import DataDirMixin


class TestCli(DataDirMixin):
    def test_list(self):
        out = self.run_cli("list").stdout
        self.assertIn("SAMPLE-10001-2026", out)
        self.assertIn("强条", out)

    def test_clause_returns_text(self):
        out = self.run_cli("clause", "SAMPLE-10001-2026", "3.1.1").stdout
        self.assertIn("安全出口", out)

    def test_strong(self):
        out = self.run_cli("strong", "SAMPLE-10001-2026").stdout
        self.assertIn("3.1.1", out)

    def test_info(self):
        self.assertIn("SAMPLE-10001-2026", self.run_cli("info", "SAMPLE-10001-2026").stdout)

    def test_missing_clause_reports_clearly(self):
        out = self.run_cli("clause", "SAMPLE-10001-2026", "99.99.99").stdout
        self.assertIn("未找到", out)

    def test_help_works(self):
        self.assertIn("usage", self.run_cli("--help").stdout.lower())

    def test_search_without_daemon_still_works(self):
        """没起常驻进程时，检索应回退本地并给出提示（而不是失败）。"""
        out = self.run_cli("search", "疏散安全出口", "--limit", "3").stdout
        self.assertTrue("SAMPLE-10001-2026" in out or "规范条文" in out)
