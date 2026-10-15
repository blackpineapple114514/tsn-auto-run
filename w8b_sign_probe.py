# -*- coding: utf-8 -*-
"""w8b_sign_probe.py - 离线复算 m.boxkj.com 云侧签名（只读，不发任何请求）

算法来源（字节级确证，classes_5.dex）：
  Lcom/bxkj/base/intercept/a;->c(Map,String token)  code@0x9dc158 insns=104
    token 为空/null -> 走 MD5 分支
      keys = sorted(map.keySet())
      sb   = "k1=v1&k2=v2&...&kn=vn"   (非末项加 '&'，值不做 URL 编码: a->a 第3参=0)
      sign = URLEncoder.encode( MD5Utils.md5(sb), "UTF-8" )   # md5 = Integer.toHexString -> 小写
    token 非空     -> 走 native libtsnsecret.so (Transform.w)，本脚本不覆盖
  Lcom/bxkj/base/intercept/a;->a(String k,String v,boolean enc)  code@0x9dc0b4
      return k + "=" + (enc ? URLEncoder.encode(v,"UTF-8") : v)

云侧分支（intercept pc 53-56）：url 以 'https://m.boxkj.com/' 开头时
      appId := "move" ; token := null ; appSecret := "e8167ef026cbc5e456ab837d9d6d9254"
      参与签名的 map = 业务字段 + appId + timestamp + appSecret   （pc 235/238/241）
      wire body 只追加 timestamp + sign                            （pc 248/251）

用法：
  python tools/w8b_sign_probe.py --demo
  python tools/w8b_sign_probe.py --kv "sportType=1" "startTime=1695000000000"
  python tools/w8b_sign_probe.py --capture 30-logs/w6_mock_capture.jsonl   # 校验抓包
"""
import sys, json, hashlib, argparse
from urllib.parse import quote, unquote, parse_qsl

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

CLOUD_HOST = "https://m.boxkj.com/"
APP_ID = "move"
APP_SECRET = "e8167ef026cbc5e456ab837d9d6d9254"


def md5_lower(s: str) -> str:
    """Lcom/bxkj/base/intercept/a;->c + MD5Utils.md5：
    字节按 UTF-8；digest 逐字节 Integer.toHexString -> 小写 2 位十六进制"""
    out = []
    for b in hashlib.md5(s.encode('utf-8')).digest():
        out.append('%02x' % b)   # toHexString 小写 + 补零
    return ''.join(out)


def sign_cloud(map_: dict) -> str:
    """token==null 分支：sign = urlencode(md5(sorted 'k=v&' join))"""
    keys = sorted(map_.keys())                       # Collections.sort(List)
    parts = []
    for k in keys:
        parts.append("%s=%s" % (k, map_[k]))         # a->a(k, v, enc=False)
    sb = "&".join(parts)
    return quote(md5_lower(sb), safe='')             # URLEncoder.encode(md5hex,"UTF-8")


def build_cloud_map(business: dict, timestamp: str) -> dict:
    m = dict(business)
    m["appId"] = APP_ID
    m["timestamp"] = timestamp
    m["appSecret"] = APP_SECRET
    return m


def cmd_demo():
    ts = "1695000000000"
    biz = {"sportType": "1", "startTime": ts}
    m = build_cloud_map(biz, ts)
    keys = sorted(m.keys())
    print("签名串（排序后）:")
    print("  " + "&".join("%s=%s" % (k, m[k]) for k in keys))
    print("md5       =", md5_lower("&".join("%s=%s" % (k, m[k]) for k in keys)))
    print("sign(云侧) =", sign_cloud(m))
    print()
    print("无 body 的 POST（如 login 走 query，body 只有 timestamp+sign）:")
    m2 = build_cloud_map({}, "1695000000001")
    print("  sign =", sign_cloud(m2))
    print()
    print("带 2 个业务字段:")
    m3 = build_cloud_map({"account": "u", "password": "p"}, "1695000000002")
    print("  sign =", sign_cloud(m3))


def cmd_kv(pairs):
    ts = None
    biz = {}
    for p in pairs:
        if '=' not in p:
            print("跳过非法 kv:", p); continue
        k, v = p.split('=', 1)
        if k == 'timestamp':
            ts = v
        else:
            biz[k] = v
    if ts is None:
        import time
        ts = str(int(time.time() * 1000))
    m = build_cloud_map(biz, ts)
    print("map =", json.dumps(m, ensure_ascii=False, sort_keys=True))
    print("sign =", sign_cloud(m))


def cmd_capture(path):
    """校验 w6_mock_capture.jsonl：每行找 body 里的 timestamp，重算 sign 比对。"""
    ok = bad = skip = 0
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                skip += 1; continue
            body = rec.get('body') or rec.get('requestBody') or ''
            if not isinstance(body, str) or 'sign=' not in body:
                skip += 1; continue
            kv = dict(parse_qsl(body, keep_blank_values=True))
            got = kv.pop('sign', None)
            ts = kv.pop('timestamp', None)
            if got is None or ts is None:
                skip += 1; continue
            m = build_cloud_map(kv, ts)
            exp = sign_cloud(m)
            if exp == got or exp == unquote(got):
                ok += 1
                print("L%d MATCH  %s" % (ln, exp))
            else:
                bad += 1
                print("L%d DIFF   got=%s exp=%s  keys=%s" %
                      (ln, got, exp, sorted(m.keys())))
    print("\n== ok=%d diff=%d skip=%d ==" % (ok, bad, skip))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--kv', nargs='*', default=None)
    ap.add_argument('--capture', default=None)
    a = ap.parse_args()
    if a.capture:
        cmd_capture(a.capture)
    elif a.kv is not None:
        cmd_kv(a.kv)
    else:
        cmd_demo()


if __name__ == '__main__':
    main()
