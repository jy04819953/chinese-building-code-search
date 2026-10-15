"""测试公共部分：临时数据目录 + 建库夹具（不碰真实数据目录）。"""
import os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "examples", "sample-standards")


def _run(args, data_dir, check=True):
    env = {**os.environ, "STDQ_DATA_DIR": data_dir, "STDQ_SOCK": os.path.join(data_dir, "d.sock"),
           "PYTHONPATH": ROOT}
    r = subprocess.run([sys.executable, "-m", "stdq"] + args, cwd=ROOT,
                       capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} 失败:\n{r.stdout}\n{r.stderr}")
    return r


def point_modules_at(data_dir):
    """把已 import 的模块里的路径常量指向临时数据目录。

    consts 里的路径是 import 时算好的，直接调函数（而非走子进程）时必须重指。
    """
    from stdq import consts, db as dbmod, search as searchmod, catalog as catmod
    dbp = os.path.join(data_dir, "standards.db")
    for mod in (consts, dbmod, searchmod, catmod):
        if hasattr(mod, "STANDARDS_DIR"):
            mod.STANDARDS_DIR = data_dir
        if hasattr(mod, "DB_PATH"):
            mod.DB_PATH = dbp
        if hasattr(mod, "DATA_DIR"):
            mod.DATA_DIR = data_dir
    os.environ["STDQ_DATA_DIR"] = data_dir


class DataDirMixin(unittest.TestCase):
    """每个测试类一份临时数据目录；样例规范拷进去后建库。"""

    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="stdq-test-")
        dst = os.path.join(cls.data, "standards")
        os.makedirs(dst, exist_ok=True)
        for f in sorted(os.listdir(SAMPLES)):
            if f.endswith(".md"):
                shutil.copy(os.path.join(SAMPLES, f), os.path.join(dst, f))
        _run(["rebuild"], cls.data)
        point_modules_at(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.data, ignore_errors=True)

    def run_cli(self, *args, check=True):
        return _run(list(args), self.data, check=check)
