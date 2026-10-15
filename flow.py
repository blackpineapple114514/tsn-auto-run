# -*- coding: utf-8 -*-
"""flow.py —— 一键全流程编排：登录 → 配置 → 开跑 → 人脸 → 上传 → 读回 → 汇总。

给「一键全流程」UI 用：把一条轨迹（或画线点）走完客户端**全部**步骤，并把每步的
**回执**收集成 `steps[]`，前端逐条显示（哪一步成了、拿回了什么）。

两条链路
--------
  v2（默认）＝ 学校 cloudUrl（`v2client.py`）：auth/oauth/token → getExerciseSetting
              → getExerciseStartTime → exerciseRunningFace ×N → addExerciseRecord
              → listExerciseRecord / sumExerciseRecord
  v1        ＝ m.boxkj.com 形态（同 `replay.py`）：getSchoolInfo → appstu/login
              → sportRecordSetting/getSetting → getRunningStartTime
              → exercise/reportEnvironment → runingFace/appRuningFace ×N
              → appSportRecord/appAddSportRecord → appSportRecordList → sumSportRecord

用法
----
  from flow import run_flow
  r = run_flow(chain="v2", base="http://127.0.0.1:18080", params={"points": [...], ...})

  python flow.py --chain v2          # 自起 mock 跑一遍（自测）
  python flow.py --chain v1

⚠️ 纪律：默认只打本仓 mock（`instrumented` 绝不连真实服务器）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS)

import tracklib as T          # noqa: E402
import envlib as E            # noqa: E402
import device_profile as DP   # noqa: E402  （设备 / app 身份的唯一出处）

DEFAULT_BASE = "http://127.0.0.1:18080"
DEFAULT_SCHOOL = "gdufe"
DEFAULT_USER = "20210001"
DEFAULT_PASS = "mock-pass"
DEFAULT_DEVICE_ID = "route-studio-device"
# 1×1 的最小 JPEG（没传真人脸照时的占位，mock 只验签名/字段，不比内容）
FAKE_FACE_JPEG = base64.b64decode(
    b"/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    b"HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    b"AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")


# ---------------------------------------------------------------- 失败回执 / 日志
# 为什么要这段：用户报「真实账号登录不上」，而界面只显示「登录失败」—— 服务器到底说了什么
# （账号错 / 被冻结 / 这所学校根本不认）全被吞在 repr(e) 里。这里把「服务器原话」抬到
# out["error"]/out["server"]（登录页会红字显示），并落一份日志给排障用。
PROJ = os.path.dirname(TOOLS)


def _logdir() -> str:
    """日志目录：打包后 app.py 用 `MOCK_LOGDIR` 指到 exe 旁的数据目录（冻结后 `__file__` 不可信，
    不能拿它推目录）；裸跑就落项目的 `30-logs/`。"""
    d = os.environ.get("MOCK_LOGDIR") or os.path.join(PROJ, "30-logs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _err_raw(e: Exception) -> str:
    """异常里的**服务器原话**：`V2Error.payload`（或 args[2]）优先，dict/list 转 JSON 串。"""
    p = getattr(e, "payload", None)
    if p is None and len(getattr(e, "args", ()) or ()) > 2:
        p = e.args[2]
    if p is None:
        return ""
    if isinstance(p, (dict, list)):
        try:
            return json.dumps(p, ensure_ascii=False)[:800]
        except Exception:                                    # noqa: BLE001
            return repr(p)[:800]
    return str(p)[:800]


def _err_text(e: Exception) -> str:
    """失败回执上写什么：`V2Error` 用 `str(e)`（形如 `v2 426: HTTP 426 用户名或密码错误`，
    已含服务器原话）；其它异常仍旧 `repr(e)`（保持既有回执形态不变）。"""
    return str(e) if getattr(e, "payload", None) is not None else repr(e)


def _v2_run_types(c) -> dict:
    """问服务器「三种跑步类型现在各开没开」（`sumExerciseRecord` 的
    `morningRun/sunRun/freedomRun.isShow`）。

    只在**配置这一步失败**时才问 —— 用来把「早操跑步未开放」这类回执翻译成
    「到底哪一种现在能跑」，省得用户一种一种试。任何异常都吞掉：探测失败
    不该盖住真正的错因（也保证 mock / 假服务器上没有这个端点时不受影响）。
    """
    d = c.sum_() or {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for key, name in (("morningRun", "晨跑"), ("sunRun", "阳光跑"), ("freedomRun", "自由跑")):
        v = d.get(key)
        if isinstance(v, dict):
            v = v.get("isShow")
        if v is None:
            continue
        out[name] = "1" if str(v) in ("1", "True", "true", "是") else "0"
    return out


def _face_summary(info: dict) -> str:
    """把「这所学校要不要扫脸」翻成一行 —— 登录那一步就能看，不用等到开跑。

    读的是设置（`getExerciseSetting` / v1 `getSetting`）里的
    `isStartFace` / `isEndFace` / `isMidwayFace` / `middleFaces`
    （含义与参考实现 `tsnRunServer` 第 347–354 行一致）。
    """
    def on(v) -> bool:
        return str(v) in ("1", "True", "true", "是")

    parts = []
    if on(info.get("is_start_face")):
        parts.append("开始")
    if on(info.get("is_end_face")):
        parts.append("结束")
    mid = info.get("middle_faces") or []
    if on(info.get("is_mid_face")) or mid:
        parts.append("中途×%d" % (len(mid) or 1))
    return (" + ".join(parts) + "（faceType 1/2/3）") if parts else "不需要"


def _write_login_log(kind: str, out: dict, st) -> str:
    """登录/配置这一步的成败落一份到 `<数据目录>/30-logs/route_studio_login.out`。

    **不写密码**；token 只留前 8 位 —— 要的是「哪一步、服务器怎么说」，不是凭据。
    """
    info = out.get("info") or {}
    sess = out.get("session") or {}
    tok = str(sess.get("token") or "")
    L = ["=" * 70,
         "time     : %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
         "kind     : %s" % kind,
         "ok       : %s" % out.get("ok"),
         "chain    : %s" % out.get("chain"),
         "base     : %s" % out.get("base"),
         "school   : %s" % (info.get("school_code") or ""),
         "username : %s" % (info.get("username") or ""),
         "run_type : %s" % (out.get("run_type", "")),
         "runTypes : %s" % (" · ".join("%s=%s" % (k, v)
                                       for k, v in (out.get("runTypes") or {}).items()) or "-"),
         "人脸     : %s" % _face_summary(out.get("info") or {}),
         "人脸底片 : %s" % _album_line(out.get("info") or {}),
         "token    : %s" % (("%s…" % tok[:8]) if tok else "-"),
         "error    : %s" % (out.get("error") or "-"),
         "server   : %s" % (out.get("server") or "-"),
         "steps    :"]
    items = list(st.items) if st is not None else list(out.get("steps") or [])
    for s in items:
        L.append("  - [%s] %s | %s" % ("OK" if s.get("ok") else "FAIL",
                                       s.get("name"), s.get("detail")))
    p = os.path.join(_logdir(), "route_studio_login.out")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    except OSError:
        return ""
    return p


def _fail(out: dict, st, name: str, e: Exception, kind: str = "login") -> dict:
    """记一条失败回执 + 抬出服务器原话 + 落日志（登录/配置失败统一走这里）。"""
    st.add(name, False, _err_text(e))
    out["ok"] = False
    out["error"] = _err_text(e)
    srv = _err_raw(e)
    if srv:
        out["server"] = srv
    out["steps"] = [dict(s) for s in st.items]
    _write_login_log(kind, out, st)
    return out


class Steps:
    """按顺序记每一步的回执。

    `on_add` 是「实时上传」用的旁路：每加一条就当场回调一次，把回执**立刻**推给上层
    （实时模式是一条长任务，不能等全跑完才让前端看见进度）。
    """

    def __init__(self, on_add=None):
        self.items = []
        self.on_add = on_add

    def add(self, name: str, ok: bool, detail: str = "", **extra) -> dict:
        it = {"name": name, "ok": bool(ok), "detail": detail, "ms": int(time.time() * 1000)}
        it.update(extra)
        self.items.append(it)
        if self.on_add:
            try:
                self.on_add(dict(it))
            except Exception:                                # noqa: BLE001
                pass
        return it

    def names(self):
        return [s["name"] for s in self.items]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Cancelled(Exception):
    """实时上传途中被「中止」——上层据此标 cancelled，不算失败。"""


def _wait_stop_tail(pacer, gen, cancel=None) -> None:
    """「中途停止」之后仍有 ~33 s 收尾段（真机也记：人还在走、计时已停）。

    墙钟必须**真等**这一段，否则上传时刻早于轨迹 ``endTime``（结束时间落在未来）
    ⇒ 平台判「数据异常-作弊嫌疑」。倍速模式下按 ``pacer.speed`` 折算（测试用），
    中止按钮照样能打断它。
    """
    if not pacer or not pacer.stopped or not isinstance(gen, dict):
        return
    tail_ms = float(gen.get("stop_tail_ms") or 0.0)
    if tail_ms <= 0:
        return
    t1 = time.time() + tail_ms / 1000.0 / pacer.speed
    while True:
        if cancel and cancel():
            raise Cancelled()
        remain = t1 - time.time()
        if remain <= 0:
            break
        time.sleep(min(pacer.TICK_S, remain))


class Pacer:
    """「实时上传」的节奏器：把**轨迹时间轴**映射到墙钟，并随时报出跑到哪了。

    - `speed=1` 就是真速度（计划跑 620 s 就真等 620 s）；测试用大倍速把十几分钟压成几秒。
    - `wait_until(offset_ms)` 等到「起跑后 offset_ms」那一刻；到点时把
      `progress(snap)` 回调一次（带 `pos`＝当前坐标，前端拿它画「跑到哪」的那个点）。
    - `cancel()` 返回真就立刻抛 `Cancelled`（中止按钮在等待中间也要能生效）。
    - `stop()` 返回真则**停在当前这一刻**（不抛异常）：时间轴钉死、之后所有等待立刻返回，
      上层据此把轨迹截到 `cut_ms`、按已跑的那一段上传（「停止并上传」）。
    """

    TICK_S = 0.2

    def __init__(self, *, start_ms: int = 0, speed: float = 1.0, progress=None,
                 cancel=None, stop=None, samples=None, total_m: float = 0.0):
        self.start_ms = int(start_ms or 0)
        self.speed = max(1e-3, float(speed or 1.0))
        self.progress = progress
        self.cancel = cancel or (lambda: False)
        self.stop = stop or (lambda: False)
        # 「中途停止（并上传）」：stop() 为真就把时间轴**钉死**在这一刻，之后不再往前走；
        # cut_ms = 那一刻的**绝对墙钟毫秒**（与 samples 的 t_ms 同一套口径，直接喂
        # tracklib.truncate）⇒ 就是「真正跑完的那一段」的终点。
        self.stopped = False
        self.cut_ms = None
        self.total_m = float(total_m or 0.0)
        self.t0 = _now_ms()
        self.total_ms = 0
        # (t_ms, lat, lng, 累计米, 速度 m/s, 是否暂停)——时间轴与位置一一对应
        self.track = [(int(s.get("t_ms") or 0), float(s["lat"]), float(s["lng"]),
                       float(s.get("cumulative_m") or 0.0), float(s.get("speed_mps") or 0.0),
                       bool(s.get("paused")))
                      for s in (samples or []) if s.get("lat") is not None and s.get("lng") is not None]

    def begin(self, total_ms: int) -> None:
        self.total_ms = max(0, int(total_ms or 0))
        self.t0 = _now_ms()
        self._emit("")

    def done_ms(self) -> int:
        """已经「跑过」的计划时间（把真实等待按倍速折算回轨迹时间轴）。"""
        d = int((_now_ms() - self.t0) * self.speed)
        return min(d, int(self.cut_ms) - int(self.start_ms)) if self.cut_ms is not None else d

    def _freeze_if_stopped(self) -> bool:
        """「中途停止」：把时间轴钉在当前这一刻；之后所有等待立刻返回（不再往前走）。"""
        if self.stopped:
            return True
        if self.stop():
            self.stopped = True
            self.cut_ms = int(self.start_ms) + int((_now_ms() - self.t0) * self.speed)
            self._emit("已停止")
            return True
        return False

    @property
    def cut_offset_ms(self):
        """停下时在**轨迹时间轴**上跑到哪了（相对起点，ms）；没停就是 None。"""
        return None if self.cut_ms is None else max(0, int(self.cut_ms) - int(self.start_ms))

    def cut_pos(self):
        """停下那一刻的位置（与 progress 的 pos 同一套坐标系）；没停就是 None。"""
        return None if self.cut_ms is None else self.pos_at(self.cut_offset_ms)

    def wait_until(self, offset_ms: int, label: str = "") -> None:
        target = self.t0 + int(max(0, int(offset_ms or 0)) / self.speed)
        while True:
            if self.cancel():
                raise Cancelled()
            if self._freeze_if_stopped():
                return
            remain = target - _now_ms()
            if remain <= 0:
                break
            time.sleep(min(self.TICK_S, remain / 1000.0))
            self._emit(label)
        self._emit(label)

    def pos_at(self, done_ms: int):
        """轨迹时间轴上 `done_ms` 处的坐标（WGS-84，与画布同一套）＋已跑距离。"""
        if not self.track:
            return None
        t = self.start_ms + int(done_ms or 0)
        lo, hi = 0, len(self.track) - 1
        while lo < hi:                                       # 二分：最后一个 t_ms <= t
            mid = (lo + hi + 1) // 2
            if self.track[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        _, lat, lng, cum, spd, paused = self.track[lo]
        return {"lat": lat, "lng": lng, "idx": lo, "n": len(self.track), "t_ms": t,
                "cum_m": cum, "total_m": self.total_m, "speed_mps": spd, "paused": paused}

    def snap(self, label: str = "") -> dict:
        d = self.done_ms()
        return {"done_ms": d, "total_ms": self.total_ms, "phase": label,
                "speed": self.speed, "stopped": self.stopped, "pos": self.pos_at(d)}

    def _emit(self, label: str) -> None:
        if not self.progress:
            return
        try:
            self.progress(self.snap(label))
        except Exception:                                    # noqa: BLE001
            pass


def _face_bytes(face_image_b64=None, face_image: bytes = None) -> bytes:
    if face_image:
        return face_image
    if face_image_b64:
        try:
            return base64.b64decode(face_image_b64)
        except Exception:                                    # noqa: BLE001
            pass
    return FAKE_FACE_JPEG


def _face_image_for(c, *, face_image=None, face_image_b64=None, bas_user_id=None):
    """要发的人脸图 + 一句来源说明。优先级：你给的 > 服务器已注册底片（加噪）> 占位图。

    「服务器已注册底片」= 参考实现 `getFaceImage()` 那条路：拿**账号自己的底片**去打卡，
    服务器的 1:1 比对才过得了（只发占位图时，真实学校会判**不通过**）。
    """
    import v2client as V2C
    if face_image or face_image_b64:
        return _face_bytes(face_image_b64, face_image), "你提供的人脸照"
    uid = bas_user_id
    if not uid:
        try:
            ui = c.user_info()
            uid = ui.get("basUserId") or ui.get("id") or ui.get("userId")
        except Exception:                                    # noqa: BLE001
            uid = None
    if uid:
        try:
            got = c.fetch_face_image(uid)
        except Exception:                                    # noqa: BLE001
            got = None
        if got:
            return V2C.add_pixel_noise(got), "服务器已注册底片（加噪）"
    return FAKE_FACE_JPEG, "占位图（未提供人脸照、也没取到底片）"


def _probe_face_album(c, identify=None) -> tuple:
    """问一句「本人已注册人脸底片有几张」⇒ (bas_user_id, {"ok":bool,"count":int,…})。

    只有要扫脸的学校才值得问；异常全吞 —— 探不到不能挡登录（顶多让开跑时退回占位图）。
    """
    uid = None
    try:
        ui = c.user_info()
        uid = ui.get("basUserId") or ui.get("id") or ui.get("userId")
    except Exception as e:                                    # noqa: BLE001
        return None, {"ok": False, "count": 0, "error": str(e)[:120]}
    if not uid:
        return None, {"ok": False, "count": 0, "error": "服务器没给 user id"}
    try:
        lst = c.list_bas_user_image_face(uid)
    except Exception as e:                                    # noqa: BLE001
        return uid, {"ok": False, "count": 0, "error": str(e)[:120]}
    return uid, {"ok": bool(lst), "count": len(lst)}


def _album_line(info: dict) -> str:
    """登录日志里的「人脸底片」一行：取到几张 / 为什么没取到。"""
    al = info.get("face_album")
    if not isinstance(al, dict):
        return "-"
    if al.get("ok"):
        return "已取到 %d 张（打卡用它）" % int(al.get("count") or 0)
    return "未取到（%s）" % (al.get("error") or "服务器没给底片")


def _first_point(gen: dict):
    """从生成结果里取首个导出坐标（GCJ-02，app 用的那套），给 getSetting 当「当前位置」。"""
    pts = gen.get("track_pointdata") or []
    if pts and isinstance(pts[0], dict):
        p = pts[0]
        lat = p.get("a", p.get("lat"))
        lng = p.get("o", p.get("lng"))
        if lat is not None and lng is not None:
            return float(lng), float(lat)
    meta = gen.get("metrics") or {}
    return meta.get("start_lng"), meta.get("start_lat")


def _gen_from(params: dict, *, start_ms=None, identify=None, run_type=1) -> dict:
    """按 /api/generate 的 body 生成一条轨迹（与 server.generate_track 同一份逻辑）。"""
    import server as S                                   # 延迟导入，避免与 server 循环
    return S.generate_track(params, start_ms=start_ms, identify=identify, run_type=run_type)


# ============================================================ v2（学校 cloudUrl）
def run_flow_v2(*, base, school_code, username, password, params=None, gen=None,
                faces=True, run_type=1, face_image=None, face_image_b64=None,
                device_id=DEFAULT_DEVICE_ID, steps=None, timeout=20.0, session=None,
                realtime=False, speed=1.0, progress=None, cancel=None, stop=None,
                device=None, app_version=None) -> dict:
    """学校 cloudUrl 那条链路。`realtime=True` 时**按计划的真实用时跑**：
    人脸打卡在它该出现的那一刻发，跑到计划结束时刻才 `addExerciseRecord`
    （墙钟与上传的 `endTime` 对齐），中途用 `progress(snap)` 报位置与进度。
    `stop()` 返回真＝**停在当前这一刻并按已跑的那一段上传**（见 tracklib.truncate）。"""
    import v2client as V2C
    st = steps or Steps()
    # 设备 / app 身份：显式 device > gen 表单里的设备四件套 > params.device > 默认（真机 iPhone）
    _dev_in = device
    _appv = app_version
    if _dev_in is None and gen:
        _dev_in = DP.device_of(gen.get("submit")) or None
        if _appv is None:
            _appv = (gen.get("submit") or {}).get("appVersion")
    if _dev_in is None and params:
        _dev_in = params.get("device") or None
        if _appv is None:
            _appv = params.get("app_version")
    c = V2C.V2Client(base, school_code=school_code or DEFAULT_SCHOOL,
                     raw_device_id=device_id or DEFAULT_DEVICE_ID, timeout=timeout,
                     device=_dev_in, app_version=_appv)
    out = {"chain": "v2", "base": base, "steps": st, "school_code": school_code,
           "realtime": bool(realtime)}

    sess = session or {}
    _s_ident = sess.get("identify") or (sess.get("setting") or {}).get("identify")
    if sess.get("token") and _s_ident:
        # 第一步已经登录过：复用 token / setting，这里不再重复登录（向导「一环扣一环」）
        c.token = sess["token"]
        bean = sess.get("setting") or {}
        identify = _s_ident
        st.add("登录（复用第一步 token）", True, "token=%s…" % (c.token or "")[:12])
        st.add("配置（复用第一步 setting）", True, "identify=%s" % identify)
    else:
        # ① 登录
        try:
            c.login(username or DEFAULT_USER, password if password is not None else DEFAULT_PASS)
            st.add("登录 auth/oauth/token", True, "拿到 Bearer token（%s…）" % (c.token or "")[:12])
        except Exception as e:                                   # noqa: BLE001
            _fail(out, st, "登录 auth/oauth/token", e, kind="run-login")
            return out

        # ② 配置
        try:
            bean = c.setting(run_type, None, None)
            identify = bean.get("identify")
            tr = float(bean.get("totalRange") or 0)          # v2 的 totalRange 单位就是 km
            st.add("配置 getExerciseSetting", bool(identify),
                   "identify=%s；目标 %.2f km / 时长门槛 %s 分 / 限速 %s" % (
                       identify, (tr / 1000.0 if tr > 100 else tr),
                       bean.get("startQualifiedTime"), bean.get("limitSpeed")),
                   setting={k: bean.get(k) for k in
                            ("identify", "totalRange", "startQualifiedTime", "step",
                             "limitSpeed", "endStride", "endLimitStepFrequency",
                             "isSequencePoint", "isStartFace", "isEndFace", "geofence")})
        except Exception as e:                                   # noqa: BLE001
            _fail(out, st, "配置 getExerciseSetting", e, kind="run-setting")
            return out
    # ③ 开跑时刻
    try:
        start_ms = c.start_time(identify) or int(time.time() * 1000)
        st.add("开跑时刻 getExerciseStartTime", start_ms > 0, "startTime=%d" % start_ms)
    except Exception as e:                                   # noqa: BLE001
        st.add("开跑时刻 getExerciseStartTime", False, repr(e))
        start_ms = int(time.time() * 1000)

    # ④ 生成轨迹（identify / startTime 已就位）
    if gen is None:
        params = dict(params or {})
        try:
            gen = _gen_from(params, start_ms=start_ms, identify=identify, run_type=run_type)
            m = gen["metrics"]
            st.add("生成轨迹", True, "%.1f m / %d 点 / 用时 %d s / %s" % (
                m["distance_m"], m["point_count"], m["elapsed_s"], gen.get("loop_mode")))
        except Exception as e:                               # noqa: BLE001
            st.add("生成轨迹", False, repr(e))
            out["ok"] = False
            return out
    out["gen"] = {k: gen.get(k) for k in ("metrics", "validity", "loop_mode")}

    # ④.5 实时上传：按计划的真实用时跑（不勾就照旧立刻跑完、立刻上传）
    _m = gen.get("metrics") or {}
    plan_ms = int(round(float(_m.get("elapsed_s") or 0) * 1000.0))
    pacer = None
    if realtime:
        pacer = Pacer(start_ms=start_ms, speed=speed, progress=progress, cancel=cancel,
                      stop=stop, samples=gen.get("samples"),
                      total_m=float(_m.get("distance_m") or 0.0))
        pacer.begin(plan_ms)
        st.add("实时上传（按真实用时）", True,
               "×%.2f：计划 %d s / %d 点 —— 人脸与上传都在各自时间点发出（不勾就是立刻跑完）"
               % (pacer.speed, plan_ms // 1000, int(_m.get("point_count") or 0)))

    # ⑤ 人脸打卡（开始 / 中途 / 结束）
    img, _face_src = _face_image_for(c, face_image=face_image, face_image_b64=face_image_b64,
                                     bas_user_id=sess.get("bas_user_id"))
    face_ok = 0
    if faces:
        for f in gen.get("faces") or []:
            if pacer:                                        # 等到这个人脸该出现的那一刻
                pacer.wait_until(int(f.get("timestamp") or 0) - int(start_ms or 0),
                                 "人脸 faceType=%s" % f.get("type"))
                if pacer.cut_ms is not None and int(f.get("timestamp") or 0) > int(pacer.cut_ms):
                    continue                                 # 已经停了：这个点属于没跑到的那段，不发
            try:
                j = c.face(img, f.get("coordinates") or "", identify,
                           run_type=run_type, face_type=int(f.get("type") or 1))
                if j.get("code") == 0:
                    face_ok += 1
                st.add("人脸打卡 faceType=%s" % f.get("type"), j.get("code") == 0,
                       "坐标 %s（%d B 图片 · %s）" % (f.get("coordinates"), len(img), _face_src))
            except Exception as e:                           # noqa: BLE001
                st.add("人脸打卡 faceType=%s" % f.get("type"), False, repr(e))
        if not (gen.get("faces") or []):
            st.add("人脸打卡", True, "本次轨迹没带人脸点（设置里没勾开始/结束/中途）")
    else:
        st.add("人脸打卡", True, "已关闭（faces=False）")

    # ⑥ 上传（实时：跑到**轨迹真实结束**那一刻才发，墙钟与 endTime 对齐）
    if pacer:
        # 只等 plan_ms（= 有效跑动时长）是不够的：轨迹墙钟跨度还要含**中途暂停**与**收尾段**
        # （本路线 1155.4 s vs 1248.4 s，差 93 s）。等不够 ⇒ 上传时刻早于 endTime，
        # `addTime < endTime`（结束时间在**未来**）⇒ 平台判「数据异常-作弊嫌疑」。
        _endoff = max(plan_ms, int(round(float(_m.get("wall_s") or 0) * 1000.0)))
        pacer.wait_until(_endoff, "上传")             # 中途停止的话这里立刻返回

    # ⑥.5 中途停止：**等完所有等待之后**才截断 —— 停止也可能发生在上面这段「等上传」里
    #      （跑过最后一帧人脸之后的那一段最长），所以必须在**上传之前**再判一次；
    #      否则会把整条计划全程传上去（表症：状态条说「已停止（按已跑上传）」、传的是全程）。
    # ⑤.5 中途停止：把轨迹截到停下的那一刻，「按已跑的那一段」重算 + 补发结束人脸
    cut_gen = None
    if pacer and pacer.stopped:
        src_faces = [dict(f) for f in (gen.get("faces") or [])]
        cut_gen = T.truncate(gen, pacer.cut_ms)
        if cut_gen is None:
            st.add("上传 addExerciseRecord", False,
                   "刚起跑就停了（还没跑出一个采样点），这一条没上传")
            out["stopped"] = True
            out["plan_ms"] = plan_ms
            out["plan_m"] = float(_m.get("distance_m") or 0.0)
            out["run_m"] = 0.0
            out["cut_ms"] = pacer.cut_ms
            out["ok"] = False
            return out
        gen = cut_gen
        out["gen"] = {k: gen.get(k) for k in ("metrics", "validity", "loop_mode")}
        st.add("中途停止", True,
               "停在计划 %d s 的第 %d s —— 只按已跑 %.0f m 重算（不臆造没跑的距离）"
               % (plan_ms // 1000, int(pacer.done_ms()) // 1000,
                  float(gen["metrics"]["distance_m"])))
        if faces and any(int(f.get("type") or 0) == 3
                         and int(f.get("timestamp") or 0) > int(pacer.cut_ms) for f in src_faces):
            ef = next((f for f in (gen.get("faces") or []) if int(f.get("type") or 0) == 3), None)
            if ef:
                try:
                    j = c.face(img, ef.get("coordinates") or "", identify,
                               run_type=run_type, face_type=3)
                    st.add("人脸打卡 faceType=3（停止时补）", j.get("code") == 0,
                           "坐标 %s（%s）" % (ef.get("coordinates"), _face_src))
                except Exception as e:                       # noqa: BLE001
                    st.add("人脸打卡 faceType=3（停止时补）", False, repr(e))

    # ⑥.6 收尾段（计时停止后的 ~33 s 步行，真机也记）：墙钟要真等这一段，
    #      否则上传时刻早于 endTime（结束时间落在未来）⇒ 又踩「数据异常」。
    _wait_stop_tail(pacer, gen, cancel)

    _rt = ""
    if pacer and not pacer.stopped:
        _rt = ("；实时 ×%.2f：跑到计划点才上传（节奏偏差 %+d ms）"
               % (pacer.speed, pacer.done_ms() - plan_ms))
    elif cut_gen is not None:
        _rt = ("；中途停止 ×%.2f：只按已跑 %.0f m / %.0f m 上传"
               % (pacer.speed, float(gen["metrics"]["distance_m"]),
                  float(_m.get("distance_m") or 0.0)))
    try:
        data, rid = c.add_record(gen, setting=bean, run_type=run_type,
                                 is_face_status=1 if faces else 0)
        st.add("上传 addExerciseRecord", rid is not None,
               "recordId=%s；风险标志 d/f/m/h=%s%s" % (rid, [t for t in c.trace
                                                          if t.get("step") == "addExerciseRecord"][-1:], _rt))
        out["recordId"] = rid
    except Exception as e:                                   # noqa: BLE001
        st.add("上传 addExerciseRecord", False, repr(e))
        out["ok"] = False
        return out

    # ⑦ 读回
    try:
        rows = c.list_records() or {}
        n = len(rows.get("records") or rows.get("rows") or []) if isinstance(rows, dict) else len(rows or [])
        st.add("读回 listExerciseRecord", n >= 1, "读到 %d 条记录（闭环）" % n)
        out["readback"] = {"count": n}
    except Exception as e:                                   # noqa: BLE001
        st.add("读回 listExerciseRecord", False, repr(e))
    try:
        s = c.sum_()
        st.add("汇总 sumExerciseRecord", True, json.dumps(s, ensure_ascii=False)[:120])
        out["sum"] = s
    except Exception as e:                                   # noqa: BLE001
        st.add("汇总 sumExerciseRecord", False, repr(e))

    out["plan_ms"] = plan_ms
    out["speed"] = (pacer.speed if pacer else 1.0)
    out["stopped"] = bool(pacer and pacer.stopped)
    out["plan_m"] = float(_m.get("distance_m") or 0.0)
    out["run_m"] = float((gen.get("metrics") or {}).get("distance_m") or 0.0)
    if pacer and pacer.stopped:
        out["cut_ms"] = pacer.cut_ms
    out["ok"] = all(x["ok"] for x in st.items)
    out["faces_ok"] = face_ok
    return out


# ============================================================ v1（m.boxkj.com 形态）
def run_flow_v1(*, base, params=None, gen=None, faces=True, run_type=1,
                face_image_b64=None, school_id="184", steps=None,
                school_code=DEFAULT_SCHOOL, username=None, password=None,
                timeout=20.0, session=None, realtime=False, speed=1.0,
                progress=None, cancel=None, stop=None, device=None) -> dict:
    """`m.boxkj.com` 形态那条链路。`realtime=True` 时按计划的真实用时跑（同 v2）。"""
    import server as S
    v1_type = T.v1_run_type(run_type)   # 老链路 runType：1 晨跑 / 2 阳光跑 / 3 自由跑
    st = steps or Steps()
    out = {"chain": "v1", "base": base, "steps": st}
    post = lambda path, form, token="mock-token": S._post_form(base, path, form, token=token)  # noqa: E731

    sess = session or {}
    token = sess.get("token")
    user_id = sess.get("user_id")
    identify = sess.get("identify") or (sess.get("setting") or {}).get("identify")
    cfg = sess.get("setting") or {}
    if token and identify:
        # 第一步已经登录过：复用 token / identify / setting（向导「一环扣一环」）
        st.add("登录（复用第一步 token）", True, "token=%s…" % (token or "")[:12])
        st.add("配置（复用第一步 setting）", True, "identify=%s" % identify)
    else:
        # ① 选校
        try:
            r = post("/app/getSchoolInfo",
                     {"schoolId": school_id,
                      "model": (device or {}).get("model") or DP.DEFAULT_DEVICE["model"]})
            d = r.get("data") or {}
            st.add("选校 getSchoolInfo", str(d.get("sysType")) == "2",
                   "schoolCode=%s / sysType=%s ⇒ 老链路" % (d.get("schoolCode"), d.get("sysType")))
        except Exception as e:                                   # noqa: BLE001
            st.add("选校 getSchoolInfo", False, repr(e))
            out["ok"] = False
            return out

        # ② 登录
        try:
            r = post("/app/appstu/login", {"uname": username or DEFAULT_USER,
                                           "pwd": password if password is not None else DEFAULT_PASS})
            d = r.get("data") or {}
            token = d.get("token")
            user_id = d.get("id") or username or DEFAULT_USER
            st.add("登录 appstu/login", bool(token), "token=%s…" % (token or "")[:12])
        except Exception as e:                                   # noqa: BLE001
            st.add("登录 appstu/login", False, repr(e))
            out["ok"] = False
            return out

        # ③ 配置
        try:
            r = post("/app/sportRecordSetting/getSetting",
                     {"runType": str(v1_type), "uid": str(user_id)})
            cfg = r.get("data") or {}
            identify = cfg.get("identify") or "identify-%d" % int(time.time())
            st.add("配置 sportRecordSetting/getSetting", bool(cfg.get("identify")),
                   "identify=%s" % identify)
        except Exception as e:                                   # noqa: BLE001
            st.add("配置 sportRecordSetting/getSetting", False, repr(e))
            out["ok"] = False
            return out
    # ④ 开跑时刻
    try:
        r = post("/app/sportRecordSetting/getRunningStartTime", {"identify": identify})
        start_ms = int((r.get("data") or {}).get("startTime") or int(time.time() * 1000))
        st.add("开跑时刻 getRunningStartTime", start_ms > 0, "startTime=%d" % start_ms)
    except Exception as e:                                   # noqa: BLE001
        st.add("开跑时刻 getRunningStartTime", False, repr(e))
        start_ms = int(time.time() * 1000)

    # ⑤ 环境真实性上报
    try:
        env = E.clean_payload(device_id=DEFAULT_DEVICE_ID, abis=["arm64-v8a"])
        chk = E.verify_payload(env)
        r = post("/app/exercise/reportEnvironment", dict(env, identify=identify))
        ok_env = bool((r.get("data") or {}).get("ok"))
        st.add("环境上报 reportEnvironment", ok_env and chk["ok"],
               "checksum 自洽=%s / mock 收下=%s" % (chk["ok"], ok_env))
    except Exception as e:                                   # noqa: BLE001
        st.add("环境上报 reportEnvironment", False, repr(e))

    # ⑥ 生成轨迹
    if gen is None:
        params = dict(params or {})
        try:
            gen = _gen_from(params, start_ms=start_ms, identify=identify, run_type=run_type)
            m = gen["metrics"]
            st.add("生成轨迹", True, "%.1f m / %d 点 / 用时 %d s" % (
                m["distance_m"], m["point_count"], m["elapsed_s"]))
        except Exception as e:                               # noqa: BLE001
            st.add("生成轨迹", False, repr(e))
            out["ok"] = False
            return out
    out["gen"] = {k: gen.get(k) for k in ("metrics", "validity", "loop_mode")}

    # ⑥.5 实时上传：按计划的真实用时跑（不勾就照旧立刻跑完、立刻上传）
    _m = gen.get("metrics") or {}
    plan_ms = int(round(float(_m.get("elapsed_s") or 0) * 1000.0))
    pacer = None
    if realtime:
        pacer = Pacer(start_ms=start_ms, speed=speed, progress=progress, cancel=cancel,
                      stop=stop, samples=gen.get("samples"),
                      total_m=float(_m.get("distance_m") or 0.0))
        pacer.begin(plan_ms)
        st.add("实时上传（按真实用时）", True,
               "×%.2f：计划 %d s / %d 点 —— 人脸与上传都在各自时间点发出（不勾就是立刻跑完）"
               % (pacer.speed, plan_ms // 1000, int(_m.get("point_count") or 0)))

    # ⑦ 人脸打卡
    face_ok = 0
    if faces:
        for f in gen.get("faces") or []:
            if pacer:                                        # 等到这个人脸该出现的那一刻
                pacer.wait_until(int(f.get("timestamp") or 0) - int(start_ms or 0),
                                 "人脸 faceType=%s" % f.get("type"))
                if pacer.cut_ms is not None and int(f.get("timestamp") or 0) > int(pacer.cut_ms):
                    continue                                 # 已经停了：这个点属于没跑到的那段，不发
            try:
                rr = post("/app/runingFace/appRuningFace",
                          {"identify": identify, "type": str(f["type"]),
                           "coordinates": f["coordinates"], "runType": str(v1_type)}, token=token)
                ok = str(rr.get("returnCode")) == "200"
                face_ok += 1 if ok else 0
                st.add("人脸打卡 faceType=%s" % f["type"], ok, "坐标 %s" % f["coordinates"])
            except Exception as e:                           # noqa: BLE001
                st.add("人脸打卡 faceType=%s" % f["type"], False, repr(e))
    else:
        st.add("人脸打卡", True, "已关闭（faces=False）")

    # ⑧ 上传（实时：跑到**轨迹真实结束**那一刻才发，墙钟与 endTime 对齐）
    if pacer:
        # 口径同 v2：只等 plan_ms（有效跑动时长）会漏掉中途暂停与收尾段
        _endoff = max(plan_ms, int(round(float(_m.get("wall_s") or 0) * 1000.0)))
        pacer.wait_until(_endoff, "上传")             # 中途停止的话这里立刻返回

    # ⑧.5 中途停止：**等完所有等待之后**才截断 —— 停止也可能发生在上面这段「等上传」里
    #      （跑过最后一帧人脸之后的那一段最长），所以必须在**上传之前**再判一次；
    #      否则会把整条计划全程传上去（表症：状态条说「已停止（按已跑上传）」、传的是全程）。
    # ⑦.5 中途停止：把轨迹截到停下的那一刻（口径同 v2）
    cut_gen = None
    if pacer and pacer.stopped:
        src_faces = [dict(f) for f in (gen.get("faces") or [])]
        cut_gen = T.truncate(gen, pacer.cut_ms)
        if cut_gen is None:
            st.add("上传 appAddSportRecord", False,
                   "刚起跑就停了（还没跑出一个采样点），这一条没上传")
            out["stopped"] = True
            out["plan_ms"] = plan_ms
            out["plan_m"] = float(_m.get("distance_m") or 0.0)
            out["run_m"] = 0.0
            out["cut_ms"] = pacer.cut_ms
            out["ok"] = False
            return out
        gen = cut_gen
        out["gen"] = {k: gen.get(k) for k in ("metrics", "validity", "loop_mode")}
        st.add("中途停止", True,
               "停在计划 %d s 的第 %d s —— 只按已跑 %.0f m 重算（不臆造没跑的距离）"
               % (plan_ms // 1000, int(pacer.done_ms()) // 1000,
                  float(gen["metrics"]["distance_m"])))
        if faces and any(int(f.get("type") or 0) == 3
                         and int(f.get("timestamp") or 0) > int(pacer.cut_ms) for f in src_faces):
            ef = next((f for f in (gen.get("faces") or []) if int(f.get("type") or 0) == 3), None)
            if ef:
                try:
                    rr = post("/app/runingFace/appRuningFace",
                              {"identify": identify, "type": "3",
                               "coordinates": ef["coordinates"], "runType": str(v1_type)},
                              token=token)
                    st.add("人脸打卡 faceType=3（停止时补）",
                           str(rr.get("returnCode")) == "200", "坐标 %s" % ef["coordinates"])
                except Exception as e:                       # noqa: BLE001
                    st.add("人脸打卡 faceType=3（停止时补）", False, repr(e))

    # ⑧.6 收尾段（口径同 v2）：墙钟要真等这 ~33 s 步行段，否则 endTime 落在未来。
    _wait_stop_tail(pacer, gen, cancel)

    _rt = ""
    if pacer and not pacer.stopped:
        _rt = ("；实时 ×%.2f：跑到计划点才上传（节奏偏差 %+d ms）"
               % (pacer.speed, pacer.done_ms() - plan_ms))
    elif cut_gen is not None:
        _rt = ("；中途停止 ×%.2f：只按已跑 %.0f m / %.0f m 上传"
               % (pacer.speed, float(gen["metrics"]["distance_m"]),
                  float(_m.get("distance_m") or 0.0)))
    try:
        _body_v1 = dict(gen["submit"])
        _body_v1["runType"] = str(v1_type)   # 老链路字段用老编号（新编号只在本仓内部流转）
        r = post("/app/appSportRecord/appAddSportRecord", _body_v1, token=token)
        ok = str(r.get("returnCode")) == "200"
        st.add("上传 appAddSportRecord", ok,
               "returnCode=%s / 距离 %s km%s" % (r.get("returnCode"),
                                               gen["submit"].get("formatSportRange"), _rt))
        if not ok:
            out["ok"] = False
            return out
    except Exception as e:                                   # noqa: BLE001
        st.add("上传 appAddSportRecord", False, repr(e))
        out["ok"] = False
        return out

    # ⑨ 读回 + 汇总（userId 必须用**上传 payload 里那个**，不是登录回包的 id）
    try:
        r = post("/app/appSportRecord/appSportRecordList",
                 {"userId": str(gen["submit"].get("userId") or user_id),
                  "sportType": str(v1_type),
                  "pageIndex": "1", "pageSize": "10"})
        rows = (r.get("data") or {}).get("rows") or []
        mine = [x for x in rows if x.get("identify") == identify]
        want = str(gen["submit"].get("formatSportRange"))
        # v1 的 identify 是 mock 固定的（同一个 mock 跑第二次就会命中多条）：
        # 所以闭环证据取「命中>=1 且本次那条（同距离）真的在里面」，而不是死磕 == 1
        hit = bool(mine) and any(str(x.get("formatSportRange")) == want for x in mine)
        st.add("读回 appSportRecordList", hit,
               "命中 %d 条（本次 %s 也在）/ 共 %d 条（闭环）" % (len(mine), want, len(rows)))
        out["readback"] = {"hit": len(mine), "rows": len(rows)}
    except Exception as e:                                   # noqa: BLE001
        st.add("读回 appSportRecordList", False, repr(e))
    try:
        r = post("/app/appSportRecord/sumSportRecord", {"userId": str(gen["submit"].get("userId") or user_id)})
        d = r.get("data") or {}
        st.add("汇总 sumSportRecord", (d.get("sumCount") or 0) >= 1, json.dumps(d, ensure_ascii=False)[:120])
        out["sum"] = d
    except Exception as e:                                   # noqa: BLE001
        st.add("汇总 sumSportRecord", False, repr(e))

    out["plan_ms"] = plan_ms
    out["speed"] = (pacer.speed if pacer else 1.0)
    out["stopped"] = bool(pacer and pacer.stopped)
    out["plan_m"] = float(_m.get("distance_m") or 0.0)
    out["run_m"] = float((gen.get("metrics") or {}).get("distance_m") or 0.0)
    if pacer and pacer.stopped:
        out["cut_ms"] = pacer.cut_ms
    out["ok"] = all(x["ok"] for x in st.items)
    out["faces_ok"] = face_ok
    return out


# ============================================================ 统一入口
def run_flow(*, chain="v2", base=DEFAULT_BASE, school_code=DEFAULT_SCHOOL,
             username=DEFAULT_USER, password=DEFAULT_PASS, params=None, gen=None,
             faces=True, run_type=1, face_image_b64=None, face_image=None,
             device_id=DEFAULT_DEVICE_ID, steps=None, session=None,
             device=None, app_version=None) -> dict:
    """跑一遍全流程。params = /api/generate 的 body（含 points）；给了 gen 就跳过生成。"""
    chain = (chain or "v2").lower()
    st = steps or Steps()
    if chain in ("v2", "new", "school"):
        r = run_flow_v2(base=base, school_code=school_code, username=username,
                        password=password, params=params, gen=gen, faces=faces,
                        run_type=run_type, face_image=face_image,
                        face_image_b64=face_image_b64, device_id=device_id, steps=st,
                        session=session, device=device, app_version=app_version)
    else:
        r = run_flow_v1(base=base, params=params, gen=gen, faces=faces, run_type=run_type,
                        face_image_b64=face_image_b64, school_code=school_code,
                        username=username, password=password, steps=st, session=session,
                        device=device)
    r["steps"] = [dict(s) for s in st.items]
    r["ok"] = bool(all(s["ok"] for s in st.items))
    r["finished_at"] = int(time.time() * 1000)
    return r


def _ring_centroid(ring):
    """区域多边形的中心（经,纬）；点认 lng/lat 或 longitude/latitude。空的返 None。"""
    xs, ys = [], []
    for p in (ring or []):
        if not isinstance(p, dict):
            continue
        try:
            xs.append(float(p.get("lng", p.get("longitude"))))
            ys.append(float(p.get("lat", p.get("latitude"))))
        except Exception:                                    # noqa: BLE001
            continue
    if not xs:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _recent_fence_index(c) -> int:
    """「最近一次跑步用的是哪个区域」：记录自带的 `geofence` 是**学校全量**（与设置同一份），
    分不出单次跑的哪个区域，所以要看这条记录**自己跑的轨迹**离哪个区域中心最近
    （记录轨迹与围栏同属学校口径 GCJ-02，直接比，不做换算）。取最近一条带轨迹的记录，返回区域下标。"""
    def _t(r):
        try:
            return int((r or {}).get("startTime") or (r or {}).get("addTime") or 0)
        except Exception:                                    # noqa: BLE001
            return 0
    lst = c.list_records()
    recs = ((lst or {}).get("records") if isinstance(lst, dict)
            else (lst if isinstance(lst, list) else [])) or []
    for r0 in sorted(recs, key=_t, reverse=True):
        rid = (r0 or {}).get("id")
        if not rid:
            continue
        dd = c.get_record(rid) or {}
        rings = dd.get("geofence") or []
        track = dd.get("gitudeLatitude") or dd.get("pointList") or []
        if not isinstance(rings, list) or len(rings) < 2 or not isinstance(track, list) or not track:
            continue
        xs, ys = [], []
        for q in track:
            if not isinstance(q, dict):
                continue
            try:
                xs.append(float(q.get("o", q.get("lng"))))
                ys.append(float(q.get("a", q.get("lat"))))
            except Exception:                                # noqa: BLE001
                continue
        if not xs:
            continue
        tc = (sum(xs) / len(xs), sum(ys) / len(ys))
        best, bd = None, None
        for i, ring in enumerate(rings):
            rc = _ring_centroid(ring)
            if not rc:
                continue
            d = (rc[0] - tc[0]) ** 2 + (rc[1] - tc[1]) ** 2
            if bd is None or d < bd:
                best, bd = i, d
        if best is not None:
            return best
    return None


# ============================================================ 分步（向导：一环扣一环）
def stage_login(*, chain="v1", base=DEFAULT_BASE, school_code=DEFAULT_SCHOOL,
                username=DEFAULT_USER, password=DEFAULT_PASS, run_type=1,
                school_id="184", device_id=DEFAULT_DEVICE_ID, timeout=20.0,
                device=None, app_version=None) -> dict:
    """**第一步「登录」**：只登录 + 拉设置，不做任何跑步动作（不生成、不上传、不写记录）。

    结果里的 `session`（token / identify / setting）交给第三步开跑复用 —— 向导是
    「一环扣一环」，所以登录这一步既不能顺手跑一遍，也不能白跑（token 要留给后面用）。
    """
    chain = (chain or "v1").lower()
    st = Steps()
    out = {"chain": chain, "base": base, "steps": st, "session": {}, "info": {},
           "run_type": run_type}
    login_ok = False   # 只认「拿到 token」；拉配置失败不该把人卡在登录页（2026-10-10）

    if chain in ("v2", "new", "school"):
        import v2client as V2C
        c = V2C.V2Client(base, school_code=school_code or DEFAULT_SCHOOL,
                         raw_device_id=device_id or DEFAULT_DEVICE_ID, timeout=timeout,
                         device=device, app_version=app_version)
        try:
            c.login(username or DEFAULT_USER, password if password is not None else DEFAULT_PASS)
            st.add("登录 auth/oauth/token", True, "拿到 Bearer token（%s…）" % (c.token or "")[:12])
            login_ok = True
        except Exception as e:                               # noqa: BLE001
            _fail(out, st, "登录 auth/oauth/token", e, kind="login")
            return out
        bean = {}
        try:
            bean = c.setting(run_type, None, None)
            tr = float(bean.get("totalRange") or 0)          # v2 的 totalRange 单位就是 km
            st.add("配置 getExerciseSetting", bool(bean.get("identify")),
                   "identify=%s；目标 %.2f km / 时长门槛 %s 分 / 限速 %s" % (
                       bean.get("identify"), (tr / 1000.0 if tr > 100 else tr),
                       bean.get("startQualifiedTime"), bean.get("limitSpeed")))
            out["set_ok"] = bool(bean.get("identify"))
        except Exception as e:                               # noqa: BLE001
            try:                                             # 顺手问一句「到底哪一种现在能跑」
                out["runTypes"] = _v2_run_types(c)
            except Exception:                                # noqa: BLE001
                out["runTypes"] = {}
            # 配置拉不到 **不代表登录失败**：token 已经到手。多半是「今日已达标 / 该类跑步未开放」。
            # 记一条 FAIL 回执就好，把登录放行（用户 2026-10-10 要求：卡在「开跑」而非「登录」）。
            st.add("配置 getExerciseSetting", False, _err_text(e))
            out["set_ok"] = False
            out["set_msg"] = _err_text(e)
            bean = {}
        # 围栏兜底：getExerciseSetting 在学校「当前未开放 / 今日已达标」时会回 999（拿不到 bean），
        # 但「有效区域」是学校固定属性，用户的历史跑步记录里也带着**同一份** —— 设置里没围栏时，
        # 就从最近一条记录取（记录读取不受时段限制），免得登录后误报「该学校没设置围栏」。
        if not (bean or {}).get("geofence"):
            try:
                _lst = c.list_records()
                _recs = ((_lst or {}).get("records") if isinstance(_lst, dict)
                         else (_lst if isinstance(_lst, list) else [])) or []
                for _r0 in _recs:
                    _rid = (_r0 or {}).get("id")
                    if not _rid:
                        continue
                    _dd = c.get_record(_rid) or {}
                    if _dd.get("geofence"):
                        bean = dict(bean or {})
                        bean["geofence"] = _dd["geofence"]
                        out["geofence_from"] = "record"
                        st.add("回落最近记录的围栏", True,
                               "记录 %s 带出 %d 个区域" % (_rid, len(_dd["geofence"])))
                        break
            except Exception as e:                           # noqa: BLE001
                st.add("回落最近记录的围栏", False, _err_text(e))
        # 「最近一次跑步用哪个区域」：好让选择弹层把那个区域标成（推荐）。
        # 记录自带的 geofence 是学校全量（分不出），所以看它**自己跑的轨迹**落在哪个区域附近。
        if len(bean.get("geofence") or []) >= 2:
            try:
                _ri = _recent_fence_index(c)
                if _ri is not None:
                    out["recent_region"] = _ri
                    st.add("识别最近一次跑步的区域", True, "区域 #%d" % (_ri + 1))
            except Exception as e:                           # noqa: BLE001
                st.add("识别最近一次跑步的区域", False, _err_text(e))
        # 顺手问一句「本人有没有已注册底片」—— 要扫脸的学校，这一步就该知道
        # （拿底片去打卡才过得了服务器的 1:1 比对；取不到就只剩「你自己传照片」或占位图）。
        _uid, _album = _probe_face_album(c, bean.get("identify"))
        out["session"] = {"chain": "v2", "base": base, "token": c.token,
                          "identify": bean.get("identify"), "setting": bean,
                          "bas_user_id": _uid}
        out["info"] = {"chain": "v2", "school_code": school_code, "username": username,
                       "identify": bean.get("identify"),
                       "total_km": (lambda t: t / 1000.0 if t > 100 else t)(
                           float(bean.get("totalRange") or 0)),
                       "is_start_face": bean.get("isStartFace"),
                       "is_end_face": bean.get("isEndFace"),
                       "is_mid_face": bean.get("isMidwayFace"),
                       "middle_faces": bean.get("middleFaces"),
                       "limit_speed": bean.get("limitSpeed"),
                       # 有效区域（电子围栏）：只有登录后才拿得到 —— 前端据此在地图上画出
                       # 学校允许跑的范围（可能 1 个或多个多边形），并做「电子围栏自检」。
                       "geofence": bean.get("geofence"),
                       "geofence_from": out.get("geofence_from") or "setting",
                       "recent_region": out.get("recent_region"),
                       "is_sequence_point": bean.get("isSequencePoint"),
                       "bas_user_id": _uid, "face_album": _album}
    else:
        import server as S
        run_type = T.v1_run_type(run_type)   # 老链路分支：下面全是 `/app/**` 的老编号字段
        post = lambda path, form, token="mock-token": S._post_form(base, path, form, token=token)  # noqa: E731
        try:
            r = post("/app/getSchoolInfo",
                     {"schoolId": school_id,
                      "model": (device or {}).get("model") or DP.DEFAULT_DEVICE["model"]})
            d = r.get("data") or {}
            st.add("选校 getSchoolInfo", str(d.get("sysType")) == "2",
                   "schoolCode=%s / sysType=%s ⇒ 老链路" % (d.get("schoolCode"), d.get("sysType")))
            school_name = d.get("schoolName") or d.get("name") or ""
        except Exception as e:                               # noqa: BLE001
            st.add("选校 getSchoolInfo", False, repr(e))
            out["ok"] = False
            out["steps"] = [dict(s) for s in st.items]
            return out
        try:
            r = post("/app/appstu/login", {"uname": username or DEFAULT_USER,
                                           "pwd": password if password is not None else DEFAULT_PASS})
            d = r.get("data") or {}
            token = d.get("token")
            user_id = d.get("id") or username or DEFAULT_USER
            st.add("登录 appstu/login", bool(token), "token=%s…" % (token or "")[:12])
        except Exception as e:                               # noqa: BLE001
            _fail(out, st, "登录 appstu/login", e, kind="login-v1")
            return out
        cfg = {}
        try:
            r = post("/app/sportRecordSetting/getSetting",
                     {"runType": str(run_type), "uid": str(user_id)})
            cfg = r.get("data") or {}
            identify = cfg.get("identify") or "identify-%d" % int(time.time())
            st.add("配置 sportRecordSetting/getSetting", bool(cfg.get("identify")),
                   "identify=%s" % identify)
        except Exception as e:                               # noqa: BLE001
            st.add("配置 sportRecordSetting/getSetting", False, repr(e))
            cfg = {}
            identify = None
        out["session"] = {"chain": "v1", "base": base, "token": token, "user_id": user_id,
                          "identify": identify, "setting": cfg}
        out["info"] = {"chain": "v1", "school_code": school_code, "username": username,
                       "school_name": school_name, "user_id": user_id, "identify": identify,
                       "total_km": cfg.get("totalRange") or cfg.get("totalKilometre"),
                       "is_start_face": cfg.get("isStartFace"),
                       "is_end_face": cfg.get("isEndFace"),
                       "is_mid_face": cfg.get("isMidwayFace"),
                       "middle_faces": cfg.get("middleFaces")}

    # 登录成不成功只看「拿到 token」这一步；v2 的「配置」失败不再把人锁在登录页。
    if chain in ("v2", "new", "school"):
        out["ok"] = bool(login_ok)
    else:
        out["ok"] = all(s["ok"] for s in st.items)
    out.setdefault("set_ok", True)
    out["steps"] = [dict(s) for s in st.items]
    out["finished_at"] = int(time.time() * 1000)
    _write_login_log("login", out, st)
    return out


def stage_run(*, chain="v1", base=DEFAULT_BASE, session=None, params=None, gen=None,
              faces=True, run_type=1, face_image=None, face_image_b64=None,
              school_id="184", school_code=DEFAULT_SCHOOL, username=DEFAULT_USER,
              password=DEFAULT_PASS, device_id=DEFAULT_DEVICE_ID, timeout=20.0,
              realtime=False, speed=1.0, progress=None, cancel=None, stop=None,
              on_step=None, device=None, app_version=None) -> dict:
    """**第三步「开跑」**：开跑时刻 → 生成 → 人脸 → 上传 → 读回 → 汇总。

    给了 `session`（第一步登录的结果）就复用它的 token / identify / setting，不再重复登录。
    `realtime=True`＝按计划的真实用时跑一遍（人脸到点发、跑到点才上传）；`progress(snap)`
    每次报进度（含当前位置 `pos`），`on_step` 每出一条回执就回调一次；`cancel()` 中止
    （不上传），`stop()` 停止**并按已跑的那一段上传**（页面上的「停止并上传」）。
    """
    chain = (chain or "v1").lower()
    st = Steps(on_add=on_step)
    if chain in ("v2", "new", "school"):
        r = run_flow_v2(base=base, school_code=school_code, username=username, password=password,
                        params=params, gen=gen, faces=faces, run_type=run_type,
                        face_image=face_image, face_image_b64=face_image_b64,
                        device_id=device_id, steps=st, timeout=timeout, session=session,
                        realtime=realtime, speed=speed, progress=progress, cancel=cancel,
                        stop=stop, device=device, app_version=app_version)
    else:
        r = run_flow_v1(base=base, params=params, gen=gen, faces=faces, run_type=run_type,
                        face_image_b64=face_image_b64, school_id=school_id,
                        school_code=school_code, username=username, password=password,
                        steps=st, timeout=timeout, session=session,
                        realtime=realtime, speed=speed, progress=progress, cancel=cancel,
                        stop=stop)
    r["steps"] = [dict(s) for s in st.items]
    r["ok"] = bool(all(s["ok"] for s in st.items))
    r["session_reused"] = bool(session)
    r["finished_at"] = int(time.time() * 1000)
    return r


# ============================================================ 自测用：自起 mock 跑一遍
def _oval(lat=23.0500, lng=113.4200, radius_m=140.0, n=60):
    import math
    rlat = radius_m / 111320.0
    rlng = radius_m / (111320.0 * math.cos(math.radians(lat)))
    return [[lng + rlng * math.cos(2 * math.pi * i / n),
             lat + rlat * math.sin(2 * math.pi * i / n)] for i in range(n + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="v2", choices=["v1", "v2"])
    ap.add_argument("--base", default=None)
    ap.add_argument("--km", type=float, default=2.0)
    ap.add_argument("--faces", type=int, default=1, help="1=开开始/结束人脸，0=关")
    a = ap.parse_args()

    srv = None
    base = a.base
    if not base:
        import mock_boxkj as M
        srv = M.ThreadingHTTPServer(("127.0.0.1", 0), M.H)
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print("[mock] 自起于 %s" % base)

    params = {"points": _oval(), "target_km": a.km, "pace_s_per_km": 330,
              "sample_interval_s": 1.0, "speed_jitter": 0.08, "loop_mode": "repeat",
              "run_type": 1, "crs": "gcj02", "start_face": bool(a.faces),
              "end_face": bool(a.faces), "middle_faces": [0.3, 0.7] if a.faces else [],
              "sex": "male", "pause_count": 0}
    try:
        r = run_flow(chain=a.chain, base=base, params=params, faces=bool(a.faces))
        for s in r["steps"]:
            print("  [%s] %s  — %s" % ("OK  " if s["ok"] else "FAIL", s["name"], s["detail"]))
        print("\n==== 全流程（%s）：%d 步 / %d FAIL ====" % (
            a.chain, len(r["steps"]), sum(1 for s in r["steps"] if not s["ok"])))
    finally:
        if srv:
            srv.shutdown()
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
