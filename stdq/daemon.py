"""常驻检索进程：把语义模型留在内存里，让查询从首次 1-3 分钟变成 0.7 秒。

只做两件事：
  1. 启动时加载一次模型与索引（常驻）
  2. 通过 Unix domain socket 接收查询、返回结果

与检索质量无关——召回与排序逻辑都在 `search.py` 里，这里只是"让模型别反复加载"。
不用它也能查（每次现场加载，慢但结果一样）。推荐装它：见 README 的「让它更快」一节。

用法：
    python -m stdq daemon start     # 前台起（可交给 systemd）
    python -m stdq daemon start -d  # 后台起（fork + setsid，无 systemd 时用）
    python -m stdq daemon status / stop
"""
from __future__ import annotations
import os, sys, json, time, signal, socket, subprocess

from .consts import SOCK_PATH, STANDARDS_DIR
from . import search as S

PID_FILE = os.path.join(STANDARDS_DIR, "stdq_daemon.pid")
READY_FILE = os.path.join(STANDARDS_DIR, "stdq_daemon.ready")
LOG_FILE = os.path.join(STANDARDS_DIR, "stdq_daemon.log")


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _send_framed(sock, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(len(body).to_bytes(8, "big") + body)


def _recv_framed(sock):
    hdr = b""
    while len(hdr) < 8:
        chunk = sock.recv(8 - len(hdr))
        if not chunk:
            raise ConnectionError("peer closed")
        hdr += chunk
    n = int.from_bytes(hdr, "big")
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return json.loads(buf.decode("utf-8"))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_start(args):
    """启动常驻进程：加载模型 → 写 ready → 监听 socket。"""
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            if _pid_alive(pid):
                print(f"已在运行（pid {pid}）。如需重启：stdq daemon stop && stdq daemon start")
                return 0
        except Exception:
            pass
        os.remove(PID_FILE)

    if getattr(args, "daemonize", False):
        pid = os.fork()
        if pid > 0:
            print(f"已后台启动（pid {pid}）。就绪判定：等 {READY_FILE} 出现，或用 `stdq daemon status`。")
            return 0
        os.setsid()
        # 脱离调用方的 stdout/stderr：否则父进程的管道一直开着，调用方会一直等
        sys.stdout.flush(); sys.stderr.flush()
        _devnull = os.open(os.devnull, os.O_RDONLY)
        _logfd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(_devnull, 0); os.dup2(_logfd, 1); os.dup2(_logfd, 2)
        os.close(_devnull); os.close(_logfd)

    for f in (READY_FILE,):
        if os.path.exists(f):
            os.remove(f)
    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)

    _log("加载模型与索引…")
    t0 = time.time()
    model, embeddings, ids = S._ensure_model()
    try:
        S.load_synonyms(); S.load_bm25_index()
        if model is not None:
            S.load_chapter_embeddings(model)
    except Exception as e:
        _log(f"索引准备告警（不影响关键词检索）: {type(e).__name__}: {e}")
    _log(f"就绪，用时 {time.time() - t0:.1f}s"
         + ("" if model is not None else "（语义检索不可用，仅关键词 + BM25）"))

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(16)
    with open(READY_FILE, "w") as f:
        f.write(str(os.getpid()))

    def _bye(signum, frame):
        _log("收到停止信号，退出")
        try:
            srv.close()
            os.remove(SOCK_PATH)
            os.remove(READY_FILE)
            os.remove(PID_FILE)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    _log(f"监听 {SOCK_PATH}")
    while True:                      # 单线程顺序处理：不同时算查询，避免争抢内存
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        with conn:
            try:
                req = _recv_framed(conn)
                if req.get("action") == "search":
                    rows, _ = S.search_auto(req.get("query", ""), limit=req.get("limit", 30),
                                            std=req.get("std"), type_filter=req.get("type"),
                                            use_daemon=False)
                    _send_framed(conn, {"ok": True, "results": rows})
                else:
                    _send_framed(conn, {"ok": False, "error": f"未知 action: {req.get('action')}"})
            except Exception as e:
                try:
                    _send_framed(conn, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass
    return 0


def cmd_stop(args):
    if not os.path.exists(PID_FILE):
        print("未运行（无 pid 文件）")
        return 0
    pid = int(open(PID_FILE).read().strip())
    if not _pid_alive(pid):
        print(f"pid {pid} 已不在，清理残留文件")
    else:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        else:
            os.kill(pid, signal.SIGKILL)     # 兜底强杀
            print("已强制停止")
    for f in (PID_FILE, READY_FILE, SOCK_PATH):
        if os.path.exists(f):
            os.remove(f)
    print("已停止")
    return 0


def cmd_status(args):
    pid = int(open(PID_FILE).read().strip()) if os.path.exists(PID_FILE) else None
    alive = bool(pid and _pid_alive(pid))
    ready = os.path.exists(READY_FILE)
    sock_ok = False
    if os.path.exists(SOCK_PATH):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2); s.connect(SOCK_PATH); sock_ok = True
        except Exception:
            pass
    print(f"pid:    {pid if pid else '—'}  ({'存活' if alive else '未运行'})")
    print(f"ready:  {'已就绪' if ready else '未就绪'}   socket: {'可连接' if sock_ok else '不可用'}")
    print(f"log:    {LOG_FILE}")
    return 0
