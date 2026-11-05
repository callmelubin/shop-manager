# -*- coding: utf-8 -*-
"""微信「下单助手」小程序接口客户端（auth_type=3，店主 SID）。"""
import json
import time
import urllib.parse

from . import net

RATE_LIMIT_CODES = {268530694, 268435472, 268435473}
SID_INVALID_CODES = {268697609, 268697610, 268697611}
RATE_LIMIT_HINT = "频率"


class DxzsError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


class SidExpired(DxzsError):
    pass


class DxzsClient:
    def __init__(self, sid, cfg, log=print, auth_type=None, v=None,
                 mch_id="", shop_id=""):
        self.sid = (sid or "").strip()
        self.base = cfg["base"].rstrip("/")
        self.v = str(v or cfg.get("v") or "1.33.9")
        self.auth_type = int(auth_type if auth_type is not None
                             else cfg.get("auth_type", 3))
        self.mch_id = str(mch_id or "")
        self.shop_id = str(shop_id or "")
        self.log = log
        self.op = net.make_opener()

    def _common(self):
        p = {"auth_type": self.auth_type, "sid": self.sid, "v": self.v}
        if self.mch_id:
            p["mch_id"] = self.mch_id
        if self.shop_id:
            p["shop_id"] = self.shop_id
        return p

    def call(self, path, body=None, timeout=30):
        q = urllib.parse.urlencode(self._common())
        payload = {"v": self.v, "sid": self.sid}
        if self.mch_id:
            payload["mch_id"] = self.mch_id
        if self.shop_id:
            payload["shop_id"] = self.shop_id
        payload.update(body or {})
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        st, _, raw = net.request(
            self.op, self.base + path + "?" + q, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": net.UA_WECHAT},
            data=data, timeout=timeout)
        if st != 200:
            raise DxzsError(st, f"HTTP {st}")
        j = net.load_json(raw, f"下单助手 {path}")
        self._check(j)
        return j

    def call_get(self, path, params=None, timeout=30):
        q = urllib.parse.urlencode({**self._common(), **(params or {})})
        st, _, raw = net.request(
            self.op, self.base + path + "?" + q, method="GET",
            headers={"Accept": "application/json", "User-Agent": net.UA_WECHAT},
            timeout=timeout)
        if st != 200:
            raise DxzsError(st, f"HTTP {st}")
        j = net.load_json(raw, f"下单助手 {path}")
        self._check(j)
        return j

    def _check(self, j):
        code = int(j.get("errcode") or 0)
        if code in SID_INVALID_CODES:
            raise SidExpired(code, "SID 已失效，请重新抓取")
        if code in RATE_LIMIT_CODES or RATE_LIMIT_HINT in str(j.get("msg") or ""):
            raise DxzsError(code, "触发频率限制")
        if code != 0:
            raise DxzsError(code, str(j.get("msg") or j.get("errmsg") or "接口返回错误"))
        return j

    def validate(self):
        paths = ["/shelfcomm/category/getall"]
        if self.auth_type == 4:
            paths += ["/shelfcomm/shelf/detail", "/shelfmgr/sjt/grayinfo"]
        last = ""
        for path in paths:
            try:
                j = self.call(path, timeout=15)
                if path.endswith("getall"):
                    cats = (j.get("data") or {}).get("category_list") or []
                    if cats:
                        return True, f"有效，{len(cats)} 个分类"
                    last = "分类为空"
                    continue
                return True, f"有效（{path}）"
            except SidExpired as e:
                return False, e.msg
            except DxzsError as e:
                last = str(e)
                continue
            except Exception as e:  # noqa: BLE001
                last = str(e)
                continue
        return False, last or "接口都不认这个 SID"

    def categories(self):
        j = self.call("/shelfcomm/category/getall")
        return (j.get("data") or {}).get("category_list") or []

    def goods_of(self, category_id, on_page=None):
        out, page = [], 1
        while True:
            j = self.call("/shelfcomm/category/getgoods",
                          {"category_id": category_id, "page_num": page, "page_size": 50})
            data = j.get("data") or {}
            out.extend(data.get("goods_list") or [])
            if on_page:
                try:
                    on_page(page, len(out))
                except Exception:  # noqa: BLE001
                    pass
            if data.get("is_last_page", True):
                break
            page = data.get("next_page_num") or (page + 1)
            time.sleep(0.2)
        return out

    def fetch_all(self):
        cats = self.categories()
        self.log(f"下单助手：{len(cats)} 个分类")
        goods, seen = [], set()
        per_cat = []
        for c in cats:
            name = c.get("category_name") or str(c.get("category_id"))
            gs = self.goods_of(c["category_id"])
            per_cat.append({"category_id": c["category_id"],
                            "category_name": name,
                            "category_type": c.get("category_type"),
                            "count": len(gs)})
            for g in gs:
                g["_category_name"] = name
                if g.get("spu_id") in seen:
                    continue
                seen.add(g.get("spu_id"))
                goods.append(g)
            self.log(f"下单助手：{name} {len(gs)} 个")
            time.sleep(0.2)
        return cats, per_cat, goods

    def edit(self, goods):
        return self.call("/shelfcomm/goods/edit", {"goods": goods})

    def add(self, goods):
        return self.call("/shelfcomm/goods/add", {"goods": goods})

    def sort_goods(self, category_id, spu_ids):
        return self.call("/shelfcomm/category/sortgoods",
                         {"category_id": category_id, "spu_id_list": list(spu_ids)})

    def upload_image(self, data, filename="goods.jpg"):
        fields = {"v": self.v, "sid": self.sid}
        if self.mch_id:
            fields["mch_id"] = self.mch_id
        if self.shop_id:
            fields["shop_id"] = self.shop_id
        body, ctype = net.multipart(fields,
                                    {"image": (filename, data, "image/jpeg")})
        q = urllib.parse.urlencode(self._common())
        st, _, raw = net.request(
            self.op, self.base + "/shelfcomm/basic/uploadimage?" + q, method="POST",
            headers={"Content-Type": ctype, "User-Agent": net.UA_WECHAT},
            data=body, timeout=60)
        if st != 200:
            raise DxzsError(st, f"上传图片 HTTP {st}")
        j = net.load_json(raw, "上传图片")
        self._check(j)
        url = ((j.get("data") or {}).get("image_url") or "").strip()
        if not url:
            raise DxzsError(0, "上传成功但没拿到图片地址")
        return url
