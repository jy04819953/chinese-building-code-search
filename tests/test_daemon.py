"""常驻进程：起停、状态三态、走它查询、停后自动回退本地。"""
import os, time, unittest

from tests.conftest import DataDirMixin, _run


class TestDaemon(DataDirMixin):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.started = False
        r = _run(["daemon", "start", "-d"], cls.data, check=False)
        if "已后台启动" in (r.stdout + r.stderr):
            cls.started = True
            ready = os.path.join(cls.data, "stdq_daemon.ready")
            for _ in range(40):                 # 首次要加载模型，给足时间
                if os.path.exists(ready):
                    break
                time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        if cls.started:
            _run(["daemon", "stop"], cls.data, check=False)
        super().tearDownClass()

    def test_status_reports_socket(self):
        out = self.run_cli("daemon", "status").stdout
        self.assertIn("pid:", out)
        self.assertIn("socket:", out)

    def test_query_works_while_running(self):
        out = self.run_cli("search", "疏散安全出口", "--limit", "3").stdout
        self.assertIn("SAMPLE-10001-2026", out)
        self.assertNotIn("daemon 未运行", out, "进程在跑时不应走本地回退")

    def test_stop_then_local_fallback(self):
        if not self.started:
            self.skipTest("常驻进程未启动")
        _run(["daemon", "stop"], self.data, check=False)
        r = self.run_cli("search", "疏散安全出口", "--limit", "3")
        out = r.stdout + r.stderr          # 回退提示走 stderr，两处都看
        self.assertIn("SAMPLE-10001-2026", out, "停掉后本地计算也应给出同样的结果")
        self.assertIn("daemon 未运行", out, "应明确提示已回退本地")
