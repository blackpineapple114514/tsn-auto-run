# -*- coding: utf-8 -*-
"""replay.py —— 全流程重放（老链路 sysType=2）。

把 route_studio 的「地图折线 → 生成的跑步记录」塞进完整的校园跑链路，
一次跑通并自证闭环：

  1. getSchoolInfo           → 分流开关 sysType=2
  2. appstu/login            → token
  3. sportRecordSetting/getSetting → identify
  4. getRunningStartTime     → startTime（真跑用的开跑时刻）
  5. reportEnvironment       → 环境真实性上报（19 §3 的 oppo/safe，校验 checksum）
  6. runingFace/appRuningFace × N → 人脸点打卡（两端 + 中途）
  7. appSportRecord/appAddSportRecord → 21 参上传（轨迹/步数桶）
  8. appSportRecord/appSportRecordList → **读回**刚上传的记录
  9. appSportRecord/sumSportRecord     → 汇总

用法
----
  python replay.py                    # 自起 mock，跑一遍
  python replay.py --base http://127.0.0.1:18080
  python replay.py --km 3 --pace 300 --faces 0.3,0.7 --crf
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
PROJ = os.path.dirname(TOOLS)
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS)

import tracklib as T          # noqa: E402
import envlib as E            # noqa: E402
import server as S            # noqa: E402
import device_profile as DP    # noqa: E402  （设备 / app 身份的唯一出处）

OKS, FAILS = [], []
REPORT = os.path.join(PROJ, "30-logs", "route_studio_replay.out")
SCHOOL_ID, SCHOOL_CODE = "184", "gdufe"
UNAME, PWD = "20210001", "mock-pass"
DEV = {k: DP.DEFAULT_DEVICE[k] for k in ("brand", "model", "system", "version")}   # 真机 iPhone 档


def log(m): print(m, flush=True)


def check(name, cond, detail=""):
    (OKS if cond else FAILS).append(name)
    log("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  <- " + detail) if detail else ""))


def oval(lat=23.0500, lng=113.4200, radius_m=140.0, n=60):
    rlat = radius_m / 111320.0
    rlng = radius_m / (111320.0 * math.cos(math.radians(lat)))
    return [[lng + rlng * math.cos(2 * math.pi * i / n),
             lat + rlat * math.sin(2 * math.pi * i / n)] for i in range(n + 1)]


def run(base, km, pace, faces, seed):
    log("\n=== 全流程重放 @ %s ===" % base)
    trace = {}

    # 1 选校
    r = S._post_form(base, "/app/getSchoolInfo", {"schoolId": SCHOOL_ID, "model": DEV["model"]})
    d = r.get("data") or {}
    trace["getSchoolInfo"] = r
    check("1 getSchoolInfo：sysType==2 ⇒ 老链路", d.get("sysType") == "2", "got=%s" % d.get("sysType"))
    check("1b schoolCode == %s" % SCHOOL_CODE, d.get("schoolCode") == SCHOOL_CODE)

    # 2 登录
    r = S._post_form(base, "/app/appstu/login", {"uname": UNAME, "pwd": PWD})
    d = r.get("data") or {}
    token = d.get("token")
    user_id = d.get("id") or UNAME
    trace["login"] = r
    check("2 appstu/login 拿到 token", bool(token), "token=%s" % token)

    # 3 配置
    # 老链路 runType 用**老编号**（1 晨跑 / 2 阳光跑 / 3 自由跑）：这里要的是「阳光跑」= 2
    r = S._post_form(base, "/app/sportRecordSetting/getSetting", {"runType": "2", "uid": str(user_id)})
    cfg = r.get("data") or {}
    identify = cfg.get("identify") or "identify-%d" % int(time.time())
    trace["getSetting"] = r
    check("3 getSetting 返回 identify", bool(cfg.get("identify")))

    # 4 开跑时刻
    r = S._post_form(base, "/app/sportRecordSetting/getRunningStartTime", {"identify": identify})
    start_ms = int((r.get("data") or {}).get("startTime") or int(time.time() * 1000))
    trace["getRunningStartTime"] = r
    check("4 getRunningStartTime 返回 startTime", start_ms > 0, str(start_ms))

    # 5 环境上报
    env = E.clean_payload(device_id="route-studio-device", abis=["arm64-v8a"])
    env_check = E.verify_payload(env)
    check("5a 本地生成的环境载荷自洽（checksum 对）", env_check["ok"], str(env_check["problems"]))
    r = S._post_form(base, "/app/exercise/reportEnvironment", dict(env, identify=identify))
    trace["reportEnvironment"] = r
    ok_env = bool((r.get("data") or {}).get("ok"))
    check("5b mock 收下环境上报且校验通过", ok_env,
          str((r.get("data") or {}).get("problems")))

    # 6 生成轨迹（含两端 + 中途人脸）
    gen = T.generate(path=oval(), target_km=km, plan_pace_s_per_km=pace,
                     start_ms=start_ms, run_type=1, device=DEV, identify=identify,
                     user_id=str(user_id),
                     sample_interval_s=1.0, speed_jitter=0.08, loop_mode="repeat",
                     crs="gcj02", start_face=True, end_face=True, middle_faces=faces,
                     seed=seed)
    check("6a 轨迹自校验通过", gen["validate"]["ok"], str(gen["validate"]["issues"])[:160])
    check("6b 距离 ≈ 目标", abs(gen["metrics"]["distance_m"] - km * 1000) <= 1.0,
          "%.2f" % gen["metrics"]["distance_m"])
    trace["generate"] = {"metrics": gen["metrics"], "validity": gen["validity"], "faces": gen["faces"]}

    # 7 人脸打卡（逐个）
    face_ok = 0
    for f in gen["faces"]:
        rr = S._post_form(base, "/app/runingFace/appRuningFace",
                          {"identify": identify, "type": str(f["type"]),
                           "coordinates": f["coordinates"], "runType": "2"}, token=token)
        if str(rr.get("returnCode")) == "200":
            face_ok += 1
    check("7 人脸点全部打卡成功", face_ok == len(gen["faces"]),
          "%d/%d" % (face_ok, len(gen["faces"])))
    trace["faces_posted"] = [{"type": f["type"], "coordinates": f["coordinates"]} for f in gen["faces"]]

    # 8 上传
    # 与 `server._submit` 的老出口同款换算：本仓表单按新编号生成，老链路必须换成老编号
    form = dict(gen["submit"])
    form["runType"] = str(T.v1_run_type(form.get("runType")))
    r = S._post_form(base, "/app/appSportRecord/appAddSportRecord", form, token=token)
    trace["addSportRecord"] = r
    check("8 appAddSportRecord 成功", str(r.get("returnCode")) == "200", json.dumps(r)[:160])

    # 9 读回
    r = S._post_form(base, "/app/appSportRecord/appSportRecordList",
                     {"userId": str(user_id), "sportType": "1", "pageIndex": "1", "pageSize": "10"})
    rows = (r.get("data") or {}).get("rows") or []
    mine = [x for x in rows if x.get("identify") == identify]
    trace["appSportRecordList"] = {"rows": len(rows)}
    check("9a 记录能读回（闭环）", len(mine) == 1, "rows=%d" % len(rows))
    if mine:
        m0 = mine[0]
        try:
            import mock_boxkj as _M
            order = _M.SPORT_FIELD_ORDER
        except Exception:                                    # noqa: BLE001
            order = []
        check("9b 读回 21 字段完整", all(k in m0 for k in order),
              "缺: %s" % [k for k in order if k not in m0])
        check("9c 读回距离与上传一致",
              m0.get("formatSportRange") == gen["submit"]["formatSportRange"],
              "%s vs %s" % (m0.get("formatSportRange"), gen["submit"]["formatSportRange"]))
        try:
            pts = json.loads(m0["gitudeLatitude"])
            check("9d 读回轨迹可再解析（老链路键名）",
                  isinstance(pts, list) and "latitude" in pts[0], "n=%d" % len(pts))
        except Exception as e:                               # noqa: BLE001
            check("9d 读回轨迹可再解析（老链路键名）", False, repr(e))

    # 10 汇总
    r = S._post_form(base, "/app/appSportRecord/sumSportRecord", {"userId": str(user_id)})
    d = r.get("data") or {}
    trace["sumSportRecord"] = d
    check("10 sumSportRecord 计数 >= 1", (d.get("sumCount") or 0) >= 1, str(d))

    return trace


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None)
    ap.add_argument("--km", type=float, default=2.0)
    ap.add_argument("--pace", type=float, default=330)
    ap.add_argument("--faces", default="0.3,0.7", help="中途人脸比例，逗号分隔；'' 表示无")
    ap.add_argument("--seed", type=int, default=20260923)
    a = ap.parse_args()

    faces = [float(x) for x in a.faces.split(",") if x.strip()] if a.faces else []

    srv = None
    base = a.base
    if not base:
        import mock_boxkj as M
        for p in (M.CAPTURE, M.RECORDS_LOG):
            try:
                os.path.exists(p) and os.remove(p)
            except Exception:
                pass
        srv = M.ThreadingHTTPServer(("127.0.0.1", 0), M.H)
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("[mock] 自起于 %s" % base)

    try:
        trace = run(base, a.km, a.pace, faces, a.seed)
    finally:
        if srv:
            srv.shutdown()

    n = len(OKS) + len(FAILS)
    log("\n==== 全流程重放：%d 项检查，%d FAIL ====" % (n, len(FAILS)))
    for f in FAILS:
        log("   FAIL: %s" % f)
    try:
        with open(REPORT, "w", encoding="utf-8") as fh:
            fh.write("route_studio 全流程重放 @ %s\n%s\n%d 项 / %d FAIL\n\n"
                     % (base, "-" * 46, n, len(FAILS)))
            fh.write(json.dumps(trace, ensure_ascii=False, indent=1))
        log("-> %s" % REPORT)
    except Exception:
        pass
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
