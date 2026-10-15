# -*- coding: utf-8 -*-
"""v2_chain.py —— 新链路（v2）契约：学校 cloudUrl + exercise/upms 路径。

为什么要它（对应「域名面复核」第 3 项）
--------------------------------------
`18` 时代我们只劫持 `m.boxkj.com`（老链路 sysType=2，`/app/**` + `returnCode`）。
但参考实现 `TiShiNengSdkPublic` 走的是**另一条链路**：

  * Host   = 学校自己的 `cloudUrl`（gdufe ⇒ `http://a.sxstczx.com/`，
             来自老链路 `getSchoolInfo` 返回的 `lan_url`）
  * Path   = 相对路径（`exercise/exerciseSetting/2a36d143/getSetting` 等，见 ROUTES）
  * 信封   = `{"code":0,"data":...,"msg":...}`（**不是** returnCode）
  * 鉴权   = `Authorization: Bearer <token>` + 头 `sign`/`timestamp`
  * 参数   = 大整数参数走 AES-ECB 加密后以 `key`(RSA 加密的 AES 密钥) + `param` 提交
  * 签名   = md5( base64( AES-CBC( unquote( sorted("k=v&") ) ) ) )，
             key/iv = md5(token)[0:16] / md5(token)[16:32]

本模块把这条链路**双向实现**：既能构造（cross-validation 用），也能校验（mock 用）。
密钥/公钥/常量与 `15-精确标识符台账.md` 一致。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, unquote

from Crypto.Cipher import AES

# --------------------------------------------------------------------------- 常量
V2_APP_ID = "c9292ee89d2f49492f983f5931af0d09"           # 公版 appId（v2 用）
V2_APP_SECRET = "e8167ef026cbc5e456ab837d9d6d9254"
APP_SIGN_HASH = "d2tnIyqximO/L8Y4MzfRELa1hSAtSRxzmvXlcOCzRyk="
RSA_PUB_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgzQ7BYqBZ5LTjoOb9aHO8fI0hbww9YRW2lnqIdDy"
    "IjBwmhthTR+EmiKNm4yFKg6Vz2GW5ix3IdUQdaAq3ZZ7se/dCOTpu3dk15ZgkO6ZImUE7gqzSXXJ0NaACudk"
    "4yJwk3Q69kB4m3xIKxiOlG2HtbEed01LrUmLag9VOP96BuSao2sP4Als5hA/8C6KqdihTOcZF1RT+lqrT3Qv"
    "ja7q+qI5QZw9d7NrFFycQs8jk8O49f9mkvLZRZCCWEbwzuCPTlMy/ZNAsMeU/gNSRKUnquOiPboc2KUhsvY4"
    "cK0GeuS9vuIrMGE01L/BCc+rUrautq3n3WiIVJwnwWiJtgk33QIDAQAB")
RSA_MOD_BYTES = 256                                       # 2048-bit ⇒ 256 B ⇒ b64 344 字符

# 学校 cloudUrl → Host 提示（判定「这条请求属于新链路」时参考）
SCHOOL_HOSTS = ("a.sxstczx.com", "h.tsnkj.com", "a.tsnkj.com")

# 相对路径表（源自参考实现 tsn_routes.ROUTE_NAMES，只取相对 URL 的那些）
ROUTES: Dict[str, str] = {
    "getAppid": "upms/sysSchool/getAppid",
    "getAppSocketServer": "upms/sysSchool/getAppSocketServer",
    "sumExerciseRecord": "exercise/exerciseRecord/sumExerciseRecord",
    "getExerciseSetting": "exercise/exerciseSetting/2a36d143/getSetting",
    "getExerciseExplanation": "exercise/exerciseExplanation/getExerciseExplanationV2",
    "addExerciseRecord": "exercise/exerciseRecord/2a36d143/addExerciseRecord",
    "listExerciseRecord": "exercise/exerciseRecord/2a36d143/listExerciseRecord",
    "getExerciseRecord": "exercise/exerciseRecord/2a36d143/getExerciseRecord",
    "statisticsExerciseRecord": "exercise/exerciseRecord/statisticsExerciseRecord",
    "addExerciseFeedback": "exercise/exerciseFeedback/addExerciseFeedback",
    "addUserFace": "upms/basUserImage/addUserFace",
    "exerciseRunningFace": "exercise/exerciseRunningFace/2a36d143/face",
    "checkTimeOut": "exercise/exerciseSetting/checkTimeOut",
    "faceVer": "exercise/exerciseRunningFace/faceVer",
    "getExerciseStartTime": "exercise/exerciseSetting/2a36d143/getExerciseStartTime",
    "addStuGymClockRecord": "app/stuGymClockRecord/addStuGymClockRecord",
    "oauthToken": "auth/oauth/token",
    "listMenu": "upms/app/listMenu",
    "getLoginUserInfo": "upms/basUser/getLoginUserInfo",
    "logout": "auth/token/logout",
    "messageArticleListByType": "upms/messageArticle/listByType",
    "isDefaultPass": "upms/basUser/isDefalutPass",
    "getFeedbackBalance": "exercise/exerciseFeedback/getFeedbackBalance",
    "latestUnreadNotice": "upms/messageNotice/getLatestUnreadNotice",
    "messageNoticeById": "upms/messageNotice/getMessageNoticeVoById",
    "getLoginUserInfoThird": "upms/basUser/getLoginUserInfoThird",
    "clickArticle": "upms/messageArticleClickRecord/appClickArticle",
    "checkExerciseRecord": "exercise/exerciseRecord/checkExerciseRecord",
    "getH5Url": "upms/app/getH5Url",
    "getSchoolDetail": "upms/sysSchool/getSchoolDetail",
    "getExerciseQuestion": "exercise/exerciseSetting/getExerciseQuestion",
    "getWeather": "upms/app/getWeather",
    "saveWeather": "upms/app/saveWeather",
    "sendFace2DeviceResult": "exercise/exerciseSetting/sendFace2DeviceResult",
    # 注意：这条不在参考的静态表 ROUTE_NAMES 里，而是代码里直接调用的
    # （`TiShiNengSdkPublic.listBasUserImageFace()` L370-372，`tsnRunServer.py:121` 用到）。
    "listBasUserImageFace": "upms/basUserImage/listBasUserImageFace",
}
# 判定「这是新链路路径」的前缀
V2_PREFIXES = ("exercise/", "upms/", "auth/", "app/stuGymClockRecord")

# 厂商默认基础域（参考 `tsn_routes.defaultBaseUrl`）：学校无自有 cloudUrl 时用它。
# 即「第三条域名面」——不是 m.boxkj.com，也不是某校 cloudUrl。
VENDOR_DEFAULT_BASE = "https://a.tsnkj.com/"
# 已知的「非 cloudUrl」主机（老链路 / 静态页 / 分发 / 圈子），用于分流与文档核对
OTHER_HOSTS = ("m.boxkj.com", "h.tsnkj.com", "www.pgyer.com", "circle.h5.tsnkj.com")


# --------------------------------------------------------------------------- 编码原语
def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode()


def _cbc_key_iv(token: str) -> Tuple[bytes, bytes]:
    t = md5(token)
    return t[0:16].encode(), t[16:32].encode()


def _pad(data: bytes) -> bytes:
    n = AES.block_size - len(data) % AES.block_size
    return data + bytes([n] * n)


def _unpad(data: bytes) -> bytes:
    return data[:-data[-1]] if data else data


def aes_cbc_encrypt(text: str, key: bytes, iv: bytes) -> str:
    c = AES.new(key=key, mode=AES.MODE_CBC, IV=iv)
    return base64.b64encode(c.encrypt(_pad(text.encode("utf-8")))).decode()


def aes_cbc_decrypt(b64: str, key: bytes, iv: bytes) -> str:
    c = AES.new(key=key, mode=AES.MODE_CBC, IV=iv)
    return _unpad(c.decrypt(base64.b64decode(b64))).decode("utf-8")


def aes_ecb_encrypt(text: str, key: bytes) -> str:
    c = AES.new(key=key, mode=AES.MODE_ECB)
    return base64.b64encode(c.encrypt(_pad(text.encode("utf-8")))).decode()


def aes_ecb_decrypt(b64: str, key: bytes) -> str:
    c = AES.new(key=key, mode=AES.MODE_ECB)
    return _unpad(c.decrypt(base64.b64decode(b64))).decode("utf-8")


# --------------------------------------------------------------------------- 登录密码
# 参考 `TiShiNengSdkPublic.__init__`: `AESCrypto('thanks,pig4cloud','thanks,pig4cloud')`
# 并用 `encrypt(password, zero_padding=True)`（**零填充**，不是 PKCS7）。
PASSWORD_KEY = "thanks,pig4cloud"


def aes_cbc_encrypt_zero(text: str, key: bytes, iv: bytes) -> str:
    """CBC + 零填充（对齐参考 `zero_pad`：长度已是 16 倍数时仍补满 16 字节）。"""
    data = text.encode("utf-8")
    data += b"\x00" * (AES.block_size - len(data) % AES.block_size)
    return base64.b64encode(AES.new(key=key, mode=AES.MODE_CBC, IV=iv).encrypt(data)).decode()


def encrypt_password(pwd: str) -> str:
    k = PASSWORD_KEY.encode()
    return aes_cbc_encrypt_zero(pwd, k, k)


def decrypt_password(b64: str) -> str:
    k = PASSWORD_KEY.encode()
    raw = AES.new(key=k, mode=AES.MODE_CBC, IV=k).decrypt(base64.b64decode(b64))
    return raw.rstrip(b"\x00").decode("utf-8")


# --------------------------------------------------------------------------- v2 签名
def build_auth_info(params: Dict[str, Any]) -> str:
    """sorted 的 `k=v&...`；None→"null"，bool→"true"/"false"，其余 str()。"""
    parts = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            t = "null"
        elif isinstance(v, bool):
            t = "true" if v else "false"
        else:
            t = str(v)
        parts.append("%s=%s" % (k, t))
    return "&".join(parts)


def aes_appid(ts: str, token: str) -> str:
    key, iv = _cbc_key_iv(token)
    return md5(aes_cbc_encrypt(V2_APP_ID + token + ts, key, iv))


def aes_appsecret(ts: str, token: str) -> str:
    key, iv = _cbc_key_iv(token)
    return md5(aes_cbc_encrypt(token + ts, key, iv))


def v2_sign(params: Dict[str, Any], token: str, ts: str) -> str:
    """md5( base64( AES-CBC( unquote(sorted k=v& 含 appId/appSecret) ) ) )。"""
    p = dict(params)
    p["appId"] = aes_appid(ts, token)
    p["appSecret"] = aes_appsecret(ts, token)
    auth = build_auth_info(p)
    key, iv = _cbc_key_iv(token)
    return md5(aes_cbc_encrypt(unquote(auth), key, iv))


def verify_v2(params: Dict[str, Any], token: str, ts: str, got: str) -> Tuple[Optional[bool], Optional[str]]:
    """校验 v2 sign；token/ts/got 缺一不可，否则返回 (None, None) 表示无法判定。"""
    if not token or not ts or not got:
        return None, None
    exp = v2_sign(params, token, ts)
    return (unquote(got) == exp or got == exp), exp


# --------------------------------------------------------------------------- 加解密参数
def session_aes_key(token: str, ts: str) -> bytes:
    """大参数链路的 AES-ECB 密钥 = md5(token+timestamp)[0:16]（16 字符 ASCII）。"""
    return hashlib.md5(("%s%s" % (token, ts)).encode("utf-8")).hexdigest()[0:16].encode()


def rsa_pub_pem(b64: str = RSA_PUB_B64) -> bytes:
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return ("-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n").encode()


def rsa_encrypt(data: bytes) -> str:
    """PKCS#1 v1.5（等价参考实现的 `rsa.encrypt`）——加密 16 字节 AES 密钥。"""
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA as _RSA
    key = _RSA.import_key(rsa_pub_pem())
    return base64.b64encode(PKCS1_v1_5.new(key).encrypt(data)).decode()


def build_enc_params(params: Dict[str, Any], token: str, ts: str, encoded: bool = False,
                     raw: bool = False) -> Dict[str, str]:
    """构造 {key, param}：param = AES-ECB(sorted json 含 v)，key = RSA(aesKey)。

    三种 wire 形态：
      * raw=True（multipart 人脸用）：原样 base64（`+` / `/` 不动）——
        对齐参考 `getFaceEncParams`；真机 face 体实测 key 里 `plus=5 / space=0`。
      * encoded=False（默认，getExerciseSetting 用）：`+` 换成空格
      * encoded=True（addExerciseRecord 用）：整体 `quote()`
    """
    k = session_aes_key(token, ts)
    p = dict(params)
    p["v"] = aes_ecb_encrypt(APP_SIGN_HASH, k)
    body = json.dumps({key: p[key] for key in sorted(p.keys())},
                      separators=(",", ":"), ensure_ascii=False)
    kb, pb = rsa_encrypt(k), aes_ecb_encrypt(body, k)
    if raw:
        return {"key": kb, "param": pb}
    if encoded:
        # 真机上传体实测：一级 quote 把 `/` 也编了（`/`→`%2F`），再被表单编一次 ⇒ `%252F`。
        # 故 safe=""（默认 safe='/' 会漏编 `/`，真机是 `%252F` 而非 `%2F`）。
        return {"key": quote(kb, safe=""), "param": quote(pb, safe="")}
    return {"key": kb.replace("+", " "), "param": pb.replace("+", " ")}


def open_enc_param(param_b64: str, token: str, ts: str) -> Dict[str, Any]:
    """解开 param（AES-ECB）→ dict。

    容错参考实现的两种 wire 形态：
      * `is_encoded=True`：param 经 `quote()`（`+`⇒`%2B`），再被 http 库编一次
        ⇒ mock 侧 `parse_qs` 只解回一层，这里再 `unquote()` 一次。
      * `is_encoded=False`：`+` 被换成**空格**（参考源码 `.replace('+',' ')`）。
    两者归一后都能 b64decode。
    """
    k = session_aes_key(token, ts)
    s = unquote(param_b64)
    if " " in s:
        s = s.replace(" ", "+")
    s = s.strip()
    s += "=" * (-len(s) % 4)
    return json.loads(aes_ecb_decrypt(s, k))


def check_v_field(decoded: Dict[str, Any], token: str, ts: str) -> bool:
    """decoded['v'] 应 == AES-ECB(appSignHash) ⇒ 证明客户端用的是对的那张证书。"""
    k = session_aes_key(token, ts)
    return decoded.get("v") == aes_ecb_encrypt(APP_SIGN_HASH, k)


def key_field_ok(key_b64: str) -> bool:
    """key 应是 RSA-2048 密文的 base64（解出 256 字节）；容错两种 wire 形态。"""
    try:
        s = unquote(key_b64).replace(" ", "+").strip()
        s += "=" * (-len(s) % 4)
        return len(base64.b64decode(s)) == RSA_MOD_BYTES
    except Exception:
        return False


# --------------------------------------------------------------------------- v2 信封
def v2ok(data: Any = None, total: int = 0, **extra) -> Dict[str, Any]:
    d = {"code": 0, "msg": "ok", "data": data, "total": total}
    d.update(extra)
    return d


def v2err(code: int, msg: str) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": None, "total": 0}


def is_v2_path(path: str) -> bool:
    p = path.lstrip("/")
    return any(p.startswith(x) for x in V2_PREFIXES)


def route_path(name: str) -> str:
    return ROUTES[name]


# --------------------------------------------------------------------------- v2 业务应答
def setting_bean(range_km: float = 2.0, identify: str = "v2-identify-0001") -> Dict[str, Any]:
    """RunRoleSetBean（与 17 §5.4 同形）；isStartFace/isEndFace=0 ⇒ 免人脸打卡。

    含参考 `tsnRunServer` 提交时要用到的限值字段（limitSpeed/limitStride/…）。
    """
    return {
        "context": "", "identify": identify, "runType": 1, "pattern": 1,
        "totalRange": range_km, "surplusNum": 0, "isClock": 0,
        "isStartFace": 0, "isEndFace": 0, "isQuestion": 0,
        "faceTimeout": 0, "faceDistanceOut": 0.0,
        "startQualifiedTime": 0, "endQualifiedTime": -1, "step": 1500,
        "isStepRecursion": 0, "userOpenDevelop": False, "remarkId": None,
        "limitSpeed": "3.0", "limitStride": "1.5", "limitStepFrequency": "200",
        "middleFaces": [],
        "geofence": [[{"lat": "23.05", "lng": "113.42"}]],
        "list": [],
    }
