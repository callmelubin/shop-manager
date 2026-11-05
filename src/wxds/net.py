# -*- coding: utf-8 -*-
"""最小 HTTP 工具：直连（不走系统代理），统一超时与错误信息。"""
import http.client
import http.cookiejar
import io
import json
import urllib.error
import urllib.request
import uuid

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA_WECHAT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
             "MicroMessenger/8.0.49 WeChat/arm64")


class NetworkError(Exception):
    pass


def make_opener(jar=None, with_proxy=False):
    handlers = []
    if not with_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def new_jar():
    return http.cookiejar.CookieJar()


def set_cookie(jar, name, value, domain, path="/"):
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=False,
        path=path, path_specified=True, secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False))


def request(opener, url, method="GET", headers=None, data=None, timeout=30):
    """返回 (status, headers, body_bytes)。4xx/5xx 不抛异常，交给调用方判断。"""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            try:
                body = r.read()
            except http.client.IncompleteRead as e:
                body = e.partial
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:
        raise NetworkError(f"网络请求失败: {e.reason}") from e
    except OSError as e:
        raise NetworkError(f"网络请求失败: {e}") from e


def load_json(raw, what="响应"):
    text = raw.decode("utf-8-sig", "replace")
    try:
        return json.loads(text)
    except ValueError:
        snippet = text.strip().replace("\n", " ")[:200]
        raise NetworkError(f"{what}不是 JSON：{snippet}")


def multipart(fields, files):
    """构造 multipart/form-data。files: {name: (filename, bytes, content_type)}"""
    boundary = "----WXDS" + uuid.uuid4().hex
    buf = io.BytesIO()
    for key, value in (fields or {}).items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        buf.write(str(value).encode("utf-8"))
        buf.write(b"\r\n")
    for key, (filename, data, ctype) in (files or {}).items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{key}"; '
                  f'filename="{filename}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"
