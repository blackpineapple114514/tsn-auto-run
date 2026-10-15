# -*- coding: utf-8 -*-
"""device_profile.py —— 设备 / app 身份的唯一出处。

服务器会把 `brand` / `model` / `system` / `version` / `appVersion` **原样存进记录**，
而 `v2client`（新链路）与 `tracklib.build_submit`（老链路 21 参表单）各自维护一份默认档，
两处都要改、还容易漏一处 ⇒ 统一收在这里。

约定
----
* `brand` / `model` / `system` / `version`：观测值，会原样上报（Android 的 `model` = 型号代码）；
* `abis` / `appType` / `versionCode` / `signatureMD5` / `userAgent`：让整包像对应平台客户端的信封字段；
* `IOS_DEVICE_CODE`：iOS 设备码 —— **每台机首次启动时随机生成并持久化**（见下方实现），
  仓库里不含任何真机码。

⚠️ 同一个账号应**固定一台机**：机型乱跳 = 异常信号。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import device_catalog as _DCAT   # 自动生成的机型目录（2023→今，见 tmp/mm_gen_catalog.py）

# ---------------------------------------------------------------- 设备档（观测值）
IOS_DEVICE: Dict[str, str] = {
    "brand": "iPhone",
    "model": "iPhone 14 Pro Max",
    "system": "iOS",
    "version": "26.6.1",
    "abis": ["arm64"],
}
ANDROID_DEVICE: Dict[str, str] = {
    "brand": "Xiaomi",
    "model": "25042PN24C",
    "system": "Android",
    "version": "16",
    "abis": ["arm64-v8a"],
}

DEFAULT_DEVICE: Dict[str, str] = dict(ANDROID_DEVICE)      # 默认中性档；用户可在页面改选
DEFAULT_APP_VERSION: str = "3.6.814"                       # 观测值
IOS_APP_VERSION: str = "3.6.814"
ANDROID_APP_VERSION: str = "2.0.23"

# ---------------------------------------------------------------- 机型目录（页面下拉用）
# 一份「市面常见真机」目录：给页面 <select> 用，每项 = brand/model/system/version 四件套。
# ⚠️ 服务器**逐条记录**都存这四件套 ⇒ 同一个账号应**固定一台机**（机型乱跳 = 异常信号）；
#    默认档见 DEFAULT_DEVICE（脱敏版 = 中性 Android 档；页面可改选）。
# 约定：
#   * iOS 的 `model` = 苹果**市场名**（app 原样上报的就是它）；
#   * Android 的 `model` = 各厂 `ro.product.model`（**型号代码**，不是市场名）；`name` 只是市场名，供下拉显示。
#   * iOS 的 `version` 一律 **26.6.1**：只有这个系统版本有真机抓包坐实，别的补丁号无从考证，故不臆造。
#   * Android 的 `version` = 该机当前常见的 Android 大版本（厂商公开资料）。
#   * 型号代码来源（2026-10-11 联网核对）：小米 14 系（维基/官网）、华为 Mate60 系（公开参数）、
#     OPPO Find X7 Ultra=PHY110（OPPO 官网）、vivo X100 系 V2309A/V2324A（维基/GSMArena）、
#     三星 S24U=SM-S9280 / S23U=SM-S9180（三星官方文档）、
#     荣耀 Magic7/6 与 200/X 系、一加 15/13/12/11/Ace 系（MobileModels 型号库 khwang9883）。
CATALOG: List[Dict[str, Any]] = [
    # ---------------- iPhone（默认档排最前）----------------
    {"brand": "iPhone", "model": "iPhone 14 Pro Max", "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 14 Pro",     "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 14",         "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 13 Pro Max", "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 13",         "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 15 Pro Max", "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 15 Pro",     "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 15",         "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 16 Pro Max", "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 16 Pro",     "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 16",         "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 16 Plus",    "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 17 Pro Max", "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 17 Pro",     "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 17",         "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 18 Pro Max", "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 18 Pro",     "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone 18",         "system": "iOS", "version": "26.6.1"},
    {"brand": "iPhone", "model": "iPhone SE (3rd generation)", "system": "iOS", "version": "26.6.1"},
    # ---------------- Android 旗舰（model = 型号代码）----------------
    {"brand": "Xiaomi",  "model": "23127PN0CC", "system": "Android", "version": "15", "name": "小米 14"},
    {"brand": "Xiaomi",  "model": "23116PN5BC", "system": "Android", "version": "15", "name": "小米 14 Pro"},
    {"brand": "Xiaomi",  "model": "24030PN60G", "system": "Android", "version": "15", "name": "小米 14 Ultra"},
    {"brand": "HUAWEI",  "model": "ALN-AL00",   "system": "Android", "version": "12", "name": "华为 Mate 60 Pro"},
    {"brand": "HUAWEI",  "model": "BRA-AL00",   "system": "Android", "version": "12", "name": "华为 Mate 60"},
    {"brand": "OPPO",    "model": "PHY110",     "system": "Android", "version": "15", "name": "OPPO Find X7 Ultra"},
    {"brand": "vivo",    "model": "V2324A",     "system": "Android", "version": "15", "name": "vivo X100 Pro"},
    {"brand": "vivo",    "model": "V2309A",     "system": "Android", "version": "15", "name": "vivo X100"},
    {"brand": "samsung", "model": "SM-S9280",   "system": "Android", "version": "16", "name": "三星 Galaxy S24 Ultra"},
    {"brand": "samsung", "model": "SM-S9180",   "system": "Android", "version": "16", "name": "三星 Galaxy S23 Ultra"},
    # ---------------- 荣耀 HONOR（model = 型号代码）----------------
    {"brand": "HONOR", "model": "PTP-AN10", "system": "Android", "version": "15", "name": "荣耀 Magic7 Pro"},
    {"brand": "HONOR", "model": "PTP-AN00", "system": "Android", "version": "15", "name": "荣耀 Magic7"},
    {"brand": "HONOR", "model": "BVL-AN16", "system": "Android", "version": "14", "name": "荣耀 Magic6 Pro"},
    {"brand": "HONOR", "model": "BVL-AN00", "system": "Android", "version": "14", "name": "荣耀 Magic6"},
    {"brand": "HONOR", "model": "ELP-AN00", "system": "Android", "version": "14", "name": "荣耀 200 Pro"},
    {"brand": "HONOR", "model": "ELI-AN00", "system": "Android", "version": "14", "name": "荣耀 200"},
    {"brand": "HONOR", "model": "BRP-AN00", "system": "Android", "version": "14", "name": "荣耀 X60 Pro"},
    {"brand": "HONOR", "model": "ALI-AN00", "system": "Android", "version": "13", "name": "荣耀 X50"},
    # ---------------- 一加 OnePlus（model = 型号代码）----------------
    {"brand": "OnePlus", "model": "PLK110", "system": "Android", "version": "16", "name": "一加 15"},
    {"brand": "OnePlus", "model": "PJZ110", "system": "Android", "version": "15", "name": "一加 13"},
    {"brand": "OnePlus", "model": "PJD110", "system": "Android", "version": "14", "name": "一加 12"},
    {"brand": "OnePlus", "model": "PHB110", "system": "Android", "version": "13", "name": "一加 11"},
    {"brand": "OnePlus", "model": "PKG110", "system": "Android", "version": "15", "name": "一加 Ace 5"},
    {"brand": "OnePlus", "model": "PJE110", "system": "Android", "version": "14", "name": "一加 Ace 3"},
    {"brand": "OnePlus", "model": "PHK110", "system": "Android", "version": "13", "name": "一加 Ace 2"},
    {"brand": "OnePlus", "model": "NE2210", "system": "Android", "version": "13", "name": "一加 10 Pro"},
]

# 2026-10-11：机型目录大扩充 —— 追加 2023→今 主流机型（自动生成，源 = MobileModels-csv）。
# 同 (brand, model) 已在上面手工档里的跳过，保持手工档的默认排序（iPhone 打头）。
for _b, _m, _s, _v, _n in _DCAT.ENTRIES:
    if not any(d["brand"] == _b and d["model"] == _m for d in CATALOG):
        CATALOG.append({"brand": _b, "model": _m, "system": _s, "version": _v, "name": _n})

# 下拉分组名（页面按品牌分组，比「iOS/安卓」二分更好找）
BRAND_GROUP: Dict[str, str] = {
    "iPhone": "iPhone",
    "Xiaomi": "小米 / 红米",
    "HUAWEI": "华为",
    "HONOR": "荣耀",
    "OPPO": "OPPO",
    "vivo": "vivo / iQOO",
    "OnePlus": "一加",
    "realme": "真我",
    "samsung": "三星",
}


def catalog() -> List[Dict[str, Any]]:
    """机型目录（供页面下拉）：每项带 `id`/`label`，其余字段与设备档同形。"""
    out: List[Dict[str, Any]] = []
    for d in CATALOG:
        name = d.get("name") or d["model"]
        label = d["model"] if name == d["model"] else "%s（%s）" % (name, d["model"])
        out.append({"id": "|".join([d["brand"], d["model"]]),
                    "label": label, "brand": d["brand"], "model": d["model"],
                    "system": d["system"], "version": d["version"],
                    "group": BRAND_GROUP.get(d["brand"], d["brand"])})
    return out


def find(brand: Optional[str], model: Optional[str]) -> Optional[Dict[str, Any]]:
    """按 brand+model 在目录里找一台；找不到返回 None。"""
    b, m = (brand or "").strip(), (model or "").strip()
    for d in CATALOG:
        if d["brand"] == b and d["model"] == m:
            return dict(d)
    return None

# ---------------------------------------------------------------- 设备码（观测值）
# 真机 iPhone 14 Pro Max **原样**发的 `uniqueCode`（2026-09-27 USB 抓包，见
# `docs/ios-notes.md` §3.1）：**UUID 形态**，App 自己生成、非硬件派生。
# ⚠️ 别对它 md5 —— 安卓参照实现（`TiShiNengSdkPublic.__init__`）是
#    `deviceId = self.getMd5(deviceId)`，但 **iOS 真机发的是原始 UUID**。
#    旧实现无脑 md5 ⇒ 挂 iOS 的「名」却发安卓形态的「号」，名/号不同源（2026-10-10 修）。
def _local_ios_code() -> str:
    """本机 iOS 设备码：首启随机生成并持久化（UUID 形态，大写）。"""
    import os as _os, uuid as _uuid
    _p = _os.path.join(_os.path.expanduser("~"), ".gdfe_run_ios_code")
    try:
        with open(_p, "r", encoding="utf-8") as _f:
            _c = _f.read().strip()
        if _c:
            return _c
    except Exception:                                        # noqa: BLE001
        pass
    _c = str(_uuid.uuid4()).upper()
    try:
        with open(_p, "w", encoding="utf-8") as _f:
            _f.write(_c)
    except Exception:                                        # noqa: BLE001
        pass
    return _c


IOS_DEVICE_CODE: str = _local_ios_code()

# ---------------------------------------------------------------- 信封（推断值）
IOS_ENVELOPE: Dict[str, Any] = {
    "appType": "iOS",
    "versionCode": 36814,                                  # iOS 无 versionCode；仅占位
    # ⚠️ **2026-09-24 21:0x 改**：原值 `""`（空签名）是纯推断，且与加密参数里的
    #    `v`（`v2_chain.APP_SIGN_HASH`，官方客户端的**硬编码**包签名散列，两个平台同源）
    #    对不上 —— 服务端若拿 `signatureMD5 ↔ v` 对账，「空签名 + 官方 v」这个组合真机拼不出来。
    #    真机 iOS 客户端要么照发同一个硬编码常量、要么干脆不带这个键；"" 两种都不是。
    #    这里先取**唯一有据可依**的值：官方 Android 包的那张签名常量（与 `v` 同源）。
    "signatureMD5": "7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3",
    # 2026-09-27 **实测值**（USB 抓包，见 docs/ios-notes.md）：
    # 真机 iOS 的 UA 是 `StudentsMarch/<appVersion> (iPhone; iOS <系统>; Scale/3.00)`，
    # 不是猜的 `TiShiNengRunning/…`。
    "userAgent": "StudentsMarch/3.6.814 (iPhone; iOS 26.6.1; Scale/3.00)",
    "deviceCode": IOS_DEVICE_CODE,          # 设备码：iOS = 原始 UUID（**不** md5）
}
ANDROID_ENVELOPE: Dict[str, Any] = {
    "appType": "Android",
    "versionCode": 20230,
    "signatureMD5": "7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3",
    "userAgent": "okhttp/4.9.0",
    "deviceCode": None,                     # 设备码：安卓 = md5(raw_device_id)（参照实现）
}

_DEV_KEYS = ("brand", "model", "system", "version", "abis")


def is_ios(device: Optional[Dict[str, Any]]) -> bool:
    """按 `system` / `brand` 判是不是苹果档（`iOS` / `iPadOS` / 品牌含 iPhone/Apple）。"""
    d = device or {}
    sysl = str(d.get("system") or "").strip().lower()
    br = str(d.get("brand") or "").strip().lower()
    return sysl in ("ios", "ipados", "iphone os") or "iphone" in br or br == "apple"


def resolve(device: Optional[Dict[str, Any]] = None,
            app_version: Optional[str] = None) -> Dict[str, Any]:
    """归一化一份设备档。

    只认 `brand/model/system/version/abis`（**其余键一律忽略** —— 调用方常常把整份上传
    表单塞进来）；空值 / 空串回落到默认档；再按 `system` 补齐
    `appType` / `versionCode` / `signatureMD5` / `userAgent` / `appVersion`。
    """
    d: Dict[str, Any] = dict(DEFAULT_DEVICE)
    if device:
        for k in _DEV_KEYS:
            v = device.get(k)
            if v not in (None, "", []):
                d[k] = v
    fam = IOS_ENVELOPE if is_ios(d) else ANDROID_ENVELOPE
    if not (device and device.get("abis")):
        d["abis"] = list(IOS_DEVICE["abis"] if fam is IOS_ENVELOPE else ANDROID_DEVICE["abis"])
    d.update(fam)
    d["appVersion"] = str(app_version or (IOS_APP_VERSION if fam is IOS_ENVELOPE
                                          else ANDROID_APP_VERSION))
    if fam is IOS_ENVELOPE:
        # UA 里的 iOS 版本**随所选机型走**（默认档 version=26.6.1 时与本文件 IOS_ENVELOPE
        # 的实测串逐字节一致）；换别的版本也不会再名/号不一。
        d["userAgent"] = "StudentsMarch/%s (iPhone; iOS %s; Scale/3.00)" % (
            d["appVersion"], d["version"])
    return d


def device_of(form: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """从一份上传表单（老链路 21 字段，或新链路 `gen['submit']`）里抠出**设备四件套**。

    找不到就返回空 dict（调用方再回落到默认档）。
    """
    form = form or {}
    return {k: form[k] for k in ("brand", "model", "system", "version") if form.get(k)}
