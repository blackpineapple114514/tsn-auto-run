# -*- coding: utf-8 -*-
"""server.py —— 路线工作台后端（纯标准库，无第三方依赖）。

接口
----
  GET  /                    → static/index.html（Leaflet 地图工作台）
  GET  /static/<file>       → 静态资源
  GET  /api/config          → 默认中心点 / 默认参数 / 坐标系说明
  GET  /api/schools         → 学校目录（登录页「省份 / 学校」下拉；公共目录，免登录）
  POST /api/generate        → 折线 → 轨迹（两套结构 + 步数桶 + 人脸 + 合格性 + 21 参表单）
  POST /api/submit          → 把生成的上传表单 POST 到本仓 mock（自动算签名）
  GET  /api/routes          → 列出已保存路线
  POST /api/routes/save     → 保存路线 {name, points, params}
  GET  /api/routes/load?id= → 读取路线

用法
----
  python server.py --port 8770
  # 浏览器开 http://127.0.0.1:8770
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
PROJ = os.path.dirname(TOOLS)
STATIC = os.path.join(HERE, "static")
ROUTES_DIR = os.environ.get("ROUTE_STUDIO_ROUTES") or os.path.join(HERE, "routes")

# 版本（edition）：精简版 / 完整版**共用同一份源码**，靠可执行文件名区分 —— 名字里带 "full" 就是完整版。
# 开发 / 自测可用 --edition 覆盖。前端按 /api/config 的 edition 决定显示哪些控件。
def _detect_edition() -> str:
    name = os.path.basename(sys.executable or (sys.argv[0] if sys.argv else "") or "").lower()
    return "full" if "full" in name else "lite"

EDITION = _detect_edition()
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS)

import tracklib as T          # noqa: E402
import device_profile as DP    # noqa: E402  （设备 / app 身份的唯一出处）

try:
    import w8b_sign_probe as SIGN   # noqa: E402
except Exception:                   # pragma: no cover
    SIGN = None

ARGS = None
_SESS = {}          # 向导第一步的登录结果（sid → session），只在内存里，重启即失效
# 实时上传的后台任务（job id → job）。实时＝按计划的真实用时真等（十几分钟都可能），
# HTTP 不能一直挂着，所以第三步立刻回一个 job id：前端轮询 /api/stage/run/status 看进度
# （回执 + 已跑位置），/api/stage/run/cancel 中止。
_JOBS = {}
_JOBS_LOCK = threading.Lock()
JOB_TTL_MS = 60 * 60 * 1000       # 一小时前的任务顺手清掉
JOB_MAX = 40


def _job_gc():
    """清过期 / 超量的任务（只在建新任务时顺手调一次）。"""
    now = int(time.time() * 1000)
    with _JOBS_LOCK:
        for jid in [k for k, v in _JOBS.items()
                    if now - int(v.get("started_at") or 0) > JOB_TTL_MS]:
            _JOBS.pop(jid, None)
        while len(_JOBS) > JOB_MAX:
            oldest = min(_JOBS.items(), key=lambda kv: kv[1].get("started_at") or 0)[0]
            _JOBS.pop(oldest, None)


def _job_pub(job: dict) -> dict:
    """任务快照（给前端轮询用，不带内部字段）。"""
    return {"ok": True, "job": job.get("id"), "state": job.get("state") or "running",
            "phase": job.get("phase") or "",
            "steps": [dict(s) for s in (job.get("steps") or [])],
            "total_ms": int(job.get("total_ms") or 0), "done_ms": int(job.get("done_ms") or 0),
            "pos": job.get("pos"), "speed": job.get("speed") or 1.0,
            "stopped": bool(job.get("stopped")), "stop": bool(job.get("stop")),
            "started_at": job.get("started_at"),
            "elapsed_ms": max(0, int(time.time() * 1000) - int(job.get("started_at") or 0)),
            "result": job.get("result"), "error": job.get("error")}
# 默认中心：广东财经大学（见 15-精确标识符台账）。坐标按 OSM(WGS-84) 显示。
DEFAULT_CENTER = {"lat": 23.1994, "lng": 113.2790, "name": "广东财经大学(三水校区) 附近"}

# ============================ 广财定制版（gdfe）============================
# 「广财阳光跑」：只选圈数 / 配速 / 性别，起点与道次随机、方向固定逆时针。
# 起点 = 广州校区田径场（中心由本人合格记录反推），几何 = 标准 400 m 跑道。
GDFE_CENTER = {"lat": 23.0937093, "lng": 113.3528926}
GDFE_SCHOOL = "gdufe"
GDFE_REAL_BASE_FALLBACK = "http://a.sxstczx.com/"
# 真跑的账号 / 密码来源：**只在进程内现读**（不打印、不落盘、不进命令行）。
# 文件形如两行：`<两字中文标签><账号>` / `<两字中文标签><密码>`（标签可有可无）。
GDFE_CRED_FILE = (os.environ.get("GDFE_CRED_FILE")
                  or os.path.join(os.path.expanduser("~"), "Desktop", "p.txt"))
# 「记住账号密码」的本地保存位置（勾了「记住」才写；明文，仅本机；清点「忘记」或删本文件）。
GDFE_CREDS_FILE = (os.environ.get("GDFE_CREDS_FILE")
                   or os.path.join(os.path.expanduser("~"), ".gdfe_run_creds.json"))
# 「选定机型」的本地保存位置（下拉选定后固定）。**与账号密码分开存**：清账号不影响机型。
GDFE_DEVICE_FILE = (os.environ.get("GDFE_DEVICE_FILE")
                    or os.path.join(os.path.expanduser("~"), ".gdfe_run_device.json"))


def _gdfe_creds_load() -> dict:
    """读回「记住的账号密码」；不存在 / 损坏则返回 {}。"""
    try:
        with open(GDFE_CREDS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {"username": str(d.get("username") or ""),
                "password": str(d.get("password") or "")}
    except Exception:                                        # noqa: BLE001
        return {}


def _gdfe_creds_save(username: str, password: str) -> None:
    """保存账号密码到本机文件（明文，仅本机）。"""
    try:
        with open(GDFE_CREDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"username": username, "password": password}, f,
                      ensure_ascii=False, indent=2)
    except Exception:                                        # noqa: BLE001
        pass


def _gdfe_creds_clear() -> None:
    """「忘记」：删掉本机保存的账号密码。"""
    try:
        if os.path.isfile(GDFE_CREDS_FILE):
            os.remove(GDFE_CREDS_FILE)
    except Exception:                                        # noqa: BLE001
        pass


def _creds_payload() -> dict:
    """GET /api/creds 的响应：本机记住的账号密码（明文，仅本机）。"""
    d = _gdfe_creds_load()
    return {"ok": True, "saved": os.path.isfile(GDFE_CREDS_FILE),
            "username": d.get("username") or "", "password": d.get("password") or ""}


def _creds_apply(body: dict) -> dict:
    """POST /api/creds：action=clear/forget/delete 删除；否则保存账号密码。"""
    b = body or {}
    if str(b.get("action") or "save").lower() in ("clear", "forget", "delete"):
        _gdfe_creds_clear()
        return {"ok": True, "saved": False}
    u = str(b.get("username") or "")
    pw = b.get("password")
    pw = "" if pw is None else str(pw)
    if u or pw:
        _gdfe_creds_save(u, pw)
    return {"ok": True, "saved": os.path.isfile(GDFE_CREDS_FILE)}


def _gdfe_device_load() -> dict:
    """读回「选定机型」四件套；不存在 / 损坏则返回 {}（调用方回落到默认档）。"""
    try:
        with open(GDFE_DEVICE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {k: str(d[k]) for k in ("brand", "model", "system", "version")
                if d.get(k)}
    except Exception:                                        # noqa: BLE001
        return {}


def _gdfe_device_save(dev: dict) -> None:
    """保存「选定机型」到本机文件（明文，仅本机）。"""
    try:
        with open(GDFE_DEVICE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: str(dev[k]) for k in ("brand", "model", "system", "version")
                       if dev.get(k)}, f, ensure_ascii=False, indent=2)
    except Exception:                                        # noqa: BLE001
        pass


# 用户「自行添加」的机型（目录里找不到时手填；下次下拉里仍能选到）。
GDFE_CUSTOM_DEVICE_FILE = (os.environ.get("GDFE_CUSTOM_DEVICE_FILE")
                           or os.path.join(os.path.expanduser("~"),
                                           ".gdfe_run_custom_devices.json"))


def _gdfe_custom_load() -> list:
    """读回用户自加机型（列表）；不存在 / 损坏则返回 []。"""
    try:
        with open(GDFE_CUSTOM_DEVICE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        out = []
        for it in (d if isinstance(d, list) else []):
            dev = {k: str(it.get(k) or "") for k in ("brand", "model", "system", "version")}
            if dev["brand"] and dev["model"]:
                dev["name"] = str(it.get("name") or "")
                out.append(dev)
        return out
    except Exception:                                        # noqa: BLE001
        return []


def _gdfe_custom_save(items: list) -> None:
    """写入用户自加机型列表。"""
    try:
        with open(GDFE_CUSTOM_DEVICE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:                                        # noqa: BLE001
        pass


def _gdfe_catalog_full() -> list:
    """目录（device_profile.CATALOG）+ 用户自加机型，合一份下拉用列表。"""
    cat = DP.catalog()
    have = {(d["brand"], d["model"]) for d in cat}
    for cd in _gdfe_custom_load():
        if (cd["brand"], cd["model"]) in have:
            continue
        have.add((cd["brand"], cd["model"]))
        name = cd.get("name") or cd["model"]
        label = cd["model"] if name == cd["model"] else "%s（%s）" % (name, cd["model"])
        cat.append({"id": "%s|%s" % (cd["brand"], cd["model"]), "label": label,
                    "brand": cd["brand"], "model": cd["model"],
                    "system": cd["system"] or "Android",
                    "version": cd["version"] or "",
                    "group": DP.BRAND_GROUP.get(cd["brand"], "自定义")})
    return cat


# 「默认机型」：用户勾选「设为默认」才写；下次打开优先用它（高于「上次选定」）。
GDFE_DEFAULT_DEVICE_FILE = (os.environ.get("GDFE_DEFAULT_DEVICE_FILE")
                            or os.path.join(os.path.expanduser("~"),
                                            ".gdfe_run_default_device.json"))


def _gdfe_default_load() -> dict:
    """读回「默认机型」四件套；不存在 / 损坏则返回 {}。"""
    try:
        with open(GDFE_DEFAULT_DEVICE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {k: str(d[k]) for k in ("brand", "model", "system", "version") if d.get(k)}
    except Exception:                                        # noqa: BLE001
        return {}


def _gdfe_default_save(dev: dict) -> None:
    """把当前机型记为「默认」。"""
    try:
        with open(GDFE_DEFAULT_DEVICE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: str(dev[k]) for k in ("brand", "model", "system", "version")
                       if dev.get(k)}, f, ensure_ascii=False, indent=2)
    except Exception:                                        # noqa: BLE001
        pass


def _gdfe_default_clear() -> None:
    """取消「默认机型」（回落到内置真机基线）。"""
    try:
        if os.path.isfile(GDFE_DEFAULT_DEVICE_FILE):
            os.remove(GDFE_DEFAULT_DEVICE_FILE)
    except Exception:                                        # noqa: BLE001
        pass



def _gdfe_cred():
    """现读账号 / 密码。返回 (user, pwd)；**绝不写回前端 / 日志 / 命令行**。"""
    import re as _re
    with open(GDFE_CRED_FILE, "r", encoding="utf-8") as f:
        raw = f.read()
    vals = []
    for ln in raw.splitlines():
        v = _re.sub(r"^[^\x00-\x7f]+", "", ln).strip()   # 去掉行首的中文标签
        if v:
            vals.append(v)
    if len(vals) < 2:
        raise RuntimeError("凭据文件需要两行：账号 / 密码")
    return vals[0], vals[1]


def _gdfe_real_base() -> str:
    """真服地址：取包内 schools.json 里 gdufe 的 lanUrl（离线可用）。"""
    for s in (_school_snapshot().get("schools") or []):
        if str(s.get("code") or "").lower() == GDFE_SCHOOL:
            b = str(s.get("lanUrl") or s.get("url") or "").strip()
            if b:
                return b
    return GDFE_REAL_BASE_FALLBACK


_RUN_TYPE_NAME = {0: "自由跑", 1: "阳光跑", 2: "晨跑"}


def _day_of_ms(ms) -> str:
    """毫秒时间戳 → **本机时区**的 `YYYY-MM-DD`（今日判定 / 记录日期共用）。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ms) / 1000.0))
    except Exception:                                            # noqa: BLE001
        return ""


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:                                            # noqa: BLE001
        return d


def _gdfe_login(body: dict) -> dict:
    """「广财阳光跑」第一步：登录 + 拉设置。

    `server='real'`（默认）连学校真服，账号密码用页面传的（缺失则回落到本机「记住的」）；
    `server='mock'` 只打本仓 mock —— **绝不连真服**。密码不进日志。
    """
    try:
        import flow as F
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "flow 未加载: %r" % e}
    server = str(body.get("server") or "real").lower()
    mock_base = (ARGS.mock_base if ARGS else None) or "http://127.0.0.1:18080"
    if server == "real":
        base = str(body.get("base") or "").strip() or _gdfe_real_base()
        username = str(body.get("username") or "").strip()
        password = body.get("password")
        if not username or password is None or password == "":
            saved = _gdfe_creds_load()          # 页面没给 → 用本机「记住的」
            username = username or saved.get("username") or ""
            if password is None or password == "":
                password = saved.get("password") or ""
        if not username or password is None or password == "":
            return {"ok": False, "server": server, "base": base,
                    "error": "需要账号 / 密码"}
    else:
        base = str(body.get("base") or "").strip() or mock_base
        username = str(body.get("username") or "20210001")
        password = body.get("password") or "mock-pass"
    try:
        r = F.stage_login(chain="v2", base=base, school_code=GDFE_SCHOOL,
                          username=username, password=password,
                          run_type=int(body.get("run_type", 1)),
                          device=body.get("device"), app_version=body.get("app_version"))
    except Exception as e:                                   # noqa: BLE001
        import traceback
        return {"ok": False, "server": server, "base": base, "error": repr(e),
                "trace": traceback.format_exc()[-2000:]}
    sess = r.pop("session", None) or {}
    if r.get("ok") and sess:
        sid = uuid.uuid4().hex[:12]
        sess["chain"] = "v2"
        sess["base"] = base
        _SESS[sid] = sess
        r["sid"] = sid
    r["base"] = base
    r["server"] = server
    # 真服时别把账号带到浏览器（密码本来就没带）
    if server == "real" and isinstance(r.get("info"), dict):
        r["info"].pop("username", None)
    return r


def _sign_body(base: str, path: str, form: dict) -> dict:
    """按 17 §2 给表单补 timestamp + sign（云侧纯 MD5 分支）。"""
    f = dict(form)
    ts = str(int(time.time() * 1000))
    f["timestamp"] = ts
    if SIGN is not None:
        f["sign"] = SIGN.sign_cloud(SIGN.build_cloud_map(f, ts))
    return f


def _post_form(base: str, path: str, form: dict, token: str = "mock-token") -> dict:
    body = urllib.parse.urlencode(_sign_body(base, path, form)).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("token", token)
    req.add_header("channel", DP.DEFAULT_DEVICE["system"])
    req.add_header("version", DP.DEFAULT_APP_VERSION)
    req.add_header("type", "1")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def generate_track(body: dict, *, start_ms=None, identify=None, run_type=None) -> dict:
    """按 /api/generate 的 body 生成一条轨迹（server 与 flow 共用）。

    `start_ms` / `identify` / `run_type` 显式给了就覆盖 body —— 全流程里这两个值
    是**服务器下发**的（getRunningStartTime / getSetting），必须在生成之前拿到。
    """
    return T.generate(
        path=body.get("points") or [],
        target_km=float(body.get("target_km", 2.0)),
        plan_pace_s_per_km=float(body.get("pace_s_per_km", 330)),
        start_ms=(int(start_ms) if start_ms else
                  (int(body["start_ms"]) if body.get("start_ms") else None)),
        run_type=int(run_type if run_type is not None else body.get("run_type", 1)),
        device=body.get("device"),
        app_version=body.get("app_version"),
        identify=identify or body.get("identify") or "identify-%d" % int(time.time()),
        user_id=str(body.get("user_id") or "20210001"),
        setting=body.get("setting"),
        sample_interval_s=float(body.get("sample_interval_s", 1.0)),
        speed_jitter=float(body.get("speed_jitter", 0.08)),
        interval_jitter_s=float(body.get("interval_jitter_s", 0.0)),
        loop_mode=body.get("loop_mode", "repeat"),
        track_lateral_m=float(body.get("track_lateral_m", T.TRACK_LATERAL_M)),
        track_pace_pct=float(body.get("track_pace_pct", T.TRACK_PACE_PCT)),
        coord_jitter_deg=float(body.get("coord_jitter_deg", 0.0)),
        gps_noise_m=float(body.get("gps_noise_m", T.GPS_NOISE_M)),
        gps_outage_p=float(body.get("gps_outage_p", T.GPS_OUTAGE_P)),
        pace_swing=float(body.get("pace_swing", 1.0)),
        dup_p=float(body.get("dup_p", T.DUP_P)),
        pause_tail_points=int(body.get("pause_tail_points", 0)),
        sex=str(body.get("sex") or "male"),
        stride_m=(float(body["stride_m"]) if body.get("stride_m") else None),
        stride_jitter=float(body.get("stride_jitter", 0.0)),
        pause_count=int(body.get("pause_count", 0)),
        warmup_s=float(body.get("warmup_s", 0.0)),
        cooldown_s=float(body.get("cooldown_s", 0.0)),
        # ⚠️ 这 4 个以前**没转发** —— 路线里的 `stop_tail_s` 被吞掉，真跑出来的记录
        #    少了「停下后继续记一段」的收尾点（真机那条有 40 个 i=true 的收尾点），
        #    也导致「干跑（直调 T.generate）」与「真跑（走本函数）」不是同一份数据。
        stop_tail_s=float(body.get("stop_tail_s", 0.0)),
        stop_walk_mps=float(body.get("stop_walk_mps", 1.5)),
        sample_drop_p=float(body.get("sample_drop_p", T.SAMPLE_DROP_P)),
        clock_jitter_p=float(body.get("clock_jitter_p", T.CLOCK_JITTER_P)),
        # B1/B2（2026-09-27）：起步丢点与停顿段形态 —— 以前这两个行为没得选，
        # 现在可由路线文件覆盖（默认 = 真机形态：丢前 4 s、停顿落成 3+4+9 点）。
        start_skip_s=float(body.get("start_skip_s", T.START_SKIP_S)),
        pause_block=bool(body.get("pause_block", True)),
        mid_pauses=body.get("mid_pauses") or [],
        crs=body.get("crs", "gcj02"),
        start_face=bool(body.get("start_face")),
        end_face=bool(body.get("end_face")),
        middle_faces=body.get("middle_faces") or [],
        target_jitter_m=float(body.get("target_jitter_m", 0.0)),
        seed=body.get("seed"),
    )


# ---- 学校目录（登录页「省份 / 学校」下拉）--------------------------------------
# 「选校」是在**登录之前**做的，所以目录是**公共**的：m.boxkj.com 的
# /app/province/findAllProvince + /app/sch/listSchoolByProvinceId（不需要 token）。
# 优先问「当前这台服务器」（调试模式下就是自带 mock ⇒ 断网也能列），
# 问不到再回落包内快照 static/schools.json（只有广东省）。
_SCHOOL_CACHE = {}
SCHOOL_TTL_S = 600
SCHOOL_SNAP_TTL_S = 60            # 回落快照只缓存 1 分钟，免得一直压着在线目录
SCHOOL_SNAPSHOT = os.path.join(STATIC, "schools.json")
SCHOOL_DIR_BASE = "https://m.boxkj.com"
SCHOOL_TIMEOUT_S = 4.0
DEFAULT_PROVINCE_ID = 11          # 广东省


def _school_snapshot() -> dict:
    try:
        with open(SCHOOL_SNAPSHOT, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                          # noqa: BLE001
        return {"provinces": [], "schools": []}


def _dir_call(base: str, path: str, form: dict):
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "okhttp/3.12.0")
    with urllib.request.urlopen(req, timeout=SCHOOL_TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _norm_school(s: dict) -> dict:
    """统一成前端用的短字段（目录两种口径的字段名都给，取任一）。"""
    return {
        "code": (s.get("schoolCode") or "").strip(),
        "name": s.get("school_name") or s.get("schName") or s.get("schoolName") or "",
        "schoolId": s.get("school_id") or s.get("schoolId") or s.get("id"),
        "url": s.get("school_url") or s.get("urlSite") or "",
        "lanUrl": s.get("lan_url") or s.get("lanUrl") or "",
        "sysType": str(s.get("sysType") if s.get("sysType") is not None else ""),
        # 目录里的「必须在校内网」开关：1 ⇒ 不在校园网里登录必失败（前端据此提前提醒）
        "insideNetwork": str(s.get("insideNetwork") if s.get("insideNetwork") is not None
                             else (s.get("inside_network") if s.get("inside_network") is not None else "")),
        "insideNetworkMsg": s.get("insideNetworkMsg") or s.get("inside_network_msg") or "",
        "province": s.get("province_name") or "",
    }


def _lan_host(s: dict) -> str:
    """取这所学校的服务器主机名（lan_url 优先，回落 H5 门户 url）。"""
    u = str(s.get("lanUrl") or s.get("url") or "").strip()
    try:
        return (urllib.parse.urlparse(u).hostname or "").lower()
    except Exception:                                          # noqa: BLE001
        return ""


def _school_rank(s: dict, share: dict):
    """同一 schoolCode 在目录里可能出现**多行**：学校自己的部署 + 厂商公共域租户
    （gdut id=501 tsn.gdut.edu.cn / id=338 h.tsnkj.com；gdufs 同理）。
    谁更像「这所学校自己的」：它的服务器主机名只有这一家在用（自有域）；
    厂商公共域是几十所学校共用的（`share[host]` 很大）。其次看名字有没有被脱敏（「某某」）。"""
    h = _lan_host(s)
    return (share.get(h, 0), 1 if "某某" in str(s.get("name") or "") else 0)


def _schools(province_id=None, refresh=False, remote=False) -> dict:
    """学校目录。返回 {ok, source: snapshot[+mock][+live], provinceId, provinces[], schools[], note?}。

    口径是**并集**：包内快照（免费、离线）先铺底，再让「当前服务器」和「公共目录」补全/覆盖 ——
    这样即使自带 mock 的数据目录里没有 `schools_gd.json`（只会回 1 所），下拉也不会只剩一项。
    """
    try:
        pid = int(province_id) if str(province_id or "").strip() else DEFAULT_PROVINCE_ID
    except (TypeError, ValueError):
        pid = DEFAULT_PROVINCE_ID
    ck = (pid, bool(remote))
    now = time.time()
    if not refresh:
        hit = _SCHOOL_CACHE.get(ck)
        if hit and now - hit[0] < hit[2]:
            return hit[1]

    snap = _school_snapshot()
    provinces = snap.get("provinces") or []
    merged = {}                      # code → 学校（本轮挑出的那一行）
    cand = []                        # 全部候选行：先包内快照，后各目录源 —— 供「同 code 挑哪行」用
    for s in snap.get("schools") or []:
        if s.get("province_id") == pid and s.get("code"):
            merged[s["code"]] = dict(s, source="snapshot")
            cand.append(merged[s["code"]])
    used = ["snapshot"] if merged else []

    mock_base = (ARGS.mock_base if ARGS else None) or "http://127.0.0.1:18080"
    tries = [(SCHOOL_DIR_BASE, "live")] if remote else [(mock_base, "mock"), (SCHOOL_DIR_BASE, "live")]
    for base, tag in tries:
        try:
            pv = _dir_call(base, "/app/province/findAllProvince", {}).get("data") or []
            rows = _dir_call(base, "/app/sch/listSchoolByProvinceId", {"provinceId": pid}).get("data") or []
        except Exception:                                       # noqa: BLE001
            continue
        if pv:
            provinces = [{"id": p.get("province_id"), "name": p.get("province_name")} for p in pv]
        got = 0
        for s in rows:
            n = _norm_school(s)
            if not n["code"]:
                continue
            cand.append(dict(n, source=tag))
            got += 1
        if got and tag not in used:
            used.append(tag)

    share = {}                       # 服务器主机名 → 有多少所学校用它（厂商公共域会很大）
    for c in cand:
        h = _lan_host(c)
        share[h] = share.get(h, 0) + 1
    for c in cand:
        code = c["code"]
        prev = merged.get(code)
        if prev is None or _school_rank(c, share) < _school_rank(prev, share):
            merged[code] = dict({**(prev or {}), **{k: v for k, v in c.items() if v}},
                                source=c.get("source"))

    rows = sorted(merged.values(), key=lambda x: x.get("name") or "")
    data = {"ok": True, "source": "+".join(used) or "none", "provinceId": pid,
            "provinces": provinces, "schools": rows}
    if not used:
        data["note"] = ("在线目录也拿不到，包内快照只有广东省 ⇒ 该省暂无数据，可直接手填 schoolCode。"
                        if pid != DEFAULT_PROVINCE_ID else
                        "目录读取失败（离线？），包内快照也没读到。")
    _SCHOOL_CACHE[ck] = (now, data, SCHOOL_TTL_S if "live" in used else SCHOOL_SNAP_TTL_S)
    return data

class H(BaseHTTPRequestHandler):
    server_version = "RouteStudio/1.0"
    # ⚠️ 必须 HTTP/1.1：HTTP/1.0 下服务器每个响应后关连接，Chromium(WebView2) 复用连接时
    # 会撞上「已关闭的 socket」→ 偶发 `TypeError: Failed to fetch`（2026-10-11 定位，
    # 表现为开跑后状态轮询偶发失败）。本文件所有响应都带 Content-Length，可安全 keep-alive。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):      # 静音
        if ARGS and ARGS.verbose:
            sys.stderr.write("[route_studio] " + (fmt % a) + "\n")

    # ------------------------------------------------------------------ helpers
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, ctype="text/html; charset=utf-8"):
        if not os.path.isfile(path):
            self._json({"error": "not found", "path": path}, 404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _raw(self, data: bytes, ctype: str, filename: str = None, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if p in ("/gdfe", "/gdfe.html"):
            return self._file(os.path.join(STATIC, "gdfe.html"))
        if p == "/api/gdfe/info":
            return self._json({"ok": True, "school": GDFE_SCHOOL, "center": GDFE_CENTER,
                               "real_base": _gdfe_real_base(),
                               "cred_file": GDFE_CRED_FILE,
                               "cred_ready": os.path.isfile(GDFE_CRED_FILE),
                               "creds_file": GDFE_CREDS_FILE,
                               "creds_saved": os.path.isfile(GDFE_CREDS_FILE)})
        if p in ("/api/gdfe/creds", "/api/creds"):
            return self._json(_creds_payload())
        if p == "/api/gdfe/devices":
            # 机型目录（唯一出处 device_profile.CATALOG）+ 用户自加机型 + 「默认机型」 + 本机选定值。
            dflt = _gdfe_default_load()
            return self._json({"ok": True, "catalog": _gdfe_catalog_full(),
                               "default": dflt or {k: DP.DEFAULT_DEVICE[k]
                                                    for k in ("brand", "model", "system", "version")},
                               "default_saved": bool(dflt),
                               "selected": _gdfe_device_load()})
        if p.startswith("/static/"):
            name = os.path.basename(p)
            ctype = "text/plain; charset=utf-8"
            if name.endswith(".js"):
                ctype = "application/javascript; charset=utf-8"
            elif name.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            return self._file(os.path.join(STATIC, name), ctype)
        if p == "/api/config":
            return self._json({
                "edition": EDITION,
                "center": DEFAULT_CENTER,
                "flow": {
                    "chain": "v2",
                    "base": ((ARGS.mock_base if ARGS else None) or "http://127.0.0.1:18080"),
                    "school_code": "gdufe",
                    "username": "20210001",
                },
                "crs": {"default": "gcj02",
                        "note": "底图=OSM(WGS-84)；app 用高德(GCJ-02)。导出默认自动偏移，可切 wgs84。"},
                "defaults": {
                    "target_km": 2.0, "pace_s_per_km": 330, "sample_interval_s": 1.0,
                    "speed_jitter": 0.08, "loop_mode": "repeat", "run_type": 1,
                    "track_lateral_m": T.TRACK_LATERAL_M, "track_pace_pct": T.TRACK_PACE_PCT,
                    "coord_jitter_deg": 0.0,
                    "gps_noise_m": T.GPS_NOISE_M, "pace_swing": 1.0, "dup_p": T.DUP_P,
                    "sex": "male", "pause_count": 0,
                    # 默认设备档 = **真机 iPhone**（唯一出处 `device_profile.py`）：真机基线
                    # brand=iPhone / model=iPhone 14 Pro Max / system=iOS /
                    # version=26.6.1 / appVersion=3.6.814（只读拉回本人合格记录坐实）。
                    "device": {k: DP.DEFAULT_DEVICE[k]
                               for k in ("brand", "model", "system", "version")},
                    "app_version": DP.DEFAULT_APP_VERSION,
                    "mock_base": ((ARGS.mock_base if ARGS else None) or "http://127.0.0.1:18080"),
                },
            })
        if p == "/api/routes":
            os.makedirs(ROUTES_DIR, exist_ok=True)
            items = []
            for fn in sorted(os.listdir(ROUTES_DIR)):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(ROUTES_DIR, fn), "r", encoding="utf-8") as f:
                            d = json.load(f)
                        items.append({"id": fn[:-5], "name": d.get("name", fn[:-5]),
                                      "points": len(d.get("points", [])),
                                      "saved_at": int(d.get("saved_at") or 0)})
                    except Exception:
                        continue
            # 最新在前：前端「选择轨迹」默认取第一条（简单模式没有下拉，默认就是最新那条）
            items.sort(key=lambda x: (x["saved_at"], x["id"]), reverse=True)
            return self._json({"routes": items})
        if p == "/api/routes/load":
            q = urllib.parse.parse_qs(u.query)
            rid = (q.get("id") or [""])[0]
            fp = os.path.join(ROUTES_DIR, "%s.json" % os.path.basename(rid))
            if not os.path.isfile(fp):
                return self._json({"error": "route not found"}, 404)
            with open(fp, "r", encoding="utf-8") as f:
                return self._json(json.load(f))
        if p == "/api/routes/export":
            q = urllib.parse.parse_qs(u.query)
            rid = (q.get("id") or [""])[0]
            crs = (q.get("crs") or ["wgs84"])[0]
            fp = os.path.join(ROUTES_DIR, "%s.json" % os.path.basename(rid))
            if not os.path.isfile(fp):
                return self._json({"error": "route not found"}, 404)
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            try:
                gpx = T.to_gpx(d.get("points") or [], name=d.get("name") or rid, crs=crs)
            except T.TrackError as e:
                return self._json({"error": str(e)}, 400)
            return self._raw(gpx.encode("utf-8"), "application/gpx+xml; charset=utf-8",
                             "%s.gpx" % os.path.basename(rid))
        if p == "/api/stage/run/status":
            q = urllib.parse.parse_qs(u.query)
            jid = (q.get("id") or [""])[0]
            with _JOBS_LOCK:
                job = _JOBS.get(jid)
                snap = _job_pub(job) if job else None
            if not snap:
                return self._json({"ok": False, "state": "not found", "error": "job not found"}, 404)
            return self._json(snap)
        if p == "/api/schools":
            q = urllib.parse.parse_qs(u.query)
            return self._json(_schools(province_id=(q.get("provinceId") or [""])[0],
                                       refresh=bool(q.get("refresh")),
                                       remote=bool(q.get("remote"))))
        if p == "/healthz":
            return self._json({"ok": True, "ts": int(time.time())})
        return self._json({"error": "not found", "path": p}, 404)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if p == "/api/generate":
                return self._json(self._generate(self._body()))
            if p == "/api/submit":
                return self._json(self._submit(self._body()))
            if p == "/api/routes/save":
                return self._json(self._save(self._body()))
            if p == "/api/routes/import":
                return self._json(self._import(self._body()))
            if p == "/api/flow/run":
                return self._json(self._flow(self._body()))
            if p == "/api/stage/login":
                return self._json(self._stage_login(self._body()))
            if p == "/api/gdfe/login":
                return self._json(_gdfe_login(self._body()))
            if p == "/api/gdfe/history":
                return self._json(self._gdfe_history(self._body()))
            if p == "/api/today":
                return self._json(self._gdfe_today(self._body()))
            if p in ("/api/gdfe/creds", "/api/creds"):
                return self._json(_creds_apply(self._body()))
            if p == "/api/gdfe/device":
                # 保存「选定机型」（下拉选中后固定）；action=clear/reset 则删掉回默认档。
                b = self._body()
                if str(b.get("action") or "save").lower() in ("clear", "reset", "default"):
                    try:
                        if os.path.isfile(GDFE_DEVICE_FILE):
                            os.remove(GDFE_DEVICE_FILE)
                    except Exception:                        # noqa: BLE001
                        pass
                    return self._json({"ok": True, "saved": False})
                dev = {k: str(b.get(k) or "") for k in ("brand", "model", "system", "version")}
                # 优先匹配目录里的档（拿回一致的 system/version）；不在目录就原样存，并记进「自加机型」。
                pick = DP.find(dev.get("brand"), dev.get("model"))
                if pick is None:
                    pick = dict(dev)
                    if pick.get("brand") and pick.get("model"):
                        items = [x for x in _gdfe_custom_load()
                                 if not (x["brand"] == pick["brand"] and x["model"] == pick["model"])]
                        items.append({"brand": pick["brand"], "model": pick["model"],
                                      "system": pick.get("system") or "Android",
                                      "version": pick.get("version") or "",
                                      "name": str(b.get("name") or "")})
                        _gdfe_custom_save(items)
                _gdfe_device_save(pick)
                if "set_default" in b:                       # 勾选/取消「设为默认机型」
                    if b.get("set_default"):
                        _gdfe_default_save(pick)
                    else:
                        _gdfe_default_clear()
                dflt = _gdfe_default_load()
                return self._json({"ok": True, "saved": os.path.isfile(GDFE_DEVICE_FILE),
                                   "device": {k: pick.get(k) for k in
                                              ("brand", "model", "system", "version")},
                                   "default": dflt or {k: DP.DEFAULT_DEVICE[k]
                                                       for k in ("brand", "model", "system", "version")},
                                   "default_saved": bool(dflt)})
            if p == "/api/stage/run":
                return self._json(self._stage_run(self._body()))
            if p == "/api/stage/run/cancel":
                # mode=stop ⇒ **停在当前这一刻、按已跑的那一段上传**（页面「停止并上传」）；
                # 默认 mode=abort ⇒ 直接中止，什么都不传（原来那条路）。
                b = self._body()
                mode = str(b.get("mode") or "abort").lower()
                stop_mode = mode in ("stop", "send", "upload")
                with _JOBS_LOCK:
                    job = _JOBS.get(str(b.get("id") or ""))
                    if job:
                        if stop_mode:
                            job["stop"] = True
                        else:
                            job["cancel"] = True
                return self._json({"ok": bool(job), "job": (job or {}).get("id"),
                                   "mode": "stop" if stop_mode else "abort",
                                   "state": (job or {}).get("state") or "not found"})
        except T.TrackError as e:
            return self._json({"error": str(e), "kind": "track"}, 400)
        except Exception as e:                                   # noqa: BLE001
            import traceback
            return self._json({"error": repr(e), "trace": traceback.format_exc()[-2000:]}, 500)
        return self._json({"error": "not found", "path": p}, 404)

    # ------------------------------------------------------------------ api impl
    def _generate(self, body: dict) -> dict:
        res = generate_track(body)
        # D 合规自检：与「已保存轨迹」的最高贴近度（同一张画布反复生成时，用来发现两条走的是同一条线）
        try:
            hist = []
            if os.path.isdir(ROUTES_DIR):
                for fn in sorted(os.listdir(ROUTES_DIR)):
                    if not fn.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(ROUTES_DIR, fn), "r", encoding="utf-8") as f:
                            d = json.load(f)
                    except Exception:                            # noqa: BLE001
                        continue
                    pts = d.get("points") or []
                    if len(pts) < 2:
                        continue
                    hist.append({"id": fn[:-5], "name": d.get("name") or fn[:-5],
                                 "overlap": T.path_overlap(
                                     [{"lat": s["lat"], "lng": s["lng"]} for s in res.get("samples", [])],
                                     pts, tol_m=5.0)})
            hist.sort(key=lambda x: x["overlap"], reverse=True)
            res["similarity"] = {"tol_m": 5.0, "count": len(hist),
                                 "best": (hist[0] if hist else None)}
        except Exception:                                        # noqa: BLE001
            res["similarity"] = None
        # 人脸点带的是导出坐标系（gcj02）⇒ 附一份 OSM(WGS-84) 显示坐标，避免底图偏移
        crs = res.get("crs", "gcj02")
        for f in res.get("faces", []):
            lng, lat = f["longitude"], f["latitude"]
            if crs == "gcj02":
                lng, lat = T.gcj02_to_wgs84(lng, lat)
            f["disp"] = [lat, lng]
        # samples 点太多，不外发全量（前端只用 metrics/预览）
        res.pop("samples", None)
        res.pop("export_samples", None)
        return res

    def _flow(self, body: dict) -> dict:
        """一键全流程：登录 → 配置 → 开跑 → 人脸 → 上传 → 读回（默认只打本仓 mock）。"""
        try:
            import flow as F
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "flow 未加载: %r" % e}
        default_base = (ARGS.mock_base if ARGS else None) or "http://127.0.0.1:18080"
        params = body.get("params")
        if params is None and body.get("points"):
            params = {"points": body.get("points")}
        try:
            r = F.run_flow(
                chain=body.get("chain") or "v2",
                base=(body.get("base") or default_base),
                school_code=body.get("school_code") or "gdufe",
                username=body.get("username") or "20210001",
                password=(body.get("password") if body.get("password") is not None else "mock-pass"),
                params=params, gen=body.get("gen"),
                faces=bool(body.get("faces", True)),
                run_type=int(body.get("run_type", 1)),
                face_image_b64=body.get("face_image_b64"),
                device_id=body.get("device_id") or "route-studio-device",
                device=body.get("device"),
                app_version=body.get("app_version"),
            )
        except Exception as e:                                   # noqa: BLE001
            import traceback
            return {"ok": False, "error": repr(e), "trace": traceback.format_exc()[-2000:]}
        r.pop("gen", None)                                       # gen 全量太大，不回给前端
        return r

    # ------------------------------------------------------------------ 分步（向导：一环扣一环）
    def _gdfe_history(self, body: dict) -> dict:
        """「查询历史」：读回本账号的历史跑步记录。**只读**，不生成、不上传、不写记录。

        优先用当前会话（`sid`）的 token；没有就先现登录一次（真服需账号密码，页面已给或
        本机「记住的」）。只走 v2 链路（广财真服就是 v2）。
        """
        try:
            import v2client as V2C
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "v2client 未加载: %r" % e}
        server = str(body.get("server") or "real").lower()
        sess = _SESS.get(body.get("sid") or "") or {}
        base = str(body.get("base") or sess.get("base") or "").strip()
        if not base:
            base = (_gdfe_real_base() if server == "real"
                    else (ARGS.mock_base if ARGS else "http://127.0.0.1:18080"))
        token = body.get("token") or sess.get("token")
        if not token:
            lg = _gdfe_login({"server": server, "base": base,
                              "username": body.get("username"),
                              "password": body.get("password"),
                              "device": body.get("device"),
                              "app_version": body.get("app_version"),
                              "run_type": int(body.get("run_type", 1))})
            if not lg.get("ok"):
                return {"ok": False, "needLogin": True,
                        "error": lg.get("error") or "登录失败，无法查询"}
            sess = _SESS.get(lg.get("sid") or "") or {}
            token = sess.get("token")
            base = lg.get("base") or base
        try:
            c = V2C.V2Client(base, school_code=GDFE_SCHOOL, token=token,
                             device=body.get("device"), app_version=body.get("app_version"))
            raw = c.list_records(status=int(body.get("status", 1)),
                                 date=str(body.get("date") or ""),
                                 page=int(body.get("page", 1)))
            try:
                summ = c.sum_()
            except Exception:                                    # noqa: BLE001
                summ = {}
        except Exception as e:                                   # noqa: BLE001
            import traceback
            return {"ok": False, "error": repr(e),
                    "trace": traceback.format_exc()[-1500:]}
        meta = raw if isinstance(raw, dict) else {}
        items = (meta.get("records") or meta.get("rows") or meta.get("list")
                 or meta.get("data") or raw)
        if not isinstance(items, list):
            items = []
        # 给每条记录补一个可读日期（前端不再自己按时区换算）。
        for r in items:
            if isinstance(r, dict) and not r.get("day"):
                r["day"] = _day_of_ms(r.get("startTime"))
        return {"ok": True, "base": base, "records": items, "sum": summ,
                "count": (meta.get("count") if meta else len(items)) or len(items),
                "dates": meta.get("dates") or [],
                "totalRange": meta.get("totalRange"), "totalTime": meta.get("totalTime"),
                "isShowSunRun": meta.get("isShowSunRun"),
                "isShowMorningRun": meta.get("isShowMorningRun"),
                "isShowFreedom": meta.get("isShowFreedom")}


    def _gdfe_today(self, body: dict) -> dict:
        """登录后自动检测：**今天还能不能跑**（只读；不生成 / 不上传 / 不写记录）。

        判据（由强到弱）：
          ① `getExerciseSetting(runType)` 成败 —— 服务器对「现在这条能不能跑」的**权威**答复；
             回 999 时 `msg` 就是原因（今日已达标 / 该类未开放）。
          ② `listExerciseRecord(status=1)` 里有没有落在**今天**的记录 —— 「每日一次」用没用掉。
          ③ `getFeedbackBalance` —— 本学期剩余次数。
          ④ `sumExerciseRecord` —— 三种类型的开放情况 + 已跑次数。
        """
        try:
            import v2client as V2C
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "v2client 未加载: %r" % e}
        server = str(body.get("server") or "real").lower()
        sess = _SESS.get(body.get("sid") or "") or {}
        base = str(body.get("base") or sess.get("base") or "").strip()
        if not base:
            base = (_gdfe_real_base() if server == "real"
                    else (ARGS.mock_base if ARGS else "http://127.0.0.1:18080"))
        token = body.get("token") or sess.get("token")
        if not token:
            lg = _gdfe_login({"server": server, "base": base,
                              "username": body.get("username"),
                              "password": body.get("password"),
                              "device": body.get("device"),
                              "app_version": body.get("app_version"),
                              "run_type": int(body.get("run_type", 1))})
            if not lg.get("ok"):
                return {"ok": False, "needLogin": True,
                        "error": lg.get("error") or "登录失败"}
            sess = _SESS.get(lg.get("sid") or "") or {}
            token = sess.get("token")
            base = lg.get("base") or base
        run_type = int(body.get("run_type", 1))
        try:
            c = V2C.V2Client(base, school_code=GDFE_SCHOOL, token=token,
                             device=body.get("device"), app_version=body.get("app_version"))
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        out = {"ok": True, "base": base, "server": server, "run_type": run_type,
               "run_type_name": _RUN_TYPE_NAME.get(run_type, str(run_type))}
        # ① 服务器权威：这条现在能不能跑
        try:
            c.setting(run_type)
            out["set_ok"] = True
            out["set_msg"] = ""
        except Exception as e:                                   # noqa: BLE001
            out["set_ok"] = False
            _m = (str(e) if getattr(e, "payload", None) is not None else repr(e))
            # 去掉「v2 999: HTTP 999 」这层前缀，只留服务器原话（给页面当提示更干净）。
            _mm = re.match(r"^v2\s*\d+\s*:\s*HTTP\s*\d+\s*(.*)$", _m)
            out["set_msg"] = (_mm.group(1).strip() if _mm else _m)
        # ② 今天的记录
        today = time.strftime("%Y-%m-%d")
        out["today"] = {"date": today, "count": 0, "km": 0.0, "records": []}
        try:
            raw = c.list_records(status=1, date="", page=1)
            recs = (raw.get("records") if isinstance(raw, dict) else raw) or []
            if not isinstance(recs, list):
                recs = []
            out["recent"] = [{"id": r.get("id"), "day": _day_of_ms(r.get("startTime")),
                              "km": round(_num(r.get("sportRange")), 3),
                              "sec": int(_num(r.get("sportTime"))),
                              "remark": r.get("remark")}
                             for r in recs[:8] if isinstance(r, dict)]
            todays = [r for r in recs if isinstance(r, dict)
                      and _day_of_ms(r.get("startTime")) == today]
            out["today"] = {"date": today, "count": len(todays),
                            "km": round(sum(_num(r.get("sportRange")) for r in todays), 3),
                            "records": [{"id": r.get("id"),
                                         "km": round(_num(r.get("sportRange")), 3),
                                         "sec": int(_num(r.get("sportTime"))),
                                         "remark": r.get("remark")} for r in todays]}
        except Exception as e:                                   # noqa: BLE001
            out["recent_err"] = repr(e)
        # ③ 剩余次数
        try:
            out["balance"] = c.get_feedback_balance()
        except Exception as e:                                   # noqa: BLE001
            out["balance_err"] = repr(e)
        # ④ 开放情况
        try:
            out["sum"] = c.sum_()
        except Exception:                                        # noqa: BLE001
            out["sum"] = {}
        # 结论：以服务器的 setting 答复为准，记录 / 余额只做旁证
        msg = out.get("set_msg") or ""
        if out.get("set_ok"):
            out["canRun"] = True
            out["verdict"] = "可以跑"
            out["reason"] = "服务器已开放「%s」" % out["run_type_name"]
        else:
            out["canRun"] = False
            if ("达标" in msg) or ("每日" in msg) or ("每天" in msg):
                out["verdict"] = "今天已经跑过了"
                out["reason"] = msg or "今日成绩已达标"
            elif ("未开放" in msg) or ("未开启" in msg) or ("未开始" in msg):
                out["verdict"] = "这类跑步现在没开放"
                out["reason"] = msg
            else:
                out["verdict"] = "现在跑不了"
                out["reason"] = msg or "服务器拒绝了这条跑步"
        return out

    def _stage_login(self, body: dict) -> dict:
        """**第一步：登录**。只登录 + 拉设置，不生成、不上传、不写任何跑步记录。

        成功后把 session 存进 `_SESS[sid]`，第三步开跑就复用它的 token / identify。
        """
        try:
            import flow as F
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "flow 未加载: %r" % e}
        chain = str(body.get("chain") or "v1").lower()
        base = body.get("base") or (ARGS.mock_base if ARGS else "http://127.0.0.1:18080")
        try:
            r = F.stage_login(chain=chain, base=base,
                              school_code=body.get("school_code") or "gdufe",
                              username=body.get("username") or "20210001",
                              password=(body.get("password") if body.get("password") is not None
                                        else "mock-pass"),
                              run_type=int(body.get("run_type", 1)),
                              device=body.get("device"),
                              app_version=body.get("app_version"))
        except Exception as e:                                   # noqa: BLE001
            import traceback
            return {"ok": False, "chain": chain, "base": base, "error": repr(e),
                    "trace": traceback.format_exc()[-2000:]}
        sess = r.pop("session", None) or {}
        if r.get("ok") and sess:
            import uuid
            sid = uuid.uuid4().hex[:12]
            sess["chain"] = chain
            sess["base"] = base
            _SESS[sid] = sess
            r["sid"] = sid
            r["base"] = base
        return r

    def _stage_run(self, body: dict) -> dict:
        """**第三步：开跑**。开跑时刻 → 生成 → 人脸 → 上传 → 读回 → 汇总。

        带 `sid`（第一步登录过）就复用 token / identify / setting，不重复登录。
        `realtime=true`（简单模式默认）＝**按计划的真实用时跑一遍**：人脸到点发、跑到
        计划结束时刻才上传。这种长任务不能占着 HTTP 请求，所以立刻回一个 `job`，
        前端轮询 `/api/stage/run/status?id=…` 看回执与已跑位置；`/api/stage/run/cancel` 中止
        （`mode=stop` = 停在当前这一刻并按**已跑的那一段**上传，`mode=abort`/缺省 = 什么都不传）。
        """
        try:
            import flow as F
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "flow 未加载: %r" % e}
        sess = _SESS.get(body.get("sid") or "") or {}
        chain = str(body.get("chain") or sess.get("chain") or "v1").lower()
        base = body.get("base") or sess.get("base") or (ARGS.mock_base if ARGS else "http://127.0.0.1:18080")
        params = body.get("params")
        if params is None and body.get("points"):
            params = {"points": body.get("points")}
        kw = dict(chain=chain, base=base, session=sess or None,
                  params=params, gen=body.get("gen"),
                  faces=bool(body.get("faces", True)),
                  run_type=int(body.get("run_type", 1)),
                  face_image_b64=body.get("face_image_b64"),
                  school_code=body.get("school_code") or "gdufe",
                  username=body.get("username") or "20210001",
                  password=(body.get("password") if body.get("password") is not None
                            else "mock-pass"),
                  device_id=body.get("device_id") or "route-studio-device",
                  device=body.get("device"),
                  app_version=body.get("app_version"))
        if body.get("realtime"):
            speed = max(1e-3, float(body.get("speed") or 1.0))
            job = self._new_job(chain=chain, base=base, speed=speed)
            threading.Thread(target=self._run_job, args=(job, kw), daemon=True).start()
            return {"ok": True, "async": True, "job": job["id"], "chain": chain, "base": base,
                    "speed": speed, "realtime": True, "state": "running", "steps": [],
                    "sid_used": bool(body.get("sid") and sess)}
        try:
            r = F.stage_run(**kw)
        except Exception as e:                                   # noqa: BLE001
            import traceback
            return {"ok": False, "chain": chain, "base": base, "error": repr(e),
                    "trace": traceback.format_exc()[-2000:]}
        r["sid_used"] = bool(body.get("sid") and sess)
        return r

    def _new_job(self, *, chain: str, base: str, speed: float) -> dict:
        _job_gc()
        job = {"id": uuid.uuid4().hex[:12], "state": "running", "phase": "准备", "steps": [],
               "result": None, "error": None, "total_ms": 0, "done_ms": 0, "pos": None,
               "cancel": False, "stop": False, "stopped": False,
               "speed": speed, "chain": chain, "base": base,
               "started_at": int(time.time() * 1000)}
        with _JOBS_LOCK:
            _JOBS[job["id"]] = job
        return job

    def _run_job(self, job: dict, kw: dict) -> None:
        """后台线程：跑「实时上传」，边跑边把回执 / 位置 / 进度写进 job。"""
        import flow as F

        def _on_step(it: dict) -> None:
            job["steps"].append(it)

        def _on_prog(snap: dict) -> None:
            job["done_ms"] = int(snap.get("done_ms") or 0)
            if snap.get("total_ms"):
                job["total_ms"] = int(snap["total_ms"])
            if snap.get("phase"):
                job["phase"] = snap["phase"]
            if snap.get("pos"):
                job["pos"] = snap["pos"]

        try:
            r = F.stage_run(realtime=True, speed=job["speed"], progress=_on_prog,
                            on_step=_on_step, cancel=lambda: bool(job["cancel"]),
                            stop=lambda: bool(job["stop"]), **kw)
            job["result"] = r
            job["steps"] = [dict(s) for s in (r.get("steps") or job["steps"])]
            job["stopped"] = bool(r.get("stopped"))
            job["state"] = "done"
            job["phase"] = "已停止（按已跑上传）" if job["stopped"] else "完成"
        except F.Cancelled:
            job["state"] = "cancelled"
            job["phase"] = "已中止"
            job["result"] = {"ok": False, "cancelled": True, "chain": job["chain"],
                             "base": job["base"], "steps": [dict(s) for s in job["steps"]],
                             "error": "已中止：这一条没有上传"}
        except Exception as e:                                   # noqa: BLE001
            import traceback
            job["state"] = "error"
            job["phase"] = "出错"
            job["error"] = repr(e)
            job["result"] = {"ok": False, "chain": job["chain"], "base": job["base"],
                             "steps": [dict(s) for s in job["steps"]], "error": repr(e),
                             "trace": traceback.format_exc()[-2000:]}

    def _submit(self, body: dict) -> dict:
        """按 `chain` 分发：v1=老链路（m.boxkj.com 形态），v2=新链路（学校 cloudUrl）。"""
        chain = str(body.get("chain") or "v1").lower()
        if chain in ("v2", "new", "school"):
            return self._submit_v2(body)
        base = body.get("base") or (ARGS.mock_base if ARGS else "http://127.0.0.1:18080")
        form = dict(body.get("form") or {})
        if not form:
            return {"error": "empty form"}
        if "runType" in form:
            # 老链路 `runType` 用**老编号**（1 晨跑 / 2 阳光跑 / 3 自由跑），而本仓表单是按
            # 新编号（0 自由跑 / 1 阳光跑 / 2 晨跑）生成的 ⇒ 出口处统一换算，别在调用方各写一遍。
            # （同一个坑 2026-09-24 在 flow.py v1 分支踩过：把「旧→新」表用反了。）
            form["runType"] = str(T.v1_run_type(form.get("runType")))
        path = body.get("path") or "/app/appSportRecord/appAddSportRecord"
        try:
            resp = _post_form(base, path, form, token=body.get("token") or "mock-token")
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "chain": "v1", "error": "连接失败: %r（mock 起了吗？）" % e, "base": base}
        return {"ok": True, "chain": "v1", "base": base, "path": path, "response": resp}

    def _submit_v2(self, body: dict) -> dict:
        """新链路：登录 → 配置 → 开跑 → 上传 → 汇总（走 tools/route_studio/v2client.py）。"""
        base = body.get("base") or (ARGS.mock_base if ARGS else "http://127.0.0.1:18080")
        gen = body.get("gen") or {}
        if not gen:
            return {"ok": False, "chain": "v2", "error": "v2 需要 `gen`（/api/generate 的结果）"}
        try:
            import v2client as V2C
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "chain": "v2", "error": "v2client 未加载: %r" % e}
        _sub = gen.get("submit") or {}
        c = V2C.V2Client(base, school_code=body.get("school_code") or "gdufe",
                         raw_device_id=body.get("device_id") or "route-studio-device",
                         token=body.get("token"),
                         device=DP.device_of(_sub) or body.get("device"),
                         app_version=body.get("app_version") or _sub.get("appVersion"))
        run_type = int(body.get("run_type", 1))
        try:
            if not c.token:
                c.login(str(body.get("username") or "20210001"),
                        str(body.get("password") or "mock-pass"))
            bean = c.setting(run_type, body.get("longitude"), body.get("latitude"))
            data, rid = c.add_record(gen, setting=bean, run_type=run_type,
                                     is_face_status=int(body.get("is_face_status", 1)),
                                     upload_type=int(body.get("upload_type", 0)))
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "chain": "v2", "base": base, "error": repr(e),
                    "trace": c.trace}
        return {"ok": True, "chain": "v2", "base": base, "recordId": rid,
                "setting": bean, "response": data, "trace": c.trace}

    def _save(self, body: dict) -> dict:
        os.makedirs(ROUTES_DIR, exist_ok=True)
        name = str(body.get("name") or ("route-%d" % int(time.time()))).strip()
        rid = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60] or "route"
        fp = os.path.join(ROUTES_DIR, "%s.json" % rid)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"name": name, "id": rid, "points": body.get("points") or [],
                       "points_crs": "wgs84",          # 存盘的点**一律**内部 WGS-84（与 params.crs 的「导出/交换口径」分开，免得标错帧）
                       "params": body.get("params") or {},
                       "saved_at": int(time.time())}, f, ensure_ascii=False, indent=1)
        return {"ok": True, "id": rid, "file": os.path.relpath(fp, PROJ)}

    def _import(self, body: dict) -> dict:
        """从 GPX 文本导入路线并落盘。"""
        pts = T.parse_gpx(str(body.get("gpx") or ""))
        name = str(body.get("name") or ("gpx-%d" % int(time.time()))).strip()
        saved = self._save({"name": name, "points": pts, "params": body.get("params") or {}})
        return {"ok": True, "id": saved["id"], "points": pts,
                "count": len(pts), "length_m": round(T.path_length_m(pts), 1),
                "file": saved["file"]}


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--mock-base", default=None,
                    help="自带 mock 的地址（/api/config 的 flow.base 会用它；app.py 启动时传入）")
    ap.add_argument("--edition", default=None, choices=["lite", "full"],
                    help="覆盖版本（默认按可执行文件名判定：含 full ⇒ 完整版）")
    ARGS = ap.parse_args()
    global EDITION
    if ARGS.edition:
        EDITION = ARGS.edition
    os.makedirs(STATIC, exist_ok=True)
    os.makedirs(ROUTES_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), H)
    print("路线工作台： http://%s:%d" % (ARGS.host, ARGS.port), flush=True)
    print("  static=%s" % STATIC, flush=True)
    print("  routes=%s" % ROUTES_DIR, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
