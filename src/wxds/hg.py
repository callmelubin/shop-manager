# -*- coding: utf-8 -*-
"""惠管家（portal.smartpos.top）只读客户端。"""
import json
import re
import time
import urllib.parse

from . import net

IMAGE_BASE = "https://assets.smartpos.top/image"


class HgError(Exception):
    pass


class HgClient:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.base = cfg["base"].rstrip("/")
        self.log = log
        self.jar = net.new_jar()
        self.op = net.make_opener(self.jar)
        self._logged = False

    # ---------- 登录 ----------
    def login(self, timeout=25):
        self.log("惠管家：打开登录页…")
        st, _, raw = net.request(self.op, self.base + "/login", timeout=timeout,
                                 headers={"User-Agent": net.UA_BROWSER})
        if st != 200:
            raise HgError(f"登录页返回 HTTP {st}")
        html = raw.decode("utf-8", "ignore")
        m = re.search(r'name="_csrf"\s+content="([^"]+)"', html)
        csrf = m.group(1) if m else ""
        if not csrf:
            raise HgError("登录页里找不到 _csrf，接口可能改版了")

        form = urllib.parse.urlencode({
            "username": self.cfg["username"],
            "password": self.cfg["password"],
            "tenant": "",
            "deviceId": "wxds-tool-1",
            "_login_type": "1",
        }).encode()
        self.log("惠管家：提交账号密码…")
        st, _, _ = net.request(
            self.op, self.base + "/login/form", method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-TOKEN": csrf,
                "Referer": self.base + "/login",
                "User-Agent": net.UA_BROWSER,
            },
            data=form, timeout=timeout)
        if st not in (200, 302):
            raise HgError(f"登录提交返回 HTTP {st}（账号或密码可能不对）")

        net.set_cookie(self.jar, "partition-site", self.cfg["partition"],
                       "portal.smartpos.top")
        net.request(self.op, self.base + "/chain", timeout=timeout,
                    headers={"User-Agent": net.UA_BROWSER})
        net.request(self.op, self.base + "/chain/erp/home", timeout=timeout,
                    headers={"User-Agent": net.UA_BROWSER})

        menu = self._api("/api/my/menu/main/userMenu", timeout=timeout)
        if not menu.get("isSuccess"):
            raise HgError("惠管家登录失败：账号/密码错误，或该账号无门店权限")
        self._logged = True
        self.log("惠管家：登录成功")
        return True

    def _headers(self):
        return {
            "Referer": self.base + "/chain/stock/singleProductInventorySummary",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "User-Agent": net.UA_BROWSER,
        }

    def _api(self, path, params=None, timeout=25):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        st, _, raw = net.request(self.op, url, headers=self._headers(), timeout=timeout)
        if st != 200:
            raise HgError(f"惠管家接口 {path} 返回 HTTP {st}")
        return net.load_json(raw, "惠管家接口")

    def _post_api(self, path, body, timeout=60):
        st, _, raw = net.request(
            self.op, self.base + path, method="POST",
            headers={**self._headers(), "Content-Type": "application/json"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=timeout)
        if st != 200:
            raise HgError(f"惠管家接口 {path} 返回 HTTP {st}")
        j = net.load_json(raw, "惠管家接口")
        if not j.get("isSuccess"):
            raise HgError(f"惠管家接口失败：{j.get('msg') or j.get('code')}")
        return j

    # ---------- 商品档案（含图片） ----------
    def fetch_archive(self, page_size=500):
        if not self._logged:
            self.login()
        rows, page, total = [], 1, None
        while True:
            j = self._post_api(
                "/api/site/erp/chain/goods/v4/queryGoodsListByGoodsArchives",
                {"branchId": self.cfg["branch_id"],
                 "belongBranchId": self.cfg["branch_id"],
                 "page": page, "limit": page_size,
                 "searchType": "allSearch", "searchVal": ""})
            data = j.get("data") or {}
            lst = data.get("list") or []
            if total is None:
                total = int(data.get("total") or 0)
                self.log(f"惠管家：商品档案 {total} 条")
            rows.extend(lst)
            if not lst or len(rows) >= total:
                break
            page += 1
            time.sleep(0.3)
        return rows

    # ---------- 库存 ----------
    def fetch_stock(self, page_size=100, log_every=5):
        if not self._logged:
            self.login()
        rows = []
        page = 1
        total = None
        while True:
            params = {
                "powerType": "queryStoreIndexPager",
                "storeType": "goods",
                "page": page,
                "limit": page_size,
                "searchType": "allSearch",
                "searchVal": "",
                "branchId": self.cfg["branch_id"],
                "branchIds": self.cfg["branch_id"],
                "brandName": "", "brandId": "",
                "supplierName": "", "supplierId": "",
                "categoryName": "", "catIds": "",
                "goodsName": "", "goodsId": "", "allSearch": "",
            }
            j = self._api("/api/site/erp/chain/report/queryStoreIndexPager", params)
            if not j.get("isSuccess"):
                raise HgError(f"库存接口失败：{j.get('msg') or j.get('code')}")
            data = j.get("data") or []
            if total is None:
                total = int(j.get("recordsTotal") or 0)
                self.log(f"惠管家：共 {total} 条库存记录")
            rows.extend(data)
            if not data or page * page_size >= total:
                break
            page += 1
            if page % log_every == 0:
                self.log(f"惠管家：已拉 {len(rows)}/{total}")
            time.sleep(0.3)
        return rows


def photo_url(photo):
    p = (photo or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    return IMAGE_BASE + ("" if p.startswith("/") else "/") + p


def download_image(url, timeout=30):
    """下载惠管家图片，返回 bytes；失败抛 HgError。"""
    st, _, data = net.request(
        net.make_opener(), url, timeout=timeout,
        headers={"User-Agent": net.UA_BROWSER, "Referer": "https://portal.smartpos.top/"})
    if st != 200:
        raise HgError(f"图片下载失败 HTTP {st}")
    if not data:
        raise HgError("图片为空")
    return data
