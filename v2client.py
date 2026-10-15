# -*- coding: utf-8 -*-
"""v2client.py —— 新链路（v2）全流程客户端：学校 cloudUrl + exercise/upms 路径。

为什么单独一个模块
------------------
`replay.py` 走的是**老链路**（`m.boxkj.com` + `/app/**` + `returnCode`）。
但真机 app 的跑步业务打的是**学校自己的域名**（gdufe ⇒ `http://a.sxstczx.com`），
协议完全不同（Bearer + AES-CBC 签名 + AES/RSA 加密参数 + `code/data` 信封）。
本模块把那条链路**完整实现一遍**，并逐处对齐参考实现
`TiShiNengSdkPublic` / `tsnRunServer` 的调用姿势（含两种参数编码、multipart 人脸、
`environment` 内嵌字段、`pointList/okPointList/stepNumbers` 的取值）。

链路（对齐 `tsnRunServer.startRun` 的 public 分支）
------------------------------------------------
  1. auth/oauth/token        → Bearer token（密码 AES-CBC/零填充，key=iv='thanks,pig4cloud'）
  2. getExerciseSetting      → 配置 bean（**密文应答**，AES-ECB 解开）
  3. getExerciseStartTime    → 开跑时刻
  4. exerciseRunningFace × N → 人脸打卡（multipart + key/param 加密）
  5. addExerciseRecord       → 上传（enc 参数；内含 environment 环境上报）
  6. sumExerciseRecord / listExerciseRecord → 汇总与读回

用法
----
  from v2client import V2Client
  c = V2Client("http://127.0.0.1:18080", school_code="gdufe")
  c.login("20210001", "mock-pass")
  bean = c.setting(1, 113.42, 23.05)
  ...
  c.full_run(gen)      # 把 tracklib.generate() 的结果整条推上去
"""
from __future__ import annotations

import gzip
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for _p in (HERE, TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import v2_chain as V2          # noqa: E402
import envlib as E             # noqa: E402
import tracklib as T           # noqa: E402  （跑步类型编号的唯一出处）
import device_profile as DP    # noqa: E402  （设备 / app 身份的唯一出处）

# 跑步类型编号的唯一出处是 `tracklib`（新链路 0 自由跑 / 1 阳光跑 / 2 晨跑）。
# 新链路的 `sportType` **就是新编号本身**，别再拿「旧→新」表换算一次 —— 见 tracklib
# 那段注释里 2026-09-24 的踩坑记录（阳光跑被算成晨跑，服务器回「晨跑未开启」）。

# 默认设备档 = **真机 iPhone**（唯一出处见 `device_profile.py`）：真机基线
# brand=iPhone / model=iPhone 14 Pro Max / system=iOS / version=26.6.1 / appVersion=3.6.814
# （2026-09-24 只读拉回本人合格记录坐实；旧包写死 Xiaomi/Android/2.0.23 与之完全不符）。
DEFAULT_DEVICE = dict(DP.DEFAULT_DEVICE)

# getExerciseSetting 的默认坐标（广东财经大学三水校区附近，与 server.DEFAULT_CENTER 同源）。
# 登录这一刻手上没有真实定位，但**必须**给个像样的坐标 —— 以前调用方传 None，`str(None)` 就把
# 字面量 `"None"` 当经纬度发出去，服务器解析不了 ⇒ 「登录」这一步整体 FAIL。
DEFAULT_LNG = 113.2790
DEFAULT_LAT = 23.1994

# 取不到真实人脸照时发的占位图（过自家 mock 行；真实学校会判**不通过**，所以日志/界面要说清）。
FACE_PLACEHOLDER = b"\xff\xd8\xff\xe0route-studio-face"


def add_pixel_noise(image_bytes: bytes, n: int = 8) -> bytes:
    """对齐参考 `tsnRunServer.add_random_pixels_to_image`：随机改几个像素点。

    为什么要加噪：上传的是**账号已注册的那张底片**，逐字节相同容易被去重/复用检测盯上
    （参考实现原话就是「add_random_pixels_to_image」）。Pillow 不在、或不是图片 ⇒ 原样返回。
    """
    try:
        import io as _io
        import random
        from PIL import Image
        im = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        px = im.load()
        w, h = im.size
        for _ in range(max(1, int(n))):
            px[random.randrange(w), random.randrange(h)] = (
                random.randrange(256), random.randrange(256), random.randrange(256))
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception:                                        # noqa: BLE001
        return image_bytes


def _short_msg(body: str) -> str:
    """把服务器回包抠成**一句人话**（pig4cloud 的 msg / Spring 的 message / 老链路的 returnMsg）。"""
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            for k in ("msg", "message", "error_description", "returnMsg", "error"):
                if j.get(k):
                    return str(j[k])
    except Exception:                                        # noqa: BLE001
        pass
    return " ".join(str(body or "").split())[:120]


class V2Error(RuntimeError):
    def __init__(self, msg, code=None, payload=None):
        super().__init__("v2 %s: %s" % (code if code is not None else "-", msg))
        self.code = code
        self.payload = payload


class V2Client:
    def __init__(self, base: str, school_code: str = "gdufe", token: Optional[str] = None,
                 raw_device_id: str = "route-studio-device", brand: str = None,
                 model: str = None, os_version: str = None,
                 abis: Iterable[str] = None, timeout: float = 20.0,
                 trace: Optional[List[dict]] = None,
                 device: Optional[dict] = None, system: str = None,
                 app_version: str = None, app_type: str = None,
                 version_code: int = None, signature_md5: str = None,
                 ua: str = None):
        self.base = base.rstrip("/")
        self.school_code = school_code
        self.timeout = timeout
        # 设备 / app 身份统一由 `device_profile` 归一化：只认 brand/model/system/version/abis，
        # 空值回落**真机 iPhone**，并按 system 补齐 appType/versionCode/signatureMD5/UA。
        _dev_in = dict(device or {})
        if brand:
            _dev_in["brand"] = brand
        if model:
            _dev_in["model"] = model
        if system:
            _dev_in["system"] = system
        if os_version:
            _dev_in["version"] = os_version
        if abis is not None:
            _dev_in["abis"] = list(abis)
        self.dev = DP.resolve(_dev_in, app_version)
        self.app_version = self.dev["appVersion"]
        self.app_type = app_type or self.dev["appType"]
        self.version_code = self.dev["versionCode"] if version_code is None else version_code
        self.signature_md5 = (self.dev["signatureMD5"] if signature_md5 is None
                              else signature_md5)
        self.ua = ua or self.dev["userAgent"]
        self.raw_device_id = raw_device_id
        # `uniqueCode` 的形态**随平台**：iOS 真机发原始 UUID（device_profile 已按档给出
        # `deviceCode`）；无该值时回落参照实现的 `md5(raw_device_id)`（安卓形态）。
        self.device_id = (str(self.dev["deviceCode"]) if self.dev.get("deviceCode")
                          else V2.md5(str(raw_device_id)))
        self.token = token
        self.trace: List[dict] = trace if trace is not None else []
        self.host = urllib.parse.urlparse(self.base).netloc or "localhost"

    # ------------------------------------------------------------------ 基础设施
    def v2_base_dict(self) -> Dict[str, Any]:
        """对齐参考 `self.v2BaseDict`（每个业务请求都要带）。"""
        return {
            "appType": self.app_type, "versionCode": self.version_code,
            "versionName": self.app_version, "signatureMD5": self.signature_md5,
            "brand": self.dev["brand"], "model": self.dev["model"],
            "system": self.dev["system"], "version": self.dev["version"],
            "uniqueCode": self.device_id,
        }

    def headers(self, sign: str = None, ts: str = None, bearer: bool = True,
                include_model: bool = False) -> Dict[str, str]:
        """对齐参考 `self.headers`（uniqueCode/school/Cookie）+ 签名字段。

        头部集合按 **2026-09-27 iOS 实测**（`22-iOS抓包与信封复核.md` §3.1 + run pcap）：
          * `model` **只出现在 `auth/oauth/token` 那一条**上 ⇒ 仅 `include_model=True` 时发
            （登录调用点传 True；其余业务请求不发）。
          * 真机**连 GET 都带** `Content-Type: …; charset=utf-8`，另有 `Accept: */*`、
            `Accept-Language: zh-Hans-CN;q=1`、`Accept-Encoding: gzip, deflate`、
            `Connection: keep-alive`。旧实现这几条**都没发**（2026-10-10 补齐）。
        """
        h = {
            "Host": self.host,
            "uniqueCode": self.device_id,
            "school": self.school_code,
            "Cookie": "host=" + self.school_code,
            "Accept": "*/*",
            "Accept-Language": "zh-Hans-CN;q=1",
            "accept-encoding": "gzip, deflate",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Connection": "keep-alive",
            "User-Agent": self.ua,
        }
        if include_model:
            h["model"] = str(self.dev["model"])
        if bearer and self.token:
            h["Authorization"] = "Bearer " + self.token
        if sign:
            h["sign"] = sign
        if ts:
            h["timestamp"] = ts
        return h

    def _req(self, method: str, path: str, *, form: dict = None, query: dict = None,
             headers: dict = None, body: bytes = None, ctype: str = None) -> Any:
        url = self.base + "/" + path.lstrip("/")
        if query:
            # 真机 iOS 的 GET query = **单层**百分编（`+`→`%2B`、`/` **原样**、`=`→`%3D`）——
            # 即 `quote(safe='/')`，而**不是** `urlencode` 默认的 `quote_plus`（会发 `%2F`）。
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                query, quote_via=urllib.parse.quote, safe="/")
        h = dict(headers or {})
        data = body
        if ctype:
            h["Content-Type"] = ctype            # 例：人脸走 multipart ⇒ 必须显式声明
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            h.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")

        def _decompress(b: bytes, enc: str) -> bytes:
            # 真机 iOS 发 `accept-encoding: gzip, deflate` ⇒ 真服常回 gzip；必须自行解压。
            enc = (enc or "").lower()
            if "gzip" in enc:
                return gzip.decompress(b)
            if "deflate" in enc:
                import zlib
                return zlib.decompress(b)
            return b

        # 走 `http.client` 而**不是** `urllib.request`：后者在 `AbstractHTTPHandler.do_open`
        # 里**写死** `headers["Connection"] = "close"`（Python 3.12 `urllib/request.py`），
        # 把我们声明的 `keep-alive` 冲掉 —— 真机 iOS 是 keep-alive，差这条就露馅。
        u = urllib.parse.urlsplit(url)
        selector = u.path + (("?" + u.query) if u.query else "")
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=self.timeout)
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        try:
            conn.request(method, selector, body=data, headers=h)
            r = conn.getresponse()
            raw_bytes, status = r.read(), r.status
            enc = r.getheader("Content-Encoding")
        except OSError as e:                                 # 连接/超时：对齐 urllib 的 URLError
            raise urllib.error.URLError(e)
        finally:
            conn.close()

        blob = _decompress(raw_bytes, enc)
        if status >= 400:
            # 服务器原话必须一路带到界面 —— 以前只报「HTTP 426」，用户和我们都看不出
            # 到底是账号错、被冻结、还是服务器根本不认这所学校（2026-09-24 登录排障踩过）。
            text = blob[:400].decode("utf-8", "replace")
            raise V2Error("HTTP %s %s" % (status, _short_msg(text)), status, text)
        raw = blob.decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:                                    # noqa: BLE001
            raise V2Error("响应不是 JSON", None, raw[:400])

    # ---------------------------------------------------------------- ① 登录
    def login(self, username: str, password: str) -> str:
        """auth/oauth/token —— **query 参数** + Basic(schoolCode:pig)；密码零填充 CBC。"""
        params = {"username": username, "password": V2.encrypt_password(password),
                  "grant_type": "password", "type": "app", "appType": "stuApp"}
        h = self.headers(bearer=False, include_model=True)   # 真机只在 oauth 上带 model
        h["Authorization"] = "Basic " + V2.b64(self.school_code + ":pig")
        # 真机是 **GET**（凭据走 query，无 body），不是 POST（run pcap 实测）
        j = self._req("GET", V2.route_path("oauthToken"), query=params, headers=h)
        tok = j.get("access_token")
        if not tok:
            raise V2Error("登录未拿到 access_token", j.get("code"), j)
        self.token = tok
        self.trace.append({"step": "oauth", "ok": True})
        return tok

    # ---------------------------------------------------------------- ② 配置
    def setting(self, run_type: int = 1, longitude: float = None, latitude: float = None) -> dict:
        """getExerciseSetting（GET + enc 参数；应答是**密文**，用同一把会话密钥解开）。

        ⚠️ `longitude/latitude` 给 None 时**回落默认中心点**，绝不发字面量 `"None"`：
        服务器的经纬度字段解析不了 "None"，会让整个「登录」步骤 FAIL（界面显示「登录失败」），
        可 token 其实早就拿到了 —— 2026-09-24 用户报「还是无法正常登陆」的根因之一。
        """
        if longitude is None:
            longitude = DEFAULT_LNG
        if latitude is None:
            latitude = DEFAULT_LAT
        params = self.v2_base_dict()
        params.update({"runType": run_type, "longitude": str(longitude), "latitude": str(latitude)})
        ts = str(int(time.time() * 1000))
        # 真机 pcap 里 getSetting 的 key/param 是**原样 base64**（`%2B` + `/`），但**真服实测**：
        # 原样 base64 → **HTTP 403**；而 `+` 换成空格的形态 → **HTTP 200**（r t329 四变体实测）。
        # 故 setting 必须走 `encoded=False`（空格替换），与上传/人脸不同 —— 那是服务端解码口径。
        enc = V2.build_enc_params(params, self.token, ts, encoded=False)
        j = self._req("GET", V2.route_path("getExerciseSetting"), query=enc,
                      headers=self.headers(self.sign(enc, ts), ts))
        if j.get("code") != 0:
            raise V2Error("getExerciseSetting 失败", j.get("code"), j)
        bean = json.loads(V2.aes_ecb_decrypt(j["data"], V2.session_aes_key(self.token, ts)))
        self.trace.append({"step": "setting", "ok": True, "identify": bean.get("identify")})
        return bean

    def start_time(self, identify: str) -> int:
        params = {"identify": identify}
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("getExerciseStartTime"), query=params,
                      headers=self.headers(self.sign(params, ts), ts))
        if j.get("code") != 0:
            raise V2Error("getExerciseStartTime 失败", j.get("code"), j)
        st = int((j.get("data") or {}).get("startTime") or 0)
        self.trace.append({"step": "startTime", "ok": bool(st), "startTime": st})
        return st

    # ---------------------------------------------------------------- ③ 人脸
    def face(self, image_bytes: bytes, coordinates: str, identify: str,
             run_type: int = 1, face_type: int = 1,
             environment: Optional[dict] = None) -> dict:
        """exerciseRunningFace —— multipart：key/param（**原始 base64**）+ file。"""
        ts = str(int(time.time() * 1000))
        params = self.v2_base_dict()
        params.update({
            "identify": identify, "type": str(face_type), "runType": str(run_type),
            "coordinates": coordinates, "timeStamp": ts, "exception": "0",
            "environment": environment if environment is not None else self.env_data(),
        })
        enc = V2.build_enc_params(params, self.token, ts, raw=True)
        # 真机 boundary = `Boundary+<16 位大写 HEX>`（实测 Boundary+96CFDAF3F2D9B5AF）
        bound = "Boundary+" + uuid.uuid4().hex[:16].upper()
        parts = []
        for k, v in enc.items():
            parts.append(b"--" + bound.encode())
            parts.append(('Content-Disposition: form-data; name="%s"' % k).encode())
            parts.append(b"")
            parts.append(str(v).encode("utf-8"))
        parts.append(b"--" + bound.encode())
        parts.append(b'Content-Disposition: form-data; name="file"; filename="fileIOS.jpg"')
        parts.append(b"Content-Type: image/png/jpg/jpeg")
        parts.append(b"")
        parts.append(image_bytes)
        parts.append(b"--" + bound.encode() + b"--")
        parts.append(b"")
        body = b"\r\n".join(parts)
        h = self.headers(self.sign(enc, ts), ts)
        j = self._req("POST", V2.route_path("exerciseRunningFace"), headers=h, body=body,
                      ctype="multipart/form-data; boundary=" + bound)
        self.trace.append({"step": "face", "type": face_type, "code": j.get("code")})
        return j

    # ---------------------------------------------------------------- ④ 环境
    def env_data(self, **kw) -> dict:
        """参考 `getEnvData()` = `tsn_environment.build_environment_payload(...)`。"""
        # 环境报告里的 `deviceId` 与 `uniqueCode` **同源**（参照实现亦然）：
        # self.device_id 已是最终形态（iOS=UUID / 安卓=md5），故不再二次 md5。
        return E.clean_payload(device_id=self.device_id, abis=self.dev["abis"],
                               hash_device_id=False, **kw)

    # ---------------------------------------------------------------- ⑤ 上传
    def record_form(self, gen: dict, *, env: dict = None, user_open_develop: bool = False,
                    offline: int = 0, setting: Optional[dict] = None,
                    is_face_status: int = 1, upload_type: int = 0,
                    run_type: int = 1) -> Dict[str, Any]:
        """把 `tracklib.generate()` 的结果拼成新链路 21+ 参（对齐 `tsnRunServer` 的调用）。"""
        sub = gen.get("submit") or {}
        metrics = gen.get("metrics") or {}
        setting = setting or {}
        points = gen.get("track_pointdata") or []
        used_time = int(round(metrics.get("elapsed_s") or 0))
        sport_range = str(sub.get("formatSportRange") or "%.3f" % ((metrics.get("distance_m") or 0) / 1000.0))
        avg_speed = str(round(float(metrics.get("distance_m") or 0) / max(used_time, 1) * 3.6, 2))
        step_numbers = list((gen.get("steps") or {}).get("buckets") or [])
        # 对齐参考 tsn_payload.aggregate_risk_flags：按点上的 f/m/h 计数 + d
        # ⚠️ 点上的 m 现在是**恒定字符串 "0"**，而 Python 里 "0" 是**真值** ⇒ 必须按
        #    真值语义判，否则 1161 个点全被算成风险点、m 直接飙成点数。
        def _t(v):
            if isinstance(v, bool):
                return v
            if v is None:
                return False
            if isinstance(v, (int, float)):
                return v != 0
            return str(v).strip().lower() in ("1", "true", "yes", "y")

        d = (1 if _t(user_open_develop) else 0) + int(offline or 0)
        f = sum(1 for p in points if _t(p.get("f")))
        m = sum(1 for p in points if _t(p.get("m")))
        h = sum(1 for p in points if _t(p.get("h")))
        geofence = setting.get("geofence")
        point_list = setting.get("list") or []
        if isinstance(point_list, str):
            point_list = []
        form = self.v2_base_dict()
        form.update({
            "sportType": T.run_type_int(run_type, 1),
            "startTime": sub.get("startTime"), "endTime": sub.get("endTime"),
            "sportTime": used_time, "sportRange": sport_range,
            "speed": sub.get("speed") or metrics.get("pace"),
            "avgSpeed": avg_speed,
            "appVersion": self.app_version,
            # stepNumbers：真机/参照都是**紧凑分隔符**（逗号无空格）
            "stepNumbers": json.dumps(step_numbers, ensure_ascii=False, separators=(",", ":")),
            # isSequencePoint：跟 setting 走（真机 setting.isSequencePoint == "1"）
            "isSequencePoint": str(setting.get("isSequencePoint") or "1"),
            "gitudeLatitude": json.dumps(points, ensure_ascii=False),
            "isFaceStatus": str(is_face_status),
            "uploadType": int(upload_type),
            "identify": sub.get("identify"),
            "geofence": json.dumps(geofence, ensure_ascii=False) if geofence is not None else "[]",
            "limitSpeed": str(setting.get("limitSpeed") or "3.0"),
            # ⚠️ 真机 setting 里**没有** limitStride / limitStepFrequency —— 它叫
            #    endStride / endLimitStepFrequency。读错键就落到硬编码兜底，被服务器
            #    原样存下来（16:43 那两条就是这样落的 1.5/200；真机是 2.3/300）。
            #    兜底值也一并改成真机值。
            "limitStride": str(setting.get("endStride") or setting.get("limitStride") or "2.3"),
            "limitStepFrequency": str(setting.get("endLimitStepFrequency")
                                      or setting.get("limitStepFrequency") or "300"),
            "gpsDistance": sport_range,
            "d": d, "f": f, "m": m, "h": h,
            "environment": env if env is not None else self.env_data(),
        })
        # pointList / okPointList：**空就不发**（真机记录里这两个键是 null；我们原来发 "[]"）
        if point_list:
            form["pointList"] = json.dumps(point_list, ensure_ascii=False)
        # isValid：参照实现的客户端会自己算一个一起发（tsn_payload.compute_is_valid）
        try:
            form["isValid"] = T.is_valid_qualified(
                setting, used_time, float(metrics.get("distance_m") or 0.0),
                int((gen.get("steps") or {}).get("total_steps") or 0))
        except Exception:                                     # noqa: BLE001
            pass
        rid = str(setting.get("remarkId") or "").strip()
        if rid and rid != "0":
            form["remarkId"] = rid
        return form

    def add_record(self, gen: dict, **kw) -> Tuple[Any, Optional[int]]:
        """addExerciseRecord —— enc 参数走 **is_encoded=True（quote）**，响应含顶层 recordId。"""
        form = self.record_form(gen, **kw)
        ts = str(int(time.time() * 1000))
        enc = V2.build_enc_params(form, self.token, ts, encoded=True)
        j = self._req("POST", V2.route_path("addExerciseRecord"), form=enc,
                      headers=self.headers(self.sign(enc, ts), ts))
        if j.get("code") != 0:
            raise V2Error("addExerciseRecord 失败", j.get("code"), j)
        rid = j.get("exerciseRecordId")
        self.trace.append({"step": "addExerciseRecord", "ok": True, "recordId": rid,
                           "d": form["d"], "f": form["f"], "m": form["m"], "h": form["h"]})
        return j.get("data"), rid

    # ---------------------------------------------------------------- ⑥ 读回
    def sum_(self) -> dict:
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("sumExerciseRecord"), query={},
                      headers=self.headers(self.sign({}, ts), ts))
        self.trace.append({"step": "sumExerciseRecord", "code": j.get("code")})
        return j.get("data") or {}

    def get_feedback_balance(self) -> dict:
        """getFeedbackBalance —— 本学期剩余次数（`validity?Run` 应跑 / `balance?Run` 还剩）。"""
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("getFeedbackBalance"), query={},
                      headers=self.headers(self.sign({}, ts), ts))
        self.trace.append({"step": "getFeedbackBalance", "code": j.get("code")})
        return j.get("data") or {}

    def list_records(self, status: int = 1, date: str = "", page: int = 1) -> Any:
        params = {"status": status, "date": date, "datePageIndex": page}
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("listExerciseRecord"), query=params,
                      headers=self.headers(self.sign(params, ts), ts))
        self.trace.append({"step": "listExerciseRecord", "code": j.get("code")})
        return j.get("data")

    def get_record(self, record_id) -> dict:
        params = {"exerciseRecordId": record_id}
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("getExerciseRecord"), query=params,
                      headers=self.headers(self.sign(params, ts), ts))
        return j.get("data") or {}

    # ---------------------------------------------------------------- 人脸底片
    def user_info(self) -> dict:
        """upms/basUser/getLoginUserInfo —— 当前登录用户（取 `basUserId` 用）。"""
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("getLoginUserInfo"), query={},
                      headers=self.headers(self.sign({}, ts), ts))
        return j.get("data") or {}

    def list_bas_user_image_face(self, bas_user_id) -> list:
        """upms/basUserImage/listBasUserImageFace?basUserId=… —— 本人**已注册**人脸底片列表。

        对齐参考 `TiShiNengSdkPublic.listBasUserImageFace()`（GET + 头 sign/timestamp + Bearer）。
        每项含 `imageRouteUrl`（可直接下载的图片地址）。
        """
        params = {"basUserId": str(bas_user_id)}
        ts = str(int(time.time() * 1000))
        j = self._req("GET", V2.route_path("listBasUserImageFace"), query=params,
                      headers=self.headers(self.sign(params, ts), ts))
        data = j.get("data")
        return data if isinstance(data, list) else []

    def fetch_face_image(self, bas_user_id) -> Optional[bytes]:
        """取本人已注册底片的第一张（这张下载失败就换下一张；一张都没取到返回 None）。"""
        for it in self.list_bas_user_image_face(bas_user_id):
            url = (it or {}).get("imageRouteUrl") if isinstance(it, dict) else None
            if url:
                try:
                    return self.download(url)
                except Exception:                            # noqa: BLE001
                    continue
        return None

    def download(self, url: str) -> bytes:
        """按 URL 拉字节（底片的 imageRouteUrl 常是**另一个域**的图床）。"""
        req = urllib.request.Request(url, headers={"User-Agent": "okhttp/4.9.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read()

    # ---------------------------------------------------------------- 签名
    def sign(self, params: Dict[str, Any], ts: str) -> str:
        return V2.v2_sign(params, self.token, ts)

    # ---------------------------------------------------------------- 编排
    def full_run(self, gen: dict, *, username: str = None, password: str = None,
                 run_type: int = 1, longitude: float = 113.42, latitude: float = 23.05,
                 face_image: bytes = None, faces: bool = True) -> dict:
        """把一条生成好的轨迹整条推上去（登录→配置→开跑→人脸→上传→读回→汇总）。"""
        out: Dict[str, Any] = {}
        if username and password:
            self.login(username, password)
        if not self.token:
            raise V2Error("未登录：请先 login() 或传 token")
        bean = self.setting(run_type, longitude, latitude)
        out["setting"] = bean
        identify = bean.get("identify")
        start_ms = self.start_time(identify) or int(time.time() * 1000)
        out["startTime"] = start_ms

        face_results = []
        if faces:
            img = face_image or b"\xff\xd8\xff\xe0route-studio-face"
            for f in gen.get("faces") or []:
                j = self.face(img, f.get("coordinates") or "", identify,
                              run_type=run_type, face_type=int(f.get("type") or 1))
                face_results.append({"type": f.get("type"), "code": j.get("code")})
        out["faces"] = face_results

        data, rid = self.add_record(gen, setting=bean, run_type=run_type)
        out["recordId"] = rid
        out["addData"] = data
        out["sum"] = self.sum_()
        out["records"] = self.list_records()
        return out
