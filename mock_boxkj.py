#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W6 私服 mock：接管 m.boxkj.com。

设计要点
--------
* 信封：**并集** {code,msg,total,returnCode,returnMsg,data} —— 一条 JSON 同时满足
  通道 A(code==0) 与通道 B/C(returnCode==200)。见 17-接口规格书 §3。
* **登录类端点例外**：/app/login /app/autoLogin /app/thirdLogin 走「平铺无 data」（通道 D）。
* 响应字段名不是拍脑袋：由 tools/mock_oracle.py 从 APK 的 dex 反汇编抽出，
  再经 tools/mock_selftest.py 反验。
* **本 mock 同时是抓包器**：每个请求（path/headers/完整 body）追加到
  30-logs/w6_mock_capture.jsonl。app 连上来后即可反推其真实 token / 参数 / 报文结构，
  无需 mitmproxy。
* 未知 /app/** 一律回成功信封，避免 app 因 mock 缺端点而崩。

用法
----
  python mock_boxkj.py                        # https:443 + http:80（app 默认端口）
  python mock_boxkj.py --https-port 18443 --http-port 18080   # 免特权端口自测
  python mock_boxkj.py --no-http              # 只开 HTTPS
仅监听本机/实验网段，不对外。
"""
import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.environ.get("MOCK_LOGDIR") or os.path.join(BASE, "30-logs")
CA = os.path.join(BASE, "ca")
CAPTURE = os.path.join(LOGDIR, "w6_mock_capture.jsonl")

LOCK = threading.Lock()
ARGS = None

# --------------------------------------------------------------------------- 签名
# 算法（W8b 字节级确证；亦见 17-接口规格书 §2 与 tools/w8b_sign_probe.py）：
#   token==null 分支 ⇒ sign = URLEncoder.encode( md5( sorted("k=v&...") ), "UTF-8" )
#   参与签名的 map = 业务字段 + appId("move") + timestamp + appSecret
#   线上只追加 timestamp + sign（appId/appSecret 不上线）
# 本 mock 把它**双向实现**：既能校验（判“app 的签名算法我们理解对了吗”），
# 又与 tools/w8b_sign_probe.py 共用同一份实现（不重写、不会两边漂移）。
try:
    import w8b_sign_probe as SIGN
except Exception:            # 允许单文件拷到别处跑
    SIGN = None

try:
    import v2_chain as V2
except Exception:
    V2 = None

try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_studio"))
    import envlib as ENV
except Exception:            # noqa: BLE001
    ENV = None

SIGN_MODE = "warn"           # off | warn | enforce
SIGN_LOG = os.path.join(LOGDIR, "main_sign_audit.jsonl")


def verify_sign(params, ts, got):
    """返回 (ok, 期望签名, 规范化串)。ok=None 表示“无法判定”。"""
    if SIGN is None or ts is None or got is None:
        return None, None, None
    m = SIGN.build_cloud_map(dict(params), ts)          # + appId + appSecret
    canon = "&".join("%s=%s" % (k, m[k]) for k in sorted(m.keys()))
    exp = SIGN.sign_cloud(m)
    try:
        from urllib.parse import unquote
        got_plain = unquote(got)
    except Exception:
        got_plain = got
    return (exp == got or exp == got_plain), exp, canon


def audit(rec):
    """签名审计落盘 —— app 真连上来时，这里能直接告诉你“算法对不对”。"""
    try:
        with LOCK:
            with open(SIGN_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

# --------------------------------------------------------------------------- 信封
# 契约（见 17-接口规格书 §3）：通道 A 看 code==0，通道 B/C 看 returnCode==200。
# 未知字段 Gson/fastjson 默认忽略 ⇒ 一条「并集信封」同时满足 A/B/C。
def env(data=None, code="200", msg=None, total=0):
    good = str(code) == "200"
    return {
        "code": 0 if good else code,
        "msg": "ok" if good else (msg if msg is not None else "error"),
        "total": total,
        "returnCode": code,
        "returnMsg": msg,
        "data": data,
    }

def ok(data=None, total=0):
    return env(data, "200", None, total)

def bad(code, msg):
    return env(None, code, msg)

# 通道 D（无 data 包裹）：登录类端点把状态与业务字段**平铺在顶层**。
# 依据：tools/mock_oracle.py 从 dex 抽出 vm/c;->L 读取的键 =
#   {code, msg, user_id, username, access_token, refresh_token, dept_id}
# ⇒ code/msg 与业务键同级，套 data 就读不到。
def flat(obj, code=0, msg="ok"):
    d = dict(obj)
    d["code"] = code
    d["msg"] = msg
    return d

# --------------------------------------------------------------------------- 数据
PROVINCES = [
    {"province_id": 22, "province_name": "北京"}, {"province_id": 1, "province_name": "陕西省"},
    {"province_id": 5, "province_name": "河北省"}, {"province_id": 14, "province_name": "广西"},
    {"province_id": 11, "province_name": "广东省"}, {"province_id": 9, "province_name": "新疆"},
    {"province_id": 2, "province_name": "青海省"}, {"province_id": 8, "province_name": "湖南省"},
    {"province_id": 7, "province_name": "安徽省"}, {"province_id": 4, "province_name": "河南省"},
    {"province_id": 3, "province_name": "山西省"}, {"province_id": 13, "province_name": "湖北省"},
    {"province_id": 15, "province_name": "甘肃省"}, {"province_id": 17, "province_name": "山东省"},
    {"province_id": 18, "province_name": "四川省"}, {"province_id": 19, "province_name": "黑龙江"},
    {"province_id": 20, "province_name": "辽宁省"}, {"province_id": 23, "province_name": "天津"},
    {"province_id": 24, "province_name": "海南省"}, {"province_id": 25, "province_name": "福建省"},
    {"province_id": 27, "province_name": "吉林省"}, {"province_id": 26, "province_name": "江西省"},
    {"province_id": 28, "province_name": "宁夏"}, {"province_id": 29, "province_name": "内蒙古"},
    {"province_id": 30, "province_name": "演示系统"}, {"province_id": 31, "province_name": "重庆"},
    {"province_id": 32, "province_name": "中小学列表"}, {"province_id": 10, "province_name": "测试"},
]

GDUFE = {
    "school_id": 184, "school_name": "广东财经大学", "schoolCode": "gdufe",
    "school_url": "https://h.tsnkj.com/?school=gdufe", "lan_url": "http://a.sxstczx.com/",
    "openId": "632e1a12397143e4a75b6b0f7d0684a7",
    "isOpenKeep": "1", "isOpenLive": "1", "isOpenEncry": "", "sysType": "2",
    "isOpenSQZS": "1", "isOpenGGLBT": "1", "province_name": "广东省",
    "openPhoneLogin": "0", "schoolType": "1", "insideNetwork": "0",
}

def load_gd():
    p = os.path.join(LOGDIR, "schools_gd.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return [GDUFE]

SCHOOLS = load_gd()

# --------------------------------------------------------------------------- 路由表
def h_province(form, q):
    return ok(PROVINCES, total=0)

def h_list_school(form, q):
    pid = form.get("provinceId") or (q.get("provinceId") or [None])[0]
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = None
    rows = [s for s in SCHOOLS if s.get("province_name") == "广东省"] if pid == 11 else \
           ([GDUFE] if pid is None else [])
    return ok(rows)

def _detail(s):
    """getSchoolById 的**详情**形状（与列表形状字段名不同）——两种命名都给，防 app 读任一种。"""
    d = dict(s)
    d.update({
        "id": s.get("school_id"),
        "schName": s.get("school_name"),
        "urlSite": s.get("school_url"),
        "isJiguangPush": "0",
        "lanUrl": s.get("lan_url"),
        "school_Id": s.get("school_id"),
    })
    return d

# getSchoolInfo 的 data 键，由 dex 判卷人钉死（16 个，见 main_response_oracle.json）
SCHOOL_INFO_KEYS = [
    "schName", "sysType", "isOpenLive", "isOpenSQZS", "isOpenGGLBT", "isJiguangPush",
    "openPhoneLogin", "insideNetwork", "insideNetworkMsg", "schoolCode", "lan_url",
    "isOpenKeep", "isOpenEncry", "openId", "urlSite", "oldToNewH5",
]

def h_get_school_info(form, q):
    """学校配置（通道 B）。★ sysType 是我们的**分流开关**：
    sysType="2" → 客户端走老链路（个人版 LoginActivity + Lp2/a），
    否则走 v2 新链路。我们选 2（拦截器已 100% 逆向、签名可离线算）。"""
    d = {
        "schName": "广东财经大学",
        "sysType": "2",
        "isOpenLive": "1",
        "isOpenSQZS": "1",
        "isOpenGGLBT": "1",
        "isJiguangPush": "0",
        "openPhoneLogin": "0",
        "insideNetwork": "0",
        "insideNetworkMsg": "",
        "schoolCode": "gdufe",
        "lan_url": "http://a.sxstczx.com/",
        "isOpenKeep": "1",
        "isOpenEncry": "",
        "openId": "632e1a12397143e4a75b6b0f7d0684a7",
        "urlSite": "https://h.tsnkj.com/?school=gdufe",
        "oldToNewH5": "0",
        # 额外给列表/详情口径的字段，防 app 读任一种
        "id": 184,
        "schoolId": 184,
        "school_id": 184,
        "schoolName": "广东财经大学",
        "school_name": "广东财经大学",
        "province_name": "广东省",
        "schoolType": "1",
    }
    return ok(d)

def h_school_by_id(form, q):
    sid = form.get("schoolId") or form.get("id") or (q.get("schoolId") or [None])[0]
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        sid = None
    for s in SCHOOLS:
        if s.get("school_id") == sid:
            return ok(_detail(s))
    return ok(_detail(GDUFE))

def h_search_school(form, q):
    kw = (form.get("key") or form.get("keyword") or form.get("name") or "")
    if kw and isinstance(kw, str):
        rows = [s for s in SCHOOLS if kw in (s.get("school_name") or "")]
        return ok(rows or [GDUFE])
    return ok([GDUFE])

MOCK_TOKEN = "MOCK-TOKEN-184"

def h_appstu_login(form, q):
    """老链路登录（通道 C）。data 的 6 个键由 dex 判卷人钉死：
    id/name/userNum/phone/sex/token（main_response_oracle.json）。"""
    return ok({
        "id": "10086",
        "name": "Mock Student",
        "userNum": "20210001",
        "phone": "13800000000",
        "sex": "1",
        "token": MOCK_TOKEN,
    })

def h_v2_login(form, q):
    """新链路登录 / autoLogin / thirdLogin（通道 D：**平铺，无 data 包裹**）。
    顶层键 = dex 抽出的 {code,msg,user_id,username,access_token,refresh_token,dept_id}。"""
    return flat({
        "user_id": 10086,
        "username": "20210001",
        "access_token": MOCK_TOKEN,
        "refresh_token": MOCK_TOKEN + "-R",
        "dept_id": 0,
    })

def h_sport_generic(form, q):
    return ok({})

# --------------------------------------------------------------------------- 记录仓
# 上传的记录存下来，让「上传 → 读回」能闭环自证据（不只是单次回包）。
RECORDS = []
RECORDS_LOG = os.path.join(LOGDIR, "main_records.jsonl")
# v2 新链路另有形态（解密后的 `opened` 参数体）：单独一份仓，别与 v1 的 21 字段混。
V2_RECORDS = []
V2_RECORDS_LOG = os.path.join(LOGDIR, "main_v2_record_store.jsonl")

# appAddSportRecord 的 21 个 @Field（顺序 = dex 参数位，见 api-map.txt L144）
SPORT_FIELD_ORDER = [
    "userId", "runType", "startTime", "endTime", "gitudeLatitude", "identify",
    "formatSportTime", "formatSportRange", "avgspeed", "speed", "okPointList",
    "brand", "model", "system", "version", "appVersion", "stepNumbers",
    "isFaceStatus", "points", "uploadType", "keepId",
]


def _load_records():
    if not os.path.exists(RECORDS_LOG):
        return
    try:
        with open(RECORDS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    RECORDS.append(json.loads(line))
    except Exception as e:
        print("[warn] load records: %s" % e, flush=True)


def _load_v2_records():
    if not os.path.exists(V2_RECORDS_LOG):
        return
    try:
        with open(V2_RECORDS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    V2_RECORDS.append(json.loads(line))
    except Exception as e:                                   # noqa: BLE001
        print("[warn] load v2 records: %s" % e, flush=True)


def h_add_sport_record(form, q):
    """上传跑步记录。客户端**完全不读 data**（RunningActivity 的 netOnSuccess
    直接收尾），且 code==202 亦当成功 ⇒ 本响应只为推进流程；真实请求体进 capture。
    这里额外把 21 个字段落 `main_records.jsonl`，使 `appSportRecordList` 可读回。"""
    _rt = _v1_run_type_of(form)
    if not RUN_OPEN_V1.get(_rt, True):
        return bad("999", _run_closed_msg(RUN_NAME_V1.get(_rt, "该类型"), "upload"))
    try:
        rec = {k: form.get(k) for k in SPORT_FIELD_ORDER}
        rec["_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        rec["id"] = int(time.time() * 1000)
        if form.get("environment"):          # 新链路才带；老链路无此字段
            rec["environment"] = form.get("environment")
        with LOCK:
            RECORDS.append(rec)
            with open(RECORDS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[warn] store record: %s" % e, flush=True)
    return ok({"recordId": int(time.time()), "isValid": True})


def h_sport_record_list(form, q):
    """上传后读回（闭环自证）。客户端只需 code==0/200 与 rows 可解析。"""
    uid = form.get("userId")
    rows = [r for r in RECORDS if not uid or str(r.get("userId")) == str(uid)]
    return ok({"total": len(rows), "rows": rows, "pageIndex": 1, "pageSize": 10})


def h_sum_sport_record(form, q):
    tot = 0.0
    for r in RECORDS:
        try:
            tot += float(r.get("formatSportRange") or 0)
        except (TypeError, ValueError):
            pass
    return ok({"sumDistance": round(tot, 3), "sumCount": len(RECORDS),
               "sumTime": 0, "validCount": len(RECORDS)})


def h_get_record_by_id(form, q):
    rid = form.get("sportRecordId") or form.get("id")
    for r in RECORDS:
        if str(r.get("id")) == str(rid):
            return ok(r)
    return ok({})

def h_sport_setting(form, q):
    """跑步配置 = RunRoleSetBean（17-接口规格书 §5.4）。
    isStartFace/isEndFace=0 ⇒ **关掉两端人脸**；list=[] ⇒ 无必打点。"""
    _rt = _v1_run_type_of(form)
    if not RUN_OPEN_V1.get(_rt, True):      # 老链路同样守门（1 晨跑）
        return bad("999", _run_closed_msg(RUN_NAME_V1.get(_rt, "该类型"), "setting"))
    return ok({
        "context": "",
        "identify": "mock-identify-0001",
        "runType": _rt,
        "pattern": 1,
        "totalRange": 2000.0,
        "surplusNum": 0,
        "isClock": 0,
        "isStartFace": 0,
        "isEndFace": 0,
        "isQuestion": 0,
        "faceTimeout": 0,
        "faceDistanceOut": 0.0,
        "middleFaces": [],
        "geofence": [[{"lat": "23.05", "lng": "113.42"}]],
        "list": [],
    })

def h_runing_face(form, q):
    """跑步中的人脸打卡（老链路）。除了回「需要/通过」，把上报内容落盘，
    让「人脸点 → 打卡上报」这条链也能自证（图片可放 file_b64，非必需）。"""
    try:
        rec = {
            "_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "identify": form.get("identify"),
            "type": form.get("type"),
            "coordinates": form.get("coordinates"),
            "file_bytes": len(base64.b64decode(form.get("file_b64") or "")) if form.get("file_b64") else 0,
        }
        with LOCK:
            with open(FACE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[warn] store face: %s" % e, flush=True)
    return ok({"needFace": False, "pass": True, "mock": True})


# --------------------------------------------------------------------------- 环境真实性上报
# 对应 19-参考实现调研 §3 的 oppo/safe 载荷（协议路线下「第 5 关」的全部对应物）。
ENV_LOG = os.path.join(LOGDIR, "main_env_reports.jsonl")
FACE_LOG = os.path.join(LOGDIR, "main_face_reports.jsonl")
ENV_FIELDS = ("root", "selinux", "xposed", "proxy", "vpn", "separation",
              "emulator", "ptrace", "frida", "hook", "breakpoint")
ENV_DETAIL = ("selinux", "frida", "hook", "breakpoint")


def _env_checksum(oppo):
    tmp = {k: v for k, v in oppo.items() if k != "checksum"}
    return hashlib.md5(json.dumps(tmp, separators=(",", ":"), ensure_ascii=False)
                       .encode("utf-8")).hexdigest()


def h_report_environment(form, q):
    """收环境真实性上报：校验 checksum + 维度合法性，落 `main_env_reports.jsonl`。
    即使校验不过也回 returnCode:200（研究用途：先看真实上报长什么样）。"""
    rec = {"_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "deviceId": form.get("deviceId"), "identify": form.get("identify")}
    problems = []
    try:
        oppo = json.loads(form.get("oppo") or "{}")
    except Exception as e:                                   # noqa: BLE001
        oppo, _ = {}, problems.append("oppo 非 JSON: %r" % e)
    if oppo:
        if oppo.get("checksum") != _env_checksum(oppo):
            problems.append("checksum 不符")
        for name in ENV_FIELDS:
            e = oppo.get(name)
            if not isinstance(e, dict) or e.get("result") not in ("true", "false"):
                problems.append("维度 %s 非法" % name)
            elif name in ENV_DETAIL and not isinstance(e.get("detail"), list):
                problems.append("%s.detail 非数组" % name)
        rec["oppo"] = oppo
    try:
        rec["safe"] = json.loads(form.get("safe") or "{}")
    except Exception as e:                                   # noqa: BLE001
        problems.append("safe 非 JSON: %r" % e)
    rec["problems"], rec["ok"] = problems, not problems
    try:
        with LOCK:
            with open(ENV_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:                                   # noqa: BLE001
        print("[warn] store env: %s" % e, flush=True)
    return ok({"ok": rec["ok"], "problems": problems})


def h_running_start_time(form, q):
    """跑步开始时间。期望键 `startTime`（long, epoch 毫秒）—— 由 dex 判卷人钉死
    （`RunDescriptionActivity$b;->netOnSuccess` → `JsonParse;->getLong('startTime')`）。
    客户端把它 putExtra 给 `ThreeTwoOneActivity`。给『现在』即可立即开跑。"""
    return ok({"startTime": int(time.time() * 1000)})


def h_sport_specification(form, q):
    """跑步说明页要显示的 HTML。期望键 `data`（String）—— 由 dex 判卷人钉死
    （`RunDescriptionActivity$a;->netOnSuccess` → `JsonParse;->getString('data')` → 塞进 WebView）。"""
    return ok({"data": "<html><body><h3>校园跑说明（mock）</h3>"
                      "<p>本页由私服下发，仅用于离线流程验证。</p></body></html>"})


def h_check_timeout(form, q):
    """是否超时。监听器 `StartRunActivity$a` **不读 data**，只看状态码：
    `const/16 201; if-ne` ⇒ **201 = 超时分支**，其余（含 200）= 正常。
    所以我们回 200 让它继续。"""
    return ok({"timeout": False})

def h_statistics(form, q):
    return ok({"sumDistance": 0, "sumCount": 0, "sumTime": 0, "validCount": 0})

# --------------------------------------------------------------------------- 新链路（v2）
# 域名面复核（见 20-域名面复核与v2链路.md）：跑步业务在**学校自家域名**（gdufe ⇒ http://a.sxstczx.com），
# 路径 exercise/…、信封 {"code":0,"data":…}、Bearer + AES/RSA 加密参数 —— 与老链路完全不重叠。
V2_LOG = os.path.join(LOGDIR, "main_v2_records.jsonl")
V2_SIGN_AUDIT = os.path.join(LOGDIR, "main_v2_sign_audit.jsonl")


def _bearer(h):
    v = (h or "")
    return v[7:].strip() if v.lower().startswith("bearer ") else None


def _env_check(env):
    """校验 v2 记录里内嵌的 `environment`（参考 tsn_environment 的载荷）。

    参考把 `{deviceId, oppo(JSON 串), safe(JSON 串)}` 塞在 addExerciseRecord /
    exerciseRunningFace 的 `environment` 字段里（不是单独端点）—— 复算 checksum、
    逐维比对，并与 route_studio/envlib.py 共用同一份校验实现（不重写）。
    """
    if not isinstance(env, dict):
        return {"ok": False, "problems": ["environment 不是对象: %r" % type(env).__name__]}
    if ENV is None:
        return {"ok": False, "problems": ["envlib 未加载"]}
    try:
        rep = ENV.verify_payload(env)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "problems": ["verify_payload 报错: %r" % e]}
    return {"ok": bool(rep.get("ok")), "problems": list(rep.get("problems") or []),
            "risky": list(rep.get("risky") or []), "deviceId": env.get("deviceId")}


_SAMPLE_FACE = None
_EMBED_FACE_JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD0uiiiuA7j/9k="


def _sample_face_jpeg() -> bytes:
    """一张**真的** JPEG —— 给 listBasUserImageFace 的 imageRouteUrl 下载用。

    Pillow 在就现画一张；不在就退内嵌的合法小图（保证任何环境都下载得到真图）。
    """
    global _SAMPLE_FACE
    if _SAMPLE_FACE is not None:
        return _SAMPLE_FACE
    try:
        import io as _io
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (120, 120), (28, 30, 34))
        d = ImageDraw.Draw(im)
        d.ellipse((24, 18, 96, 90), fill=(224, 196, 168))
        d.ellipse((44, 46, 54, 56), fill=(30, 30, 30))
        d.ellipse((66, 46, 76, 56), fill=(30, 30, 30))
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        _SAMPLE_FACE = buf.getvalue()
    except Exception:                                        # noqa: BLE001
        _SAMPLE_FACE = base64.b64decode(_EMBED_FACE_JPEG_B64)
    return _SAMPLE_FACE


# --------------------------------------------------------------- 跑步类型的开放情况（同真实服务器口径）
# 两套编号（定义与出处见 `route_studio/tracklib.py` 的「跑步类型编号」段）：
#   v2 `sportType/runType` = 0 自由跑 / 1 阳光跑 / 2 晨跑（早操）；
#   v1 `runType`          = 1 晨跑 / 2 阳光跑 / 3 自由跑。
# 真实服务器的「晨跑」只在早上时段开放 ⇒ 这里固定记为关闭，好让离线自测能踩到这条
# （2026-09-24 用户实测就是踩在它上面：界面选「阳光跑」，真服却回「晨跑未开启」——
#  根因是客户端把「旧→新」表用了两次，把阳光跑算成了晨跑）。
# 反过来这条守门还兼**回归网**：谁再弄错编号，本 mock 会立刻回 999 把他顶出来。
RUN_OPEN_V2 = {0: True, 1: True, 2: False}
RUN_OPEN_V1 = {1: False, 2: True, 3: True}
RUN_NAME_V2 = {0: "自由跑", 1: "阳光跑", 2: "晨跑"}
RUN_NAME_V1 = {1: "晨跑", 2: "阳光跑", 3: "自由跑"}


def _run_closed_msg(name: str, where: str) -> str:
    """拒绝时回给客户端的原话：配置那步「早操跑步未开放」，上传那步「晨跑未开启」。"""
    if name == "晨跑" and where == "setting":
        return "早操跑步未开放"
    return "%s未%s" % (name, "开放" if where == "setting" else "开启")


def _v2_reject(msg: str, http: int = 999, code: int = 1) -> dict:
    """业务拒绝。真实服务器就长这样：**HTTP 999** + `{"code":1,"msg":"..."}`（2026-09-24 实测）。

    客户端 `V2Client._req` 把 **HTTP 状态码**当 code、把 body 的 `msg` 抬进错误文案，
    所以这里的 `__http` 会被 `H._handle` 真正用作响应状态码 —— 不然「错误形状」对不上真机。
    """
    return {"__http": http, "code": code, "msg": msg, "data": None, "total": 0}


def _v2_run_type_of(merged, opened) -> int:
    """取请求里的跑步类型；取不到按 1（阳光跑）算，别误伤没有这个字段的调用。"""
    src = opened if isinstance(opened, dict) and "__error" not in opened else {}
    for k in ("sportType", "runType"):
        for d in (src, merged if isinstance(merged, dict) else {}):
            try:
                return int(d.get(k))
            except (TypeError, ValueError):
                continue
    return 1


def _v1_run_type_of(form) -> int:
    """老链路 `runType`（1 晨跑 / 2 阳光跑 / 3 自由跑）；取不到按 2（阳光跑）算。"""
    try:
        return int((form or {}).get("runType"))
    except (TypeError, ValueError):
        return 2


def _v2_run_open_fields() -> dict:
    """`sumExerciseRecord` 里带的「三种类型各开没开」（客户端 `flow._v2_run_types` 读它）。"""
    return {"morningRun": {"isShow": 1 if RUN_OPEN_V2[2] else 0},
            "sunRun": {"isShow": 1 if RUN_OPEN_V2[1] else 0},
            "freedomRun": {"isShow": 1 if RUN_OPEN_V2[0] else 0}}


def h_v2(path, merged, headers, raw_body):
    """新链路应答。返回 v2 信封（code/data）。附带签名校验 + 参数解密自证。"""
    p = path.lstrip("/")
    token = _bearer(headers.get("Authorization"))
    ts = headers.get("timestamp") or merged.get("timestamp")
    got = merged.get("sign") or headers.get("sign")
    params_for_sign = {k: v for k, v in merged.items() if k != "sign"}

    sign_ok, sign_exp = V2.verify_v2(params_for_sign, token, ts, got)
    if got is not None:
        try:
            with LOCK:
                with open(V2_SIGN_AUDIT, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "path": p, "sign_ok": sign_ok,
                                        "expected": sign_exp, "got": got,
                                        "has_token": bool(token), "ts": ts}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # 大参数链路：解开 param 自证（能解开 = 我们的密钥派生理解对）
    opened = None
    if merged.get("param") and token and ts:
        try:
            opened = V2.open_enc_param(merged["param"], token, ts)
            opened["__v_ok"] = V2.check_v_field(opened, token, ts)
            if merged.get("key"):
                opened["__key_ok"] = V2.key_field_ok(merged["key"])
        except Exception as e:                                   # noqa: BLE001
            opened = {"__error": repr(e)}

    if opened is not None:
        try:
            with LOCK:
                with open(V2_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "path": p, "sign_ok": sign_ok,
                                        "opened": opened}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---- 应答 ----
    if p.endswith("auth/oauth/token"):
        pwd_ok = None
        if merged.get("password"):
            try:
                pwd_ok = bool(V2.decrypt_password(merged["password"]))
            except Exception:                                # noqa: BLE001
                pwd_ok = False
        return {"access_token": "MOCK-V2-TOKEN-%s" % GDUFE["schoolCode"],
                "refresh_token": "MOCK-V2-REFRESH",
                "token_type": "bearer", "expires_in": 3600, "scope": "server",
                "password_decoded_ok": pwd_ok}
    if p.endswith("getSetting"):
        _rt = _v2_run_type_of(merged, opened)
        if not RUN_OPEN_V2.get(_rt, True):
            return _v2_reject(_run_closed_msg(RUN_NAME_V2.get(_rt, "该类型"), "setting"))
        bean = V2.setting_bean()
        # 这所「学校」要求人脸打卡：开始 / 结束 / 中途×1（比例 0.5）—— 让「扫脸」这条路在离线也跑得到。
        # （`V2.setting_bean()` 的默认是全 0 = 免人脸；这里只改本 mock 返回的那份，不动共享默认。）
        bean["isStartFace"] = 1
        bean["isEndFace"] = 1
        bean["isMidwayFace"] = 1
        bean["middleFaces"] = [0.5]
        if token and ts:
            k = V2.session_aes_key(token, ts)
            return V2.v2ok(V2.aes_ecb_encrypt(json.dumps(bean, separators=(",", ":"),
                                                         ensure_ascii=False), k))
        return V2.v2ok(bean)
    if p.endswith("getExerciseStartTime"):
        return V2.v2ok({"startTime": int(time.time() * 1000)})
    if p.endswith("addExerciseRecord"):
        _rt = _v2_run_type_of(merged, opened)
        if not RUN_OPEN_V2.get(_rt, True):
            return _v2_reject(_run_closed_msg(RUN_NAME_V2.get(_rt, "该类型"), "upload"))
        rid = int(time.time() * 1000)
        env_check = None
        if isinstance(opened, dict) and "__error" not in opened:
            env_check = _env_check(opened.get("environment"))
            # 落 v2 记录仓：让「上传 → listExerciseRecord 读回」闭环自证。
            try:
                rec = {k: v for k, v in opened.items() if not k.startswith("__")}
                rec["_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                rec["id"] = rid
                rec["env_check"] = env_check
                with LOCK:
                    V2_RECORDS.append(rec)
                    with open(V2_RECORDS_LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:                                # noqa: BLE001
                print("[warn] store v2 record: %s" % e, flush=True)
            try:
                with LOCK:
                    with open(V2_LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                            "path": p, "env_check": env_check},
                                           ensure_ascii=False) + "\n")
            except Exception:
                pass
        return V2.v2ok({"recordId": rid, "isValid": 1, "env_check": env_check},
                       exerciseRecordId=rid)
    if p.endswith("sumExerciseRecord"):
        if V2_RECORDS:                       # 有仓就按仓算（上传后自洽）；冷启动仍是原值
            tot = 0.0
            for r in V2_RECORDS:             # v2 里程字段是 sportRange（km），兜底 gpsDistance
                try:
                    tot += float(r.get("sportRange") or r.get("gpsDistance") or 0)
                except (TypeError, ValueError):
                    pass
            return V2.v2ok(dict({"sumDistance": round(tot, 3), "sumCount": len(V2_RECORDS),
                                 "sumTime": 0, "validCount": len(V2_RECORDS)},
                                **_v2_run_open_fields()))
        return V2.v2ok(dict({"sumDistance": 2.0, "sumCount": 1, "sumTime": 0, "validCount": 1},
                            **_v2_run_open_fields()))
    if p.endswith("listExerciseRecord"):
        return V2.v2ok(V2_RECORDS, total=len(V2_RECORDS))
    if p.endswith("statisticsExerciseRecord"):
        return V2.v2ok({"sumDistance": 2.0, "sumCount": 1})
    if p.endswith("face") or p.endswith("faceVer"):
        return V2.v2ok({"needFace": False, "pass": True, "mock": True})
    if p.endswith("getLoginUserInfo"):
        return V2.v2ok({"basUserId": 10086, "id": 10086, "userId": 10086,
                        "username": "20210001", "nickName": "mock-user"})
    if p.endswith("basUserImage/listBasUserImageFace"):
        # 回一张「本人已注册底片」：URL 指向本 mock 的预览端点，客户端会真的去下载它。
        host = headers.get("Host") or ("%s:%d" % (ARGS.bind, ARGS.http_port))
        return V2.v2ok([{"id": 1, "basUserId": int(merged.get("basUserId") or 10086),
                         "fileName": "face.jpg",
                         "imageRouteUrl": "http://%s/upms/basUserImage/preview/1" % host}])
    return V2.v2ok({"__mock": "v2-unhandled", "path": p})


ROUTES = [
    ("POST", "/app/province/findAllProvince", h_province),
    ("POST", "/app/sch/listSchoolByProvinceId", h_list_school),
    ("POST", "/app/sch/getSchoolById", h_school_by_id),
    ("POST", "/app/getSchoolInfo", h_get_school_info),
    ("POST", "/app/searchSchool", h_search_school),
    ("POST", "/app/appstu/login", h_appstu_login),
    ("POST", "/app/login", h_v2_login),
    ("POST", "/app/autoLogin", h_v2_login),
    ("POST", "/app/thirdLogin", h_v2_login),
    ("POST", "/app/appSportRecord/appAddSportRecord", h_add_sport_record),
    ("POST", "/app/appSportRecord/appSportRecordList", h_sport_record_list),
    ("POST", "/app/appSportRecord/sumSportRecord", h_sum_sport_record),
    ("POST", "/app/appSportRecord/getSportRecordId", h_get_record_by_id),
    ("POST", "/app/sportRecordSetting/getSetting", h_sport_setting),
    ("POST", "/app/sportRecordSetting/setting", h_sport_setting),
    ("POST", "/app/sportRecordSetting/getRunningStartTime", h_running_start_time),
    ("POST", "/app/sportRecordSetting/checkTimeOut", h_check_timeout),
    ("POST", "/app/appSportRecord/getSportSpecification", h_sport_specification),
    ("POST", "/app/getSetting", h_sport_setting),
    ("POST", "/app/runingFace/appRuningFace", h_runing_face),
    ("POST", "/app/runingFace/isUpFaces", h_runing_face),
    ("POST", "/app/exercise/reportEnvironment", h_report_environment),
    ("POST", "/app/exerciseRunningFace/reportEnvironment", h_report_environment),
    ("POST", "/app/env/report", h_report_environment),
]
PREFIX_OK = ("/app/", "/admin/", "/api/")   # 未知路径一律回成功信封


def _parse_multipart(body: bytes, boundary: str) -> dict:
    """极简 multipart/form-data 解析：只要非文件的 name/value 字段
    （人脸打卡把 key/param 放在 multipart 里，不解析就验不了签）。"""
    out = {}
    sep = b"--" + boundary.encode()
    for chunk in body.split(sep):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"', head)
        if m and b"filename=" not in head:
            out[m.group(1).decode()] = data.rstrip(b"\r\n").decode("utf-8", "replace")
    return out


class H(BaseHTTPRequestHandler):
    server_version = "w6-mock/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- io
    def _read(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _capture(self, method, body, extra=None):
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tls": isinstance(self.connection, ssl.SSLSocket),
            "method": method, "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body_len": len(body),
        }
        if extra:
            rec.update(extra)
        try:
            rec["body"] = body.decode("utf-8")
        except Exception:
            rec["body_b64"] = base64.b64encode(body).decode()
        line = json.dumps(rec, ensure_ascii=False)
        with LOCK:
            with open(CAPTURE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        print("[cap] %s %s len=%d" % (method, self.path, len(body)), flush=True)

    def _send(self, status, payload, ct="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---------------------------------------------------------------- route
    def _handle(self, method):
        body = self._read()
        u = urlparse(self.path)
        # keep_blank_values=True：`keepId=`（空值）也必须参与签名，
        # 否则本 mock 重算的规范化串会少键、误报 MISMATCH。
        path, q = u.path, parse_qs(u.query, keep_blank_values=True)
        form = {}
        ct = (self.headers.get("Content-Type") or "").lower()
        if body:
            if "json" in ct:
                try:
                    form = json.loads(body.decode("utf-8"))
                except Exception:
                    form = {}
            elif "form-urlencoded" in ct or not ct:
                try:
                    form = {k: v[0] for k, v in
                            parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
                except Exception:
                    form = {}
            elif "multipart/form-data" in ct:
                mb = re.search(r'boundary="?([^";,]+)"?', ct)
                if mb:
                    try:
                        form = _parse_multipart(body, mb.group(1))
                    except Exception:
                        form = {}

        # ---- 签名校验（query ∪ form 参与签名，除 sign/timestamp）----
        merged = {k: v[0] for k, v in q.items()}
        merged.update(form)
        got_sign = merged.pop("sign", None)
        ts = merged.pop("timestamp", None)
        sign_ok, sign_exp, sign_canon = verify_sign(merged, ts, got_sign)
        is_v2 = bool(V2 is not None and V2.is_v2_path(path))
        extra = {"sign_ok": sign_ok, "sign_expected": sign_exp,
                 "sign_got": got_sign, "sign_canon": sign_canon,
                 "sign_mode": SIGN_MODE, "chain": "v2" if is_v2 else "v1"}
        self._capture(method, body, extra)
        if got_sign is not None:
            audit({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "path": path,
                   "sign_ok": sign_ok, "expected": sign_exp, "got": got_sign,
                   "canon": sign_canon})
            if sign_ok is False and SIGN_MODE != "off":
                tag = "REJECT" if SIGN_MODE == "enforce" else "MISMATCH"
                print("[sign %s] %s\n   got      = %s\n   expected = %s\n   canon    = %s"
                      % (tag, path, got_sign, sign_exp, sign_canon), flush=True)

        if path in ("/api/health", "/___health"):
            return self._send(200, {"status": "ok", "mock": True, "ts": int(time.time())})

        # 人脸底片预览：listBasUserImageFace 给的 imageRouteUrl 指向这里（发的是真图字节）
        if path.startswith("/upms/basUserImage/preview/"):
            return self._send(200, _sample_face_jpeg(), ct="image/jpeg")

        # ---- 新链路（v2）：学校 cloudUrl + exercise/upms 路径 + code/data 信封 ----
        if is_v2:
            try:
                resp = h_v2(path, merged, self.headers, body)
            except Exception as e:                               # noqa: BLE001
                return self._send(500, V2.v2err(500, "mock v2 handler error: %s" % e))
            if isinstance(resp, dict) and "__http" in resp:
                resp = dict(resp)                                # 业务拒绝要**真的发**那个状态码
                return self._send(int(resp.pop("__http")), resp)
            return self._send(200, resp)

        if SIGN_MODE == "enforce" and sign_ok is False:
            return self._send(200, bad("401", "mock: sign 校验失败"))

        for m, p, fn in ROUTES:
            if m == method and path == p:
                try:
                    return self._send(200, fn(form, q))
                except Exception as e:
                    return self._send(500, bad("500", "mock handler error: %s" % e))

        if path.startswith(PREFIX_OK):
            return self._send(200, ok({"__mock": "unhandled", "path": path}))
        return self._send(404, bad("404", "mock: no route for %s" % path))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def serve(tag, port, ctx):
    try:
        srv = ThreadingHTTPServer((ARGS.bind, port), H)
    except OSError as e:
        print("[%s] BIND FAIL %s:%d -> %s" % (tag, ARGS.bind, port, e), flush=True)
        return
    if ctx is not None:
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print("[%s] listening %s:%d%s" % (tag, ARGS.bind, port, " (TLS)" if ctx else ""), flush=True)
    srv.serve_forever()


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1",
                    help="默认只听 loopback（QEMU slirp 的 10.0.2.2 即宿主 127.0.0.1）；需内外网可达才用 0.0.0.0")
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--https-port", type=int, default=443)
    ap.add_argument("--cert", default=os.path.join(CA, "server-chain.pem"))
    ap.add_argument("--key", default=os.path.join(CA, "server.key"))
    ap.add_argument("--no-http", action="store_true")
    ap.add_argument("--no-https", action="store_true")
    ap.add_argument("--sign", choices=["off", "warn", "enforce"], default="warn",
                    help="签名校验：off=忽略，warn=记录不符但放行（默认），enforce=不符则拒")
    ARGS = ap.parse_args()

    global SIGN_MODE
    SIGN_MODE = ARGS.sign

    os.makedirs(LOGDIR, exist_ok=True)
    _load_records()
    _load_v2_records()
    print("capture -> %s" % CAPTURE, flush=True)
    print("sign mode = %s   (audit -> %s)" % (SIGN_MODE, SIGN_LOG), flush=True)
    print("records -> %s  (loaded %d)" % (RECORDS_LOG, len(RECORDS)), flush=True)
    print("schools loaded: %d (gdufe=%s)" % (
        len(SCHOOLS), any(s.get("schoolCode") == "gdufe" for s in SCHOOLS)), flush=True)

    ts = []
    if not ARGS.no_http:
        t = threading.Thread(target=serve, args=("http", ARGS.http_port, None), daemon=True)
        t.start(); ts.append(t)
    if not ARGS.no_https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(ARGS.cert, ARGS.key)
        t = threading.Thread(target=serve, args=("https", ARGS.https_port, ctx), daemon=True)
        t.start(); ts.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("bye", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
