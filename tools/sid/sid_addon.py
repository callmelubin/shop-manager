"""抓 payapp 请求里的 sid（店主 auth_type=3 / 经营码店员 auth_type=4）。"""
import json
import os
import re
import socket
import time
from urllib.parse import urlparse, parse_qs

_orig = socket.getaddrinfo


def _ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("SID_CAPTURE") or os.path.join(BASE, "sid_capture.jsonl")
DEBUG = os.environ.get("SID_DEBUG") or ""


def _body_json(flow):
    try:
        text = flow.request.get_text(strict=False) or ""
    except Exception:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _first(qs, body, key):
    if key in qs and qs[key]:
        return qs[key][0]
    v = body.get(key)
    return v if isinstance(v, (str, int)) else None


def _header_sid(flow):
    try:
        for k, v in flow.request.headers.items():
            if k.lower() in ("sid", "x-sid", "x-wx-sid") and re.match(
                    r"^[A-Za-z0-9_\-]{30,64}$", (v or "").strip()):
                return v.strip()
        cookie = flow.request.headers.get("cookie", "") or ""
        m = re.search(r"(?:^|;\s*)sid=([A-Za-z0-9_\-]{30,64})", cookie)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


class Addon:
    def request(self, flow):
        host = flow.request.pretty_host
        if DEBUG and ("weixin.qq.com" in host or host.endswith("qq.com")):
            try:
                with open(DEBUG, "a", encoding="utf-8") as f:
                    f.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), host,
                                            urlparse(flow.request.pretty_url).path))
            except Exception:
                pass
        url = flow.request.pretty_url
        path = urlparse(url).path
        if ("payapp.weixin.qq.com" not in host
                and not (host.endswith("weixin.qq.com")
                         and ("shelf" in path or "sjt" in path))):
            return
        qs = parse_qs(urlparse(url).query)
        body = _body_json(flow)
        sid = _first(qs, body, "sid")
        if not sid:
            try:
                text = flow.request.get_text(strict=False) or ""
            except Exception:
                text = ""
            m = re.search(r'"sid"\s*:\s*"([^"]{20,64})"', text)
            if m:
                sid = m.group(1)
        if not sid:
            sid = _header_sid(flow)
        if not sid:
            if DEBUG and ("shelf" in path or "sjt" in path):
                try:
                    raw = flow.request.get_text(strict=False) or ""
                except Exception:
                    raw = ""
                masked = re.sub(r"([A-Za-z0-9_\-]{16,})",
                                lambda m: m.group(1)[:6] + "…", raw[:400])
                try:
                    with open(DEBUG, "a", encoding="utf-8") as f:
                        f.write("CAND %s %s %s\n" % (path, body.keys() if body else "",
                                                     masked))
                except Exception:
                    pass
            return
        auth = _first(qs, body, "auth_type")
        if auth is None and (urlparse(url).path.startswith("/shelfmgr")
                             or _first(qs, body, "mch_id")
                             or _first(qs, body, "shop_id")):
            auth = 4
        info = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sid": sid,
            "host": host,
            "path": urlparse(url).path,
            "auth_type": auth,
            "mch_id": _first(qs, body, "mch_id"),
            "shop_id": _first(qs, body, "shop_id"),
            "v": _first(qs, body, "v"),
        }
        try:
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(info, ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            pass


addons = [Addon()]
