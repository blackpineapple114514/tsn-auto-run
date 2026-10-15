# -*- coding: utf-8 -*-
"""app.py —— 一键启动：自带 mock + 路线工作台 + 桌面窗口。

一个进程里跑三样东西：
  ① `mock_boxkj.py` 的 HTTP mock（默认 127.0.0.1:18080）——**不连真实服务器**；
  ② `route_studio/server.py` 的工作台（默认 127.0.0.1:8770），并告诉它 mock 在哪；
  ③ pywebview 桌面窗口打开工作台页面（没装 pywebview 就退回系统浏览器）。

用法
----
  python app.py                 # 起 mock + 工作台 + 窗口
  python app.py --no-window     # 只起服务（自测 / 无桌面环境）
  python app.py --self-test     # 起服务后自己打一遍 /api/flow/run，打印每步回执后退出

数据目录
--------
  裸跑：就用项目目录（`30-logs/`、`tools/route_studio/routes/`）。
  打包后（frozen）：默认放 exe 旁边 `route-studio-data/`，写不进去才退回
  `%LOCALAPPDATA%\\route_studio`；可用 `--data-dir` 指定。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for p in (HERE, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------- 数据目录 / 端口
def pick_data_dir(explicit=None) -> str:
    """冻结后返回可写的数据目录（exe 旁边优先，其次 LOCALAPPDATA）。"""
    if not FROZEN:
        return TOOLS
    if explicit:
        os.makedirs(explicit, exist_ok=True)
        return explicit
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for cand in (os.path.join(exe_dir, "route-studio-data"),
                 os.path.join(os.environ.get("LOCALAPPDATA") or exe_dir, "route_studio")):
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".w")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return cand
        except Exception:                                    # noqa: BLE001
            continue
    return os.getcwd()


def resource_dir() -> str:
    """静态资源根：打包后在 `_MEIPASS`（解包临时目录），裸跑就在 route_studio 目录。"""
    return getattr(sys, "_MEIPASS", None) or HERE


def free_port(preferred: int) -> int:
    """优先用 preferred；被占了就退回系统分配（0）。

    ⚠️ 探针**不能**开 SO_REUSEADDR：Windows 上那会让「已被别人占用的端口」也 bind 成功，
    于是探针以为空闲、真服务器也 bind 成功，结果连上去的还是旧进程（表症：404）。
    ⚠️ 系统分配也可能落到**被保留/排除的端口段**（本机就有 50000–50059，由 Hyper-V/WSL/
    策略保留）—— 此时 bind 直接抛 PermissionError(10013)。所以**必须多重试几次**，
    绝不把 0 原样交出去：调用方拿 0 再绑一次同样会撞排除段，表症就是「双击没反应、
    窗口都出不来」（launcher.log 里就是一条 10013 的 traceback）。
    """
    for p in (preferred,) + (0,) * 16:
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            if port:
                return port
        except OSError:
            continue
    return preferred or 0


def bind_server(cls, handler, host: str, preferred: int):
    """构造一个 HTTPServer，返回 (server, port)。

    端口被占（10048）/撞系统排除段（10013）都换端口重试，避免「双击启动即崩、窗口都出不来」。
    首次试 preferred（探针已确认可独占绑定），其后一律走系统分配。
    """
    err = None
    for attempt in range(20):
        port = free_port(preferred if attempt == 0 else 0)
        try:
            srv = cls((host, port), handler)
            return srv, srv.server_address[1]
        except OSError as e:                                   # noqa: PERF203
            err = e
            continue
    raise RuntimeError("绑不到可用端口：%s" % err)


class _LogWriter:
    """没有控制台时的 stdout/stderr 落点：懒打开 `<数据目录>/30-logs/launcher.log`。

    ⚠️ PyInstaller `console=False` 的构建里 `sys.stdout is None`，此时 `print()` 会直接抛
    `AttributeError: 'NoneType' object has no attribute 'write'` —— 消息全丢、还可能把启动打断。
    """

    def __init__(self, path):
        self.path = path
        self._fh = None

    def _open(self):
        if self._fh is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        return self._fh

    def write(self, s):
        try:
            self._open().write(s)
        except Exception:                                    # noqa: BLE001
            pass
        return len(s)

    def flush(self):
        try:
            if self._fh:
                self._fh.flush()
        except Exception:                                    # noqa: BLE001
            pass

    def isatty(self):
        return False


def ensure_stdio() -> str:
    """windowed 构建（`console=False`）里把 stdout/stderr 接回去 —— 否则 `print()` 会直接抛异常。

    ① 先试 `AttachConsole(-1)`：从 cmd/pwsh 启动时能直接看到输出（`--self-test` 照旧）；
    ② 双击启动（没有父控制台）就写 `<数据目录>/30-logs/launcher.log`（**不弹黑框**）。
    返回 `"keep"`（本来就有标准输出，比如裸跑 python）/ `"console"` / `"log"`。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return "keep"
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):          # ATTACH_PARENT_PROCESS
            f = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stdout, sys.stderr = f, f
            return "console"
    except Exception:                                        # noqa: BLE001
        pass
    w = _LogWriter(os.path.join(pick_data_dir(None), "30-logs", "launcher.log"))
    sys.stdout = sys.stderr = w
    return "log"


def _append_launch_log(info) -> str:
    """把启动信息追加到 `<数据目录>/30-logs/launcher.log`。

    双击启动时没有控制台，控制台里那几行地址就没人看得见 —— 所以**不管有没有控制台都留一份**。
    （踩过的坑：windowed 构建里 `sys.stdout` 实测**不是 None**，拿它当「有没有控制台」的判据会一直判错。）
    """
    path = os.path.join(info["logdir"], "launcher.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s]\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            f.write("  自带 mock ：%s\n" % info["mock_base"])
            f.write("  工作台    ：%s\n" % info["ws_base"])
            f.write("  数据目录  ：%s\n" % info["data_dir"])
    except Exception:                                        # noqa: BLE001
        pass
    return path


def serve(server) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 起 mock + 工作台
def start_all(*, mock_port=18080, ws_port=8770, data_dir=None, verbose=False):
    data_dir = pick_data_dir(data_dir)
    logdir = os.path.join(data_dir, "30-logs")
    routes_dir = os.path.join(data_dir, "routes")
    os.makedirs(logdir, exist_ok=True)
    os.makedirs(routes_dir, exist_ok=True)
    # 让两个模块把数据写到数据目录（见 s72 加的两个环境变量）
    os.environ["MOCK_LOGDIR"] = logdir
    os.environ["ROUTE_STUDIO_ROUTES"] = routes_dir

    import mock_boxkj as M
    import server as S

    S.STATIC = os.path.join(resource_dir(), "static")   # 冻结后 __file__ 不可靠，显式指过去

    mock_srv, mport = bind_server(M.ThreadingHTTPServer, M.H, "127.0.0.1", mock_port)
    mock_base = "http://127.0.0.1:%d" % mport
    serve(mock_srv)

    class _A:                      # 冒充 server.ARGS（server 只读这几个字段）
        pass
    a = _A()
    a.host = "127.0.0.1"
    a.port = 0
    a.verbose = bool(verbose)
    a.mock_base = None
    ws_srv, wport = bind_server(S.ThreadingHTTPServer, S.H, "127.0.0.1", ws_port)
    a.port = wport
    a.mock_base = mock_base
    S.ARGS = a
    serve(ws_srv)

    return {"mock_base": mock_base, "mock_port": mport,
            "ws_base": "http://127.0.0.1:%d" % wport, "ws_port": wport,
            "data_dir": data_dir, "logdir": logdir, "routes_dir": routes_dir,
            "mock_srv": mock_srv, "ws_srv": ws_srv}


# ---------------------------------------------------------------- 自测（无窗口真跑一遍）
def self_test(ws_base: str, chain: str = "v2", km: float = 2.0) -> int:
    import json as _json
    import math
    import urllib.request

    lat, lng, r = 23.0500, 113.4200, 140.0
    rlat = r / 111320.0
    rlng = r / (111320.0 * math.cos(math.radians(lat)))
    pts = [[lng + rlng * math.cos(2 * math.pi * i / 60),
            lat + rlat * math.sin(2 * math.pi * i / 60)] for i in range(61)]
    body = _json.dumps({"chain": chain, "params": {
        "points": pts, "target_km": km, "pace_s_per_km": 330, "sample_interval_s": 1.0,
        "speed_jitter": 0.08, "loop_mode": "repeat", "run_type": 1, "crs": "gcj02",
        "start_face": True, "end_face": True, "middle_faces": [0.3, 0.7], "sex": "male"}}).encode()
    req = urllib.request.Request(ws_base + "/api/flow/run", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as f:
        r = _json.loads(f.read().decode("utf-8"))
    for s in r.get("steps") or []:
        print("  [%s] %s  — %s" % ("OK  " if s["ok"] else "FAIL", s["name"], s.get("detail", "")))
    nfail = sum(1 for s in (r.get("steps") or []) if not s["ok"])
    print("\n==== app 自测（%s）：%d 步 / %d FAIL ====" % (chain, len(r.get("steps") or []), nfail))
    return 0 if r.get("ok") else 1


# ---------------------------------------------------------------- 开窗口
def open_window(url: str, title: str = "校园跑 · 路线工作台", auto_close: float = 0.0,
                width: int = 1280, height: int = 860, min_size=(900, 600)) -> bool:
    try:
        import webview
    except Exception as e:                                   # noqa: BLE001
        print("[warn] pywebview 不可用（%r），改用系统浏览器" % e, flush=True)
        return False
    try:
        win = webview.create_window(title, url, width=width, height=height, min_size=min_size)
        if auto_close > 0:                                   # 打包冒烟：开 N 秒自己关
            def _closer(w):
                time.sleep(auto_close)
                try:
                    w.destroy()
                except Exception:                            # noqa: BLE001
                    pass
            webview.start(_closer, win)
        else:
            webview.start()                                  # 阻塞到窗口关闭
        return True
    except Exception as e:                                   # noqa: BLE001
        print("[warn] 窗口打不开（%r），改用系统浏览器" % e, flush=True)
        return False


# ---------------------------------------------------------------- 单实例（防多开）
_INSTANCE_HANDLE = None          # 保活：句柄一旦被 GC，锁就没了
_MUTEX_NAME = "Local\\tsn-auto-run-single-instance"


def acquire_single_instance(name: str = _MUTEX_NAME):
    """抢单实例锁：抢到返回句柄（须保活到进程退出），已被占用返回 None。

    Windows 用**命名互斥体**（跨进程、进程退出自动释放）；非 Windows 退化为占一个本机端口。
    """
    global _INSTANCE_HANDLE
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            h = k32.CreateMutexW(None, False, name)
            if not h:
                return None                       # 拿不到就当作已在跑，宁可不启第二个
            if k32.GetLastError() == 183:         # ERROR_ALREADY_EXISTS
                k32.CloseHandle(h)
                return None
            _INSTANCE_HANDLE = h
            return h
        except Exception:                        # noqa: BLE001
            return object()                      # 互斥体不可用时不误挡
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 17665))
    except OSError:
        s.close()
        return None
    _INSTANCE_HANDLE = s
    return s


def _last_ws_url(data_dir=None):
    """从 launcher.log 里抠出上次的工作台地址（用于提示已在运行的实例在哪）。"""
    try:
        import re
        log = os.path.join(pick_data_dir(data_dir), "30-logs", "launcher.log")
        if not os.path.isfile(log):
            return None
        txt = open(log, encoding="utf-8", errors="replace").read()
        hits = re.findall(r"工作台\s*：\s*(http://127\.0\.0\.1:\d+)", txt)
        return hits[-1] if hits else None
    except Exception:                            # noqa: BLE001
        return None


def notify_already_running(data_dir=None) -> None:
    """已经在跑时的提示：能弹窗就弹窗（windowed 无控制台），否则写日志。"""
    url = _last_ws_url(data_dir)
    msg = "程序已经在运行了。\n\n请直接用已打开的工作台窗口，不要重复启动。"
    if url:
        msg += "\n\n当前地址：%s" % url
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "校园跑 · 路线工作台", 0x40)
            return
        except Exception:                        # noqa: BLE001
            pass
    print("[info] " + msg.replace("\n", " "), flush=True)


def main():
    stdio = ensure_stdio()          # 必须在任何 print 之前（windowed 下 stdout 是 None）
    ap = argparse.ArgumentParser(description="校园跑 · 路线工作台（一键全流程）")
    ap.add_argument("--mock-port", type=int, default=18080)
    ap.add_argument("--ws-port", type=int, default=8770)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    ap.add_argument("--gdfe", action="store_true", help="广财定制版：开「广财阳光跑」精简页（手机尺寸）")
    ap.add_argument("--self-test", action="store_true", help="起服务后自己跑一遍全流程再退出")
    ap.add_argument("--chain", default="v2", choices=["v1", "v2"])
    ap.add_argument("--km", type=float, default=2.0)
    ap.add_argument("--window-seconds", type=float, default=0.0,
                    help=">0 时开窗这么多秒后自动关（打包冒烟用；0=正常开着）")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if acquire_single_instance() is None:     # 防多开：已在运行就别再起一个
        notify_already_running(a.data_dir)
        return 0

    info = start_all(mock_port=a.mock_port, ws_port=a.ws_port,
                     data_dir=a.data_dir, verbose=a.verbose)
    print("=" * 62, flush=True)
    print(" 自带 mock ： %s   （只监听本机，不连真实服务器）" % info["mock_base"], flush=True)
    print(" 工作台    ： %s" % info["ws_base"], flush=True)
    print(" 数据目录  ： %s" % info["data_dir"], flush=True)
    print("   30-logs  ： %s" % info["logdir"], flush=True)
    print("   routes   ： %s" % info["routes_dir"], flush=True)
    print("=" * 62, flush=True)
    print("（上面这些地址也写在 %s）" % _append_launch_log(info), flush=True)

    if a.self_test:
        rc = self_test(info["ws_base"], chain=a.chain, km=a.km)
        info["ws_srv"].shutdown()
        info["mock_srv"].shutdown()
        return rc

    if a.no_window:
        print("（--no-window：服务已起，Ctrl+C 退出）", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    wurl = info["ws_base"].rstrip("/") + ("/gdfe" if a.gdfe else "/")
    wtitle = "广财阳光跑" if a.gdfe else "校园跑 · 路线工作台"
    wsize = dict(width=470, height=900, min_size=(380, 560)) if a.gdfe else \
        dict(width=1280, height=860, min_size=(900, 600))
    if not open_window(wurl, title=wtitle, auto_close=a.window_seconds, **wsize):
        webbrowser.open(wurl)
        print("（浏览器已打开；关掉本窗口即退出）", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
