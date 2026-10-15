# -*- coding: utf-8 -*-
"""envlib.py —— 环境真实性上报生成器（对应 19-参考实现调研 §3 的 oppo/safe 载荷）。

为什么要它
----------
app 的反作弊（`libemulator_check` / `libproperty_get` / `libliantian`）防的是
**「客户端进程自证自己是模拟器」**。走协议路线时不跑 apk，就没有自检对象；
但要喂给服务器的那份「环境报告」得自己造 —— 这就是本模块。

结构（与公开参考实现同形；键名/校验方式已交叉核对）
--------------------------------------------------
  oppo = { auth:"success", root/selinux/xposed/proxy/vpn/separation/emulator/
           ptrace/frida/hook/breakpoint: {result:"true"|"false"[, detail:[...]]},
           nonce, timestamp, checksum }
  safe = { sign, root, emulator, hook, debug, breakpoint, supported_abis[] }
  checksum = md5( 去掉 checksum 后的 json.dumps(oppo, separators=(',',':'), ensure_ascii=False) )

`result` 是**字符串** "true"/"false"（不是布尔）—— 这是协议细节，写错就露馅。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

# 参与上报的环境维度（顺序即协议顺序）
OPPO_FIELDS = ("root", "selinux", "xposed", "proxy", "vpn", "separation",
               "emulator", "ptrace", "frida", "hook", "breakpoint")
# 这三项带 detail 数组
DETAIL_FIELDS = ("selinux", "frida", "hook", "breakpoint")

CLEAN_SELINUX_DETAIL = ["Enforcing\n"]
CLEAN_ABIS = ["arm64-v8a"]


def bstr(v: Any) -> str:
    return "true" if v else "false"


def checksum(oppo: Dict[str, Any]) -> str:
    """去掉 checksum 后按紧凑 JSON 求 MD5 —— 与参考实现同法。"""
    tmp = {k: v for k, v in oppo.items() if k != "checksum"}
    raw = json.dumps(tmp, separators=(",", ":"), ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_oppo(flags: Optional[Dict[str, bool]] = None,
               details: Optional[Dict[str, List[str]]] = None,
               nonce: Optional[str] = None,
               timestamp_ms: Optional[int] = None) -> Dict[str, Any]:
    flags = dict(flags or {})
    details = dict(details or {})
    oppo: Dict[str, Any] = {"auth": "success"}
    for name in OPPO_FIELDS:
        entry: Dict[str, Any] = {"result": bstr(bool(flags.get(name, False)))}
        if name in DETAIL_FIELDS:
            entry["detail"] = list(details.get(name, []))
        oppo[name] = entry
    oppo["nonce"] = nonce or str(uuid.uuid4())
    oppo["timestamp"] = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    oppo["checksum"] = checksum(oppo)
    return oppo


def clean_payload(device_id: str,
                  abis: Optional[Iterable[str]] = None,
                  nonce: Optional[str] = None,
                  timestamp_ms: Optional[int] = None,
                  debug: bool = False,
                  signed: bool = True,
                  flags: Optional[Dict[str, bool]] = None,
                  details: Optional[Dict[str, List[str]]] = None,
                  hash_device_id: bool = True) -> Dict[str, Any]:
    """产出一份环境报告（默认：root/xposed/frida/emulator 全 false，selinux=Enforcing）。

    `hash_device_id=True`（默认）会把 `device_id` 先 md5 —— 对应参考实现
    `TiShiNengSdkPublic.__init__` 的 `self.deviceId = self.getMd5(deviceId)`，
    也即「传原始串」。若你手里已经是 md5 后的值，请传 `hash_device_id=False`，
    否则会**双重哈希**（与真机上报对不上）。

    `safe` 与本份 `oppo` 用**同一组 flags** 推导，保证两份不互相矛盾
    （`safe` 缺这些维度时的默认值就是 `false`）。
    """
    flags = dict(flags or {})
    d = details if details is not None else {
        "selinux": list(CLEAN_SELINUX_DETAIL), "frida": [], "hook": [], "breakpoint": []}
    oppo = build_oppo(flags=flags, details=d, nonce=nonce, timestamp_ms=timestamp_ms)
    safe = {
        "sign": bstr(signed),
        "root": bstr(bool(flags.get("root", False))),
        "emulator": bstr(bool(flags.get("emulator", False))),
        "hook": bstr(bool(flags.get("hook", False))),
        "debug": bstr(debug),
        "breakpoint": bstr(bool(flags.get("breakpoint", False))),
        "supported_abis": list(abis if abis is not None else CLEAN_ABIS),
    }
    raw_id = str(device_id)
    return {
        "deviceId": hashlib.md5(raw_id.encode("utf-8")).hexdigest() if hash_device_id else raw_id,
        "oppo": json.dumps(oppo, separators=(",", ":"), ensure_ascii=False),
        "safe": json.dumps(safe, separators=(",", ":"), ensure_ascii=False),
    }


# oppo 里的维度 → safe 里的对应键（两边必须一致，否则报告自相矛盾）
OPPO_SAFE_PAIRS = (("root", "root"), ("emulator", "emulator"),
                   ("hook", "hook"), ("breakpoint", "breakpoint"))


def verify_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验一份上报（checksum 自洽、字段齐、oppo↔safe 不矛盾）。返回诊断 dict。

    返回：`ok` / `problems` / `risky`（哪些维度被报成 true）。
    """
    out: Dict[str, Any] = {"ok": False, "problems": [], "risky": []}
    try:
        oppo = json.loads(payload.get("oppo") or "{}")
    except Exception as e:                                   # noqa: BLE001
        out["problems"].append("oppo 不是合法 JSON: %r" % e)
        return out
    if oppo.get("auth") != "success":
        out["problems"].append("oppo.auth 不是 'success': %r" % oppo.get("auth"))
    if not oppo.get("nonce"):
        out["problems"].append("oppo 缺 nonce")
    if not str(oppo.get("timestamp") or "").isdigit():
        out["problems"].append("oppo.timestamp 非数字串: %r" % oppo.get("timestamp"))
    got = oppo.get("checksum")
    exp = checksum(oppo)
    if got != exp:
        out["problems"].append("checksum 不符：got=%s exp=%s" % (got, exp))
    for name in OPPO_FIELDS:
        e = oppo.get(name)
        if not isinstance(e, dict) or "result" not in e:
            out["problems"].append("缺少维度 %s.result" % name)
        elif e["result"] not in ("true", "false"):
            out["problems"].append("%s.result 非字符串布尔: %r" % (name, e["result"]))
        elif name in DETAIL_FIELDS and not isinstance(e.get("detail"), list):
            out["problems"].append("%s.detail 非数组" % name)
        else:
            if e["result"] == "true":
                out["risky"].append(name)
    try:
        safe = json.loads(payload.get("safe") or "{}")
        for k in ("sign", "root", "emulator", "hook", "debug", "breakpoint", "supported_abis"):
            if k not in safe:
                out["problems"].append("safe 缺 %s" % k)
        if not isinstance(safe.get("supported_abis"), list) or \
                not all(isinstance(x, str) for x in (safe.get("supported_abis") or [])):
            out["problems"].append("safe.supported_abis 非字符串数组: %r" % safe.get("supported_abis"))
        # oppo ↔ safe 交叉一致性（两边矛盾 = 报告露馅）
        for ok_, sk_ in OPPO_SAFE_PAIRS:
            ov = (oppo.get(ok_) or {}).get("result")
            if ov is not None and sk_ in safe and safe[sk_] != ov:
                out["problems"].append("oppo.%s=%s 与 safe.%s=%s 矛盾" % (ok_, ov, sk_, safe[sk_]))
    except Exception as e:                                   # noqa: BLE001
        out["problems"].append("safe 不是合法 JSON: %r" % e)
    out["ok"] = not out["problems"]
    return out


def risky_flags(**on: bool) -> Dict[str, bool]:
    """构造一份「有破绽」的报告（用于负例/测试）。"""
    return {k: bool(v) for k, v in on.items() if k in OPPO_FIELDS}
