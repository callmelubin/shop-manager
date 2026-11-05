# -*- coding: utf-8 -*-
"""微信下单助手小程序管理（原生桌面版）。

两个页签：
  库存同步 —— 惠管家库存 → 下单助手，差异预览后写回
  上架商品 —— 惠管家商品档案 + 图片 → 下单助手新品
"""
import copy
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
from decimal import Decimal

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from wxds import __version__, capture, config, engine, listing
from wxds.dxzs import RATE_LIMIT_CODES, DxzsClient, DxzsError, SidExpired
from wxds.hg import HgClient, HgError, download_image, photo_url

APP_TITLE = "微信下单助手小程序管理"
GREEN = "#07C160"
GREEN_DARK = "#06AD56"
BG = "#f2f3f5"
CARD = "#ffffff"
TEXT = "#1f2329"
SUB = "#8a9099"
RED = "#e5484d"
AMBER = "#d97706"
BLUE = "#1677ff"
FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")
FONT_T = ("Microsoft YaHei UI", 15, "bold")


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def state_cn(s):
    return engine.STATE_CN.get(s, s or "-")


def center_window(win, parent):
    def apply():
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        if w <= 1 or h <= 1:
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    win.update_idletasks()
    fw, fh = win.winfo_reqwidth(), win.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - fw) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - fh) // 2
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.bind("<Map>", lambda _e: win.after(10, apply))
    win.after(60, apply)


class Backend:
    """网络与业务都在后台线程里跑，结果通过队列回主线程。"""

    def __init__(self, app):
        self.app = app
        self.cfg = config.load()
        self.stores = config.load_stores(self.cfg)
        idx = config.read_last_store()
        self.store_idx = 0
        self._dedupe_stores()
        if self.stores:
            self.store_idx = min(max(idx, 0), len(self.stores) - 1)
        self.sid = (self.stores[self.store_idx].get("sid") or "").strip() if self.stores else ""
        self.busy = False
        self.capture_found = False
        self._probing = False
        self.stock_rows = []
        self.archive = []
        self.mini_goods = []
        self.categories = []
        self.plan = None
        self.candidates = []
        self.image_cache = {}
        self.sort_cats = []
        self.sort_order = {}
        self.sort_items = {}
        self.capturing = False
        self.capture_stop = False
        self.capture_obj = None

    # ---------- 店铺 ----------
    def _dedupe_stores(self):
        """同一个 shop_id 的店员店只保留一条，缺 mch_id 的用另一条补上。"""
        seen, out, changed = {}, [], False
        for s in self.stores:
            if s.get("auth_type") == 4 and str(s.get("shop_id") or ""):
                key = str(s["shop_id"])
                if key in seen:
                    keep = seen[key]
                    if not keep.get("mch_id") and s.get("mch_id"):
                        keep["mch_id"] = s["mch_id"]
                    if s.get("sid"):
                        keep["sid"] = s["sid"]
                    if s.get("v"):
                        keep["v"] = s["v"]
                    changed = True
                    continue
                seen[key] = s
            out.append(s)
        if changed:
            self.stores = out
            try:
                config.save_stores(out)
            except OSError:
                pass

    def set_sid(self, sid):
        sid = (sid or "").strip()
        if not sid:
            return
        self.sid = sid
        try:
            config.write_sid(sid, self.cfg)
        except OSError:
            pass
        if self.stores:
            self.stores[self.store_idx]["sid"] = sid
            try:
                config.save_stores(self.stores)
            except OSError:
                pass

    def use_store(self, idx):
        if idx < 0 or idx >= len(self.stores):
            return
        self.store_idx = idx
        st = self.stores[idx]
        try:
            config.save_stores(self.stores, idx)
        except OSError:
            pass
        self.sid = (st.get("sid") or "").strip()
        if self.sid:
            try:
                config.write_sid(self.sid, self.cfg)
            except OSError:
                pass
        self.plan = None
        self.candidates = []
        self.sort_cats = []
        self.sort_order = {}
        self.sort_items = {}
        self.post("store_changed", st)

    # ---------- 事件 ----------
    def post(self, kind, data=None):
        self.app.events.put((kind, data))

    def log(self, msg):
        self.post("log", msg)

    def error(self, msg):
        self.post("error", msg)

    def run(self, fn, *args, **kwargs):
        if self.busy:
            self.log("有任务在执行，请稍等")
            return False
        self.busy = True
        self.post("busy", True)

        def wrapper():
            try:
                fn(*args, **kwargs)
            except (HgError, DxzsError) as e:
                self.log(f"✗ {e}")
                self.error(str(e))
            except Exception as e:  # noqa: BLE001
                self.log(f"✗ 未预期错误：{e}")
                self.log(traceback.format_exc(limit=3))
                self.error(str(e))
            finally:
                self.busy = False
                self.post("busy", False)

        threading.Thread(target=wrapper, daemon=True).start()
        return True

    # ---------- 目录 ----------
    def client(self, sid=None, log=None):
        st = self.stores[self.store_idx] if self.stores else {}
        return DxzsClient(sid or self.sid, self.cfg["dxzs"],
                          log=log or self.log,
                          auth_type=int(st.get("auth_type") or 3),
                          v=st.get("v") or None,
                          mch_id=str(st.get("mch_id") or ""),
                          shop_id=str(st.get("shop_id") or ""))

    def data_path(self, *parts):
        p = os.path.join(config.data_dir(), *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def save_json(self, name, obj):
        p = self.data_path("snap", name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        return p

    # ---------- SID ----------
    def probe_sid(self):
        """后台探测当前店铺 SID 是否有效，结果用 sid_probe 事件回传。"""
        if self._probing:
            return
        sid = (self.sid or "").strip()
        if not sid:
            self.post("sid_probe", {"sid": "", "ok": False, "kind": "none"})
            return
        self._probing = True

        def job():
            try:
                kind, _m, _s = self._detect_sid(sid)
                self.post("sid_probe", {"sid": sid,
                                        "ok": kind in ("owner", "clerk"),
                                        "kind": kind})
            except Exception as e:  # noqa: BLE001
                self.post("sid_probe", {"sid": sid, "ok": False,
                                        "kind": "error", "msg": str(e)})
            finally:
                self._probing = False

        threading.Thread(target=job, daemon=True).start()

    def check_sid(self, sid):
        sid = (sid or "").strip()
        if not sid:
            self.error("请先粘贴 SID")
            return
        self.log("正在识别 SID 类型…")

        def job():
            kind, mch, shop = self._detect_sid(sid)
            if kind == "clerk" and mch and shop:
                self.log(f"✓ 识别为店员 SID（mch={mch}，shop={shop}）")
                self.post("clerk_captured", {"sid": sid, "auth_type": 4,
                                             "v": "0.11.35", "mch_id": mch,
                                             "shop_id": shop})
                return
            if kind == "owner":
                self.set_sid(sid)
                self.log(f"✓ SID 有效（尾号 {sid[-6:]}），已保存")
                self.post("sid", (True, "有效"))
                return
            self.log("✗ SID 无效或已过期")
            self.post("sid", (False, "SID 无效或已过期，请重新抓取"))

        self.run(job)

    # ---------- 抓取 SID ----------
    def capture_ready(self):
        return capture.SidCapture(config.app_dir(), config.data_dir()).available()

    def start_capture(self):
        if self.capturing:
            return
        self.capture_found = False
        cap = capture.SidCapture(config.app_dir(), config.data_dir(), self.log)
        if not cap.available():
            self.error("抓取组件缺失：tools\\sid 目录不完整")
            return
        if not capture.cert_installed():
            if not messagebox.askyesno(
                    "需要安装证书",
                    "抓取 HTTPS 需要把 mitmproxy 根证书装进当前用户证书库。\n"
                    "是否现在安装？"):
                return
            try:
                capture.install_cert(cap.pem)
                self.log("✓ 证书已安装")
            except capture.CaptureError as e:
                self.error(str(e))
                return
        self.capturing = True
        self.capture_stop = False
        self.capture_obj = cap
        self.post("capture", "starting")
        threading.Thread(target=self._capture_job, args=(cap,), daemon=True).start()

    def stop_capture(self):
        self.capture_stop = True
        self.log("正在停止抓取…")

    def start_watch(self, seconds=150):
        if self.capturing:
            self.error("正在抓取中，先停止再记录")
            return False
        cap = capture.SidCapture(config.app_dir(), config.data_dir(), log=self.log)
        if not cap.available():
            self.error("抓取组件缺失")
            return False
        self.capturing = True
        self.capture_stop = False
        self.capture_obj = cap
        threading.Thread(target=self._watch_job, args=(cap, seconds),
                         daemon=True).start()
        return True

    def _watch_job(self, cap, seconds):
        try:
            cap.start()
            self.post("capture", "watching")
            for i in range(seconds):
                if self.capture_stop:
                    break
                time.sleep(1)
                if i % 5 == 0:
                    self.post("capture_seen", cap.seen())
        except Exception as e:  # noqa: BLE001
            self.post("capture_fail", f"记录失败：{e}")
        finally:
            try:
                cap.stop()
            except Exception:  # noqa: BLE001
                pass
            tail = cap.tail(500)
            path = ""
            try:
                path = self.data_path("capture", "watch_last.log")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(tail))
            except OSError:
                pass
            self.capturing = False
            self.capture_obj = None
            self.post("watch_done", {"path": path, "seen": cap.seen(),
                                     "tail": tail[-30:]})
            self.post("capture_done", None)

    def _capture_job(self, cap):
        deadline = time.time() + 180
        last_seen = 0
        try:
            try:
                cap.start()
                self.post("capture", "waiting")
                while time.time() < deadline and not self.capture_stop:
                    time.sleep(1)
                    for info in cap.poll():
                        if self._accept_sid(info):
                            return
                    n = cap.seen()
                    if n != last_seen:
                        last_seen = n
                        self.post("capture_seen", n)
                if not self.capture_stop:
                    tail = cap.tail(20)
                    if tail:
                        self.log("最近抓到的微信请求：")
                        for ln in tail:
                            self.log("  " + ln)
                    if last_seen == 0:
                        msg = ("180 秒内没有看到任何微信请求。\n"
                               "说明代理没生效，或不是在这台电脑的微信里打开。\n"
                               "请确认：电脑微信 → 设置 → 通用 → 代理 使用系统代理。")
                    else:
                        msg = (f"180 秒内看到 {last_seen} 条微信请求，但没找到可用 SID。\n"
                               "已把最近的请求写进日志，请点「日志」查看后发我。")
                    self.post("capture_fail", msg)
            except capture.CaptureError as e:
                self.log(f"内嵌抓包启动失败（{e}），打开抓取工具窗口…")
                self.post("capture", "fallback")
                sid = self._capture_via_gui(cap, deadline)
                if sid:
                    return
                if not self.capture_stop:
                    self.post("capture_fail", "抓取工具没有抓到 SID（超时或未开始抓取）。")
        except Exception as e:  # noqa: BLE001
            self.post("capture_fail", f"抓取异常：{e}")
        finally:
            cap.stop()
            self.capturing = False
            self.capture_obj = None
            self.post("capture_done", None)

    def _detect_sid(self, sid, mch="", shop="", v=None):
        """自动判断 SID 是店主还是店员，返回 (kind, mch, shop)。"""
        cfg = self.cfg["dxzs"]
        if mch and shop:
            try:
                c = DxzsClient(sid, cfg, auth_type=4, v=v,
                               mch_id=mch, shop_id=shop)
                c.call("/shelfcomm/category/getall", timeout=15)
                return "clerk", mch, shop
            except Exception:  # noqa: BLE001
                pass
        try:
            c = DxzsClient(sid, cfg, auth_type=4, v=v)
            j = c.call_get("/sjtassistant/assistant/getshoplist", timeout=15)
            shops = (j.get("data") or {}).get("shop_list") or []
            if shops:
                s0 = shops[0]
                return ("clerk", str(s0.get("mch_id") or ""),
                        str(s0.get("shop_id") or ""))
        except Exception:  # noqa: BLE001
            pass
        try:
            c = DxzsClient(sid, cfg, auth_type=3, v=v)
            c.call("/shelfcomm/category/getall", timeout=15)
            return "owner", "", ""
        except Exception:  # noqa: BLE001
            pass
        return "invalid", "", ""

    def _accept_sid(self, info):
        if isinstance(info, str):
            info = {"sid": info, "auth_type": 3}
        sid = (info.get("sid") or "").strip()
        if not sid:
            return False
        mch = str(info.get("mch_id") or "")
        shop = str(info.get("shop_id") or "")
        v = info.get("v") or None
        kind, m2, s2 = self._detect_sid(sid, mch, shop, v)
        if kind == "clerk" and m2 and s2:
            self.capture_found = True
            self.log(f"✓ 抓到店员 SID（mch={m2}，shop={s2}）")
            self.post("clerk_captured", {"sid": sid, "auth_type": 4, "v": v,
                                         "mch_id": m2, "shop_id": s2})
            return True
        if kind == "owner":
            self.capture_found = True
            self.set_sid(sid)
            self.post("captured", sid)
            return True
        self.log("抓到的 SID 暂时无效（可能已过期），继续等待…")
        return False

    def _capture_via_gui(self, cap, deadline):
        """兜底：打开原抓取工具窗口，等它把 current_sid.txt 写出来。"""
        sid_file = os.path.join(os.path.dirname(cap.exe), "current_sid.txt")
        before = os.path.getmtime(sid_file) if os.path.exists(sid_file) else None
        try:
            os.startfile(cap.exe)  # noqa: S606
        except OSError as e:
            self.post("capture_fail", f"打不开抓取工具：{e}")
            return None
        while time.time() < deadline and not self.capture_stop:
            time.sleep(1)
            if not os.path.exists(sid_file):
                continue
            mtime = os.path.getmtime(sid_file)
            if before is not None and mtime == before:
                continue
            before = mtime
            with open(sid_file, encoding="ascii", errors="ignore") as f:
                sid = f.read().strip()
            if sid and self._accept_sid(sid):
                return sid
        return None

    # ---------- 库存同步 ----------
    def pull_stock(self):
        if not self.sid:
            self.error("没有 SID，请先粘贴并检测")
            return
        self.log("开始拉取：惠管家库存 + 下单助手商品…")

        def job():
            ok, msg = self.client(log=self.log).validate()
            if not ok:
                raise DxzsError(0, f"SID 无效或已过期（{msg}）")
            self.post("progress", (0, 0, "登录惠管家"))
            hg = HgClient(self.cfg["hg"], log=self.log)
            hg.login()
            self.stock_rows = hg.fetch_stock()
            self.post("progress", (0, 0, "拉取小程序商品"))
            dx = self.client(log=self.log)
            cats, per_cat, goods = dx.fetch_all()
            self.categories = cats
            self.mini_goods = goods
            self.save_json(f"hg_{time.strftime('%Y%m%d_%H%M%S')}.json",
                           {"ts": now_str(), "rows": self.stock_rows})
            self.save_json(f"dnzs_{time.strftime('%Y%m%d_%H%M%S')}.json",
                           {"ts": now_str(), "goods": goods})
            self.post("progress", (0, 0, "比对差异"))
            plan = engine.build_plan(goods, self.stock_rows, self.cfg["policies"])
            self.plan = plan
            self.log(f"✓ 比对完成：匹配 {plan['summary']['matched']}，"
                     f"待改 {plan['summary']['changed']}")
            self.post("stock_plan", plan)

        self.run(job)

    def apply_stock(self, spus):
        if not self.plan:
            self.error("还没有比对结果")
            return
        items = [i for i in self.plan["items"] if i["changed"] and int(i["spu"]) in set(spus)]
        if not items:
            self.error("没有选中的待同步商品")
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup = self.data_path("backup", f"before_apply_{ts}.json")
        with open(backup, "w", encoding="utf-8") as f:
            json.dump({"ts": now_str(), "items": [
                {"spu": i["spu"], "name": i["name"], "goods": i["goods"]} for i in items]},
                f, ensure_ascii=False, indent=1)
        self.log(f"✓ 已备份 {len(items)} 个商品：{os.path.basename(backup)}")
        self.post("backup", backup)
        cfg = self.cfg
        delay = max(0, int(cfg["rate"]["write_delay_ms"])) / 1000.0
        retry = max(0, int(cfg["rate"]["retry"]))
        retry_delay = max(0, int(cfg["rate"]["retry_delay_ms"])) / 1000.0
        mode = cfg.get("payload_mode") or "full"
        total = len(items)

        def job():
            client = self.client(log=self.log)
            ok_n, results, stopped = 0, [], None
            use_mode = mode
            for idx, item in enumerate(items, 1):
                if self.app.stop_flag:
                    stopped = "已停止"
                    break
                ok, msg = self._edit(client, item, use_mode, delay, retry, retry_delay)
                if not ok and use_mode != "minimal":
                    self.log(f"… {item['name']} 完整载荷失败，改精简载荷重试")
                    ok2, msg2 = self._edit(client, item, "minimal", delay, retry, retry_delay)
                    if ok2:
                        ok, msg, use_mode = True, "", "minimal"
                    else:
                        msg = f"{msg} / 精简:{msg2}"
                results.append((item["name"], ok, msg))
                if ok:
                    ok_n += 1
                    if item.get("target_price") is not None:
                        item["goods"]["price"] = item["target_price"]
                    st = item.get("target_state") or item["cur_state"]
                    item["cur_stock"] = (item["target_stock"]
                                         if item.get("target_state") else item["cur_stock"])
                    item["cur_state"] = st
                    item["changed"] = False
                    item["action"] = "noop"
                    item["status"] = "已同步"
                else:
                    self.log(f"✗ {item['name']}：{msg}")
                self.post("progress", (idx, total, "写回下单助手"))
            self.log(f"✓ 同步结束：成功 {ok_n}，失败 {total - ok_n}"
                     + (f"（{stopped}）" if stopped else ""))
            self.post("apply_done", {"ok": ok_n, "fail": total - ok_n,
                                     "backup": backup, "stopped": stopped})

        self.app.stop_flag = False
        self.run(job)

    def _edit(self, client, item, mode, delay, retry, retry_delay):
        tstate = item.get("target_state") or item["cur_state"]
        tstock = item["target_stock"] if item.get("target_state") else item["cur_stock"]
        payload = engine.build_payload(item["goods"], tstock, tstate, mode,
                                       price=item.get("target_price"))
        attempt = 0
        try:
            while True:
                try:
                    client.edit(payload)
                    return True, ""
                except SidExpired:
                    raise
                except DxzsError as e:
                    if e.code in RATE_LIMIT_CODES and attempt < retry:
                        attempt += 1
                        time.sleep(retry_delay)
                        continue
                    return False, str(e)
        finally:
            time.sleep(delay)

    # ---------- 上架 ----------
    def load_candidates(self, include_zero=True):
        if not self.sid:
            self.error("没有 SID，请先粘贴并检测")
            return
        self.log("拉取惠管家商品档案（含图片）+ 库存…")

        def job():
            hg = HgClient(self.cfg["hg"], log=self.log)
            hg.login()
            self.stock_rows = hg.fetch_stock()
            self.archive = hg.fetch_archive()
            dx = self.client(log=self.log)
            cats = dx.categories()
            self.categories = cats
            _, per_cat, goods = dx.fetch_all()
            self.mini_goods = goods
            self.post("progress", (0, 0, "计算候选"))
            cands = listing.build_candidates(self.archive, self.stock_rows, goods,
                                             self.cfg["policies"], include_zero)
            for c in cands:
                c["category_id"] = 0
                c["price_fen"] = listing.to_fen(c["price_yuan"])
                c["name_edit"] = c["name"]
                c["image_url"] = ""
            self.candidates = cands
            self.save_json("archive.json", {"ts": now_str(), "rows": self.archive})
            in_stock = sum(1 for c in cands if c["qty"] > 0)
            self.log(f"✓ 惠管家有、小程序没有的条码 {len(cands)} 个"
                     f"（其中有正库存 {in_stock} 个）")
            self.post("candidates", cands)

        self.run(job)

    def add_products(self, selected):
        if not selected:
            self.error("没有选中要上架的商品")
            return
        total = len(selected)
        self.log(f"开始上架 {total} 个商品…")
        self.post("add_start", total)

        def job():
            client = self.client(log=self.log)
            delay = max(0, int(self.cfg["rate"]["write_delay_ms"])) / 1000.0
            ok_n, done, fails = 0, [], []
            for idx, c in enumerate(selected, 1):
                if self.app.stop_flag:
                    break
                try:
                    image_url = ""
                    if c.get("photo"):
                        self.post("progress", (idx - 1, total,
                                               f"下载图片 {idx}/{total}：{c['name']}"))
                        raw = download_image(photo_url(c["photo"]))
                        jpg = listing.prepare_image(raw)
                        image_url = client.upload_image(jpg, f'{c["barcode"]}.jpg')
                        self.log(f"  图片已上传：{c['name']}")
                    goods = listing.build_goods(
                        c, c["category_id"], image_url=image_url,
                        name=c.get("name_edit") or c["name"],
                        price_fen=c.get("price_fen"))
                    client.add(goods)
                    ok_n += 1
                    done.append(c["barcode"])
                    self.log(f"✓ 已上架：{c['name']}（{c['price_fen'] / 100:.2f} 元）")
                except SidExpired:
                    self.log("✗ SID 失效，停止上架")
                    fails.append((c["name"], "SID 失效"))
                    break
                except Exception as e:  # noqa: BLE001
                    self.log(f"✗ 上架失败 {c['name']}：{e}")
                    fails.append((c["name"], str(e)[:120]))
                self.post("progress", (idx, total, f"上架 {idx}/{total}：{c['name']}"))
                time.sleep(delay)
            self.log(f"✓ 上架结束：成功 {ok_n}/{total}")
            self.post("add_done", {"ok": ok_n, "fail": total - ok_n,
                                   "done": done, "fails": fails})

        self.app.stop_flag = False
        self.run(job)

    # ---------- 排序 / 标签 ----------
    def load_sort_data(self):
        if not self.sid:
            self.error("没有 SID，请先粘贴并检测")
            return
        self.log("拉取分类与商品（排序/标签）…")

        def job():
            dx = self.client(log=self.log)
            self.post("sort_progress", (0, 0, "正在连接小程序…"))
            cats = dx.categories()
            total = len(cats)
            self.post("sort_progress", (0, total, f"共 {total} 个分类，开始拉取…"))
            items, order = {}, {}
            for i, cat in enumerate(cats, 1):
                name = cat.get("category_name") or str(cat.get("category_id"))
                self.post("sort_progress",
                          (i - 1, total, f"拉取 {i}/{total}：{name}"))

                def cb(page, n, _i=i, _name=name):
                    self.post("sort_progress",
                              (_i - 1, total,
                               f"{_i}/{total} {_name} · 第{page}页 · {n}个"))

                gs = dx.goods_of(cat["category_id"], on_page=cb)
                for g in gs:
                    items[g["spu_id"]] = g
                order[cat["category_id"]] = [g["spu_id"] for g in gs]
                self.log(f"下单助手：{name} {len(gs)} 个")
                self.post("sort_progress",
                          (i, total, f"{i}/{total} {name} · 已拉取 {len(items)} 个"))
            self.sort_cats = cats
            self.sort_items = items
            self.sort_order = order
            self.categories = cats
            self.save_json("sort_%s.json" % time.strftime("%Y%m%d_%H%M%S"),
                           {"cats": cats, "order": order})
            self.log(f"✓ {len(cats)} 个分类，{len(items)} 个商品")
            self.post("sort_data", (cats, order, items))

        self.run(job)

    def save_sort(self, cid, ordered, tags):
        if not ordered and not tags:
            self.error("没有需要保存的改动")
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = self.data_path("backup", f"sort_{stamp}.json")
        with open(backup, "w", encoding="utf-8") as f:
            json.dump({"ts": now_str(), "category_id": cid, "order": ordered,
                       "tags": {str(k): v for k, v in tags.items()}}, f,
                      ensure_ascii=False, indent=1)
        self.log(f"✓ 已备份排序/标签到 {os.path.basename(backup)}")
        cfg = self.cfg
        delay = max(0, int(cfg["rate"]["write_delay_ms"])) / 1000.0
        total = len(tags) + (1 if ordered else 0)
        self.post("progress", (0, total, "保存排序/标签"))

        def job():
            dx = self.client(log=self.log)
            done, fails = 0, []
            for spu, tag in tags.items():
                g = self.sort_items.get(spu)
                if not g:
                    continue
                payload = copy.deepcopy(g)
                payload.pop("_category_name", None)
                payload["tag_type"] = tag
                attempt = 0
                while True:
                    try:
                        dx.edit(payload)
                        g["tag_type"] = tag
                        break
                    except SidExpired:
                        raise
                    except DxzsError as e:
                        if e.code in RATE_LIMIT_CODES and attempt < 3:
                            attempt += 1
                            time.sleep(2)
                            continue
                        fails.append((g.get("name"), str(e)))
                        break
                    except Exception as e:  # noqa: BLE001
                        fails.append((g.get("name"), str(e)))
                        break
                done += 1
                self.post("progress", (done, total, "保存标签"))
                time.sleep(delay)
            sort_ok, sort_msg = False, ""
            if ordered:
                try:
                    dx.sort_goods(cid, ordered)
                    sort_ok = True
                    self.sort_order[cid] = list(ordered)
                except SidExpired:
                    raise
                except Exception as e:  # noqa: BLE001
                    sort_msg = str(e)
                done += 1
                self.post("progress", (done, total, "保存排序"))
            self.post("sort_done", {"tags": len(tags), "tag_fail": fails,
                                    "sort_ok": sort_ok, "sort_msg": sort_msg,
                                    "backup": backup})

        self.run(job)

    # ---------- 分类 ----------
    def create_category(self, name):
        name = (name or "").strip()
        if not name:
            return
        self.log(f"正在新建分类：{name}")

        def job():
            dx = self.client(log=self.log)
            last = ""
            for path in ("/shelfcomm/category/add", "/shelfcomm/category/create",
                         "/shelfcomm/category/save"):
                try:
                    dx.call(path, {"category_name": name,
                                   "category_type": "EN_CATEGORY_TYPE_NORMAL"},
                            timeout=20)
                    cats = dx.categories()
                    self.sort_cats = cats
                    self.categories = cats
                    self.log(f"✓ 已新建分类：{name}")
                    self.post("category_added", {"name": name, "cats": cats})
                    return
                except SidExpired:
                    raise
                except Exception as e:  # noqa: BLE001
                    last = str(e)
                    continue
            self.error(f"新建分类失败：{last}")

        self.run(job)

    def set_category(self, spus, cid, cname):
        spus = [s for s in spus if s in self.sort_items]
        if not spus:
            self.error("没有选中商品")
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = self.data_path("backup", f"category_{stamp}.json")
        with open(backup, "w", encoding="utf-8") as f:
            json.dump({"ts": now_str(), "category_id": cid,
                       "spus": spus,
                       "goods": [self.sort_items[s] for s in spus]}, f,
                      ensure_ascii=False, indent=1)
        cfg = self.cfg
        delay = max(0, int(cfg["rate"]["write_delay_ms"])) / 1000.0
        self.post("progress", (0, len(spus), "批量设置分类"))

        def job():
            dx = self.client(log=self.log)
            ok, fails = 0, []
            for i, spu in enumerate(spus, 1):
                g = self.sort_items.get(spu)
                if not g:
                    continue
                payload = copy.deepcopy(g)
                payload.pop("_category_name", None)
                payload["category_id"] = cid
                attempt = 0
                while True:
                    try:
                        dx.edit(payload)
                        g["category_id"] = cid
                        ok += 1
                        break
                    except SidExpired:
                        raise
                    except DxzsError as e:
                        if e.code in RATE_LIMIT_CODES and attempt < 3:
                            attempt += 1
                            time.sleep(2)
                            continue
                        fails.append((g.get("name"), str(e)))
                        break
                    except Exception as e:  # noqa: BLE001
                        fails.append((g.get("name"), str(e)))
                        break
                self.post("progress", (i, len(spus), f"设置分类 {i}/{len(spus)}"))
                time.sleep(delay)
            self.post("category_set", {"ok": ok, "fails": fails, "cid": cid,
                                       "cname": cname, "spus": spus,
                                       "backup": backup})

        self.run(job)


class StockTab(ttk.Frame):
    COLS = [
        ("sel", "全选", 44, "center"),
        ("name", "商品", 270, "w"),
        ("cat", "分类", 108, "w"),
        ("bc", "条码", 160, "w"),
        ("cur", "当前库存", 86, "center"),
        ("curstate", "当前状态", 86, "center"),
        ("hg", "惠管家", 76, "center"),
        ("unit", "单位", 48, "center"),
        ("target", "同步后", 118, "center"),
        ("action", "动作", 78, "center"),
        ("note", "说明", 215, "w"),
    ]

    def __init__(self, master, app):
        super().__init__(master, style="Tab.TFrame")
        self.app = app
        self.backend = app.backend
        self.selected = set()
        self.items = {}
        self.sort_key = None
        self.sort_desc = False
        self.only_diff = tk.BooleanVar(value=False)
        self.edit_win = None
        self.edit_spu = None
        self.edit_img_ref = None
        self._build()

    def _build(self):
        bar = ttk.Frame(self, style="Tab.TFrame")
        bar.pack(fill="x", padx=14, pady=(12, 4))
        self.btn_pull = ttk.Button(bar, text="① 拉取并比对", style="Primary.TButton",
                                   command=self.backend.pull_stock)
        self.btn_pull.pack(side="left")
        ttk.Button(bar, text="全选", command=lambda: self.select_all(True)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="差异全选", command=lambda: self.select_all(True, True)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="清空", command=lambda: self.select_all(False)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="手动修改", command=self.edit_selected).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="批量修改", command=self.batch_edit).pack(
            side="left", padx=(8, 0))
        self.btn_apply = ttk.Button(bar, text="同步选中 (0)", style="Danger.TButton",
                                    command=self.apply, state="disabled")
        self.btn_apply.pack(side="right")

        info = ttk.Frame(self, style="Tab.TFrame")
        info.pack(fill="x", padx=16, pady=(0, 4))
        ttk.Button(info, text="备份目录", command=self.open_backup).pack(side="right")
        ttk.Checkbutton(info, text="只看差异", variable=self.only_diff,
                        command=self._resort).pack(side="right", padx=(0, 10))
        tk.Label(info, text="点表头可排序 · 方框勾选（一致的行也能勾选后批量改）· 双击一行手动修改 · 全选按表头",
                 bg=BG, fg=SUB, font=("Microsoft YaHei UI", 8), anchor="w").pack(side="left")
        self.summary = tk.Label(self, text="点「拉取并比对」开始", bg=BG, fg=SUB,
                                font=FONT, anchor="w")
        self.summary.pack(fill="x", padx=16, pady=(0, 8))

        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in self.COLS:
            self.tree.heading(key, text=title,
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("name", "note")))
        self.tree.tag_configure("sell", background="#ffe2e2", font=FONT_B)
        self.tree.tag_configure("restore", background="#dcf7e7", font=FONT_B)
        self.tree.tag_configure("update", background="#fff3c4", font=FONT_B)
        self.tree.tag_configure("skip", foreground="#9aa0a6")
        self.tree.tag_configure("manual", background="#ffe6cc", font=FONT_B)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double)

    # ---------- 交互 ----------
    def on_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        if row:
            self.toggle(int(row))
        return "break"

    def on_double(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.open_edit(int(row))
        return "break"

    def toggle(self, spu):
        item = self.items.get(spu)
        if not item:
            return
        if spu in self.selected:
            self.selected.discard(spu)
        else:
            self.selected.add(spu)
        self.refresh_row(str(spu))
        self.update_apply_button()

    def refresh_row(self, row):
        spu = int(row)
        item = self.items.get(spu)
        if not item:
            return
        self.tree.item(row, values=self._row_values(item),
                       tags=(self._tag(item),))

    def _tag(self, it):
        return {"sell_out": "sell", "restock": "restore", "update": "update",
                "skip_off_shelf": "skip", "manual": "manual"}.get(it["action"], "")

    def _row_values(self, it):
        spu = int(it["spu"])
        target = ""
        if it.get("target_state"):
            target = "不限" if it["target_stock"] is None else str(it["target_stock"])
            target += " " + state_cn(it["target_state"])
        hg = "" if it["hg_qty"] is None else f'{it["hg_qty"]:g}'
        cur = "" if it["cur_stock"] is None else str(it["cur_stock"])
        return ("☑" if spu in self.selected else "☐", it["name"], it["category"],
                it["barcode"], cur, state_cn(it["cur_state"]), hg, it["unit"],
                target, engine.ACTION_CN.get(it["action"], it["action"]),
                it["status"])

    # ---------- 排序 ----------
    def sort_by(self, key):
        if key == "sel":
            ids = {int(r) for r in self.tree.get_children()}
            self.select_all(not ids.issubset(self.selected))
            return
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = key, False
        self._resort()

    def _sort_val(self, it, key):
        if key == "name":
            return it["name"] or ""
        if key == "cat":
            return it["category"] or ""
        if key == "bc":
            return it["barcode"] or ""
        if key == "cur":
            v = it["cur_stock"]
            return (v is None, v if v is not None else 0)
        if key == "curstate":
            return state_cn(it["cur_state"])
        if key == "hg":
            v = it["hg_qty"]
            return (v is None, v if v is not None else 0)
        if key == "unit":
            return it["unit"] or ""
        if key == "target":
            v = it["target_stock"]
            return (v is None, v if v is not None else 0)
        if key == "action":
            return engine.ACTION_CN.get(it["action"], it["action"])
        return it["status"] or ""

    def _resort(self):
        items = [it for it in self.items.values()
                 if (not self.only_diff.get() or it["changed"])]
        if self.sort_key:
            items.sort(key=lambda it: self._sort_val(it, self.sort_key),
                       reverse=self.sort_desc)
        self.tree.delete(*self.tree.get_children())
        for it in items:
            self._insert(it)
        for key, title, *_ in self.COLS:
            text = title
            if key == self.sort_key:
                text += " ▼" if self.sort_desc else " ▲"
            self.tree.heading(key, text=text)

    # ---------- 手动修改 ----------
    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("手动修改", "先点一行，再点「手动修改」")
            return
        self.open_edit(int(sel[0]))

    def batch_edit(self):
        spus = sorted(self.selected)
        if not spus:
            messagebox.showinfo("批量修改", "先勾选要修改的商品，再点「批量修改」")
            return
        win = tk.Toplevel(self)
        win.title(f"批量修改 · {len(spus)} 个商品")
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        frame = tk.Frame(win, bg=CARD)
        frame.pack(padx=18, pady=14)
        tk.Label(frame, text=f"将修改已勾选的 {len(spus)} 个商品", bg=CARD, fg=TEXT,
                 font=FONT_B).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(frame, text="留空 = 不修改；库存填「不限」= 清空库存限制",
                 bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        stock_var = tk.StringVar()
        price_var = tk.StringVar()
        state_var = tk.StringVar(value="不修改")
        for i, (label, var, widget) in enumerate((
                ("库存", stock_var, "entry"),
                ("售价（元）", price_var, "entry"),
                ("状态", state_var, "combo"))):
            tk.Label(frame, text=label, bg=CARD, fg=SUB, font=FONT).grid(
                row=2 + i, column=0, sticky="w", pady=4)
            if widget == "entry":
                ttk.Entry(frame, textvariable=var, width=22).grid(
                    row=2 + i, column=1, sticky="ew", pady=4)
            else:
                ttk.Combobox(frame, textvariable=var, width=20, state="readonly",
                             values=["不修改", "在售", "售罄", "下架"]).grid(
                    row=2 + i, column=1, sticky="ew", pady=4)

        def apply():
            stock_txt = stock_var.get().strip()
            keep_stock = stock_txt == ""
            stock_val = None
            if not keep_stock:
                if stock_txt == "不限":
                    stock_val = None
                else:
                    try:
                        stock_val = int(float(stock_txt))
                        if stock_val < 0:
                            raise ValueError
                    except ValueError:
                        messagebox.showwarning("库存不对", "库存填 0/正整数、留空或「不限」",
                                               parent=win)
                        return
            price_val = None
            if price_var.get().strip():
                try:
                    price_val = listing.to_fen(float(price_var.get()))
                    if price_val <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showwarning("价格不对", "售价格式不对", parent=win)
                    return
            chosen = state_var.get()
            for spu in spus:
                it = self.items.get(spu)
                if not it:
                    continue
                if not keep_stock:
                    it["target_stock"] = stock_val
                st = None
                if chosen != "不修改":
                    st = {"在售": engine.NORMAL, "售罄": engine.SOLD_OUT,
                          "下架": engine.OFF_SHELF}[chosen]
                elif not keep_stock:
                    st = (engine.NORMAL if (stock_val is None or stock_val > 0)
                          else engine.SOLD_OUT)
                if st is not None:
                    it["target_state"] = st
                elif not it.get("target_state"):
                    it["target_state"] = it["cur_state"]
                if it["target_state"] == engine.SOLD_OUT:
                    it["target_stock"] = 0
                if price_val is not None:
                    it["target_price"] = price_val
                it["changed"] = True
                it["action"] = "manual"
                parts = []
                if not keep_stock:
                    parts.append("库存 " + ("不限" if stock_val is None else str(stock_val)))
                if chosen != "不修改":
                    parts.append(chosen)
                if price_val is not None:
                    parts.append(f"¥{price_val / 100:.2f}")
                it["status"] = "批量：" + " / ".join(parts)
                self.refresh_row(str(spu))
            self.update_apply_button()
            self.backend.log(f"批量修改 {len(spus)} 个商品")
            win.destroy()

        row = tk.Frame(frame, bg=CARD)
        row.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="应用", style="Primary.TButton",
                   command=apply).pack(side="right", padx=(0, 8))
        center_window(win, self.winfo_toplevel())
        win.grab_set()

    def open_edit(self, spu, on_saved=None, on_closed=None):
        it = self.items.get(spu)
        if not it:
            return
        win = tk.Toplevel(self)
        win.title(f'手动修改 · {it["name"]}')
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        self.edit_win = win
        self.edit_spu = spu
        self.edit_img_ref = None

        frame = tk.Frame(win, bg=CARD)
        frame.pack(padx=18, pady=14)
        tk.Label(frame, text=it["name"], bg=CARD, fg=TEXT, font=FONT_B,
                 wraplength=360, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(frame, text=f'条码 {it["barcode"]} · 惠管家 {it["hg_qty"]:g}{it["unit"]}',
                 bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        img_row = tk.Frame(frame, bg=CARD)
        img_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.edit_img = tk.Label(img_row, text="无图", bg="#f7f8fa", fg=SUB,
                                 width=26, height=7)
        self.edit_img.pack(side="left")
        side = tk.Frame(img_row, bg=CARD)
        side.pack(side="left", padx=(12, 0), anchor="n")
        self.edit_img_btn = ttk.Button(side, text="更换图片…", command=lambda: upload())
        self.edit_img_btn.pack(anchor="w")
        self.edit_img_tip = tk.Label(side, text="JPG/PNG，会自动压到 2MB 内",
                                     bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8),
                                     wraplength=150, justify="left")
        self.edit_img_tip.pack(anchor="w", pady=(6, 0))

        name_var = tk.StringVar(value=it["name"])
        stock_var = tk.StringVar(
            value="" if it["cur_stock"] is None else str(it["cur_stock"]))
        price_var = tk.StringVar(
            value=f'{(it["goods"].get("price") or 0) / 100:.2f}')
        state_var = tk.StringVar(value=state_cn(it["cur_state"]))
        for i, (label, var, widget) in enumerate((
                ("商品名称", name_var, "entry"),
                ("售价（元）", price_var, "entry"),
                ("库存（空=不限）", stock_var, "entry"),
                ("状态", state_var, "combo"))):
            tk.Label(frame, text=label, bg=CARD, fg=SUB, font=FONT).grid(
                row=3 + i, column=0, sticky="w", pady=4)
            if widget == "entry":
                ttk.Entry(frame, textvariable=var, width=26).grid(
                    row=3 + i, column=1, sticky="ew", pady=4)
            else:
                ttk.Combobox(frame, textvariable=var, width=24, state="readonly",
                             values=["在售", "售罄", "下架"]).grid(
                    row=3 + i, column=1, sticky="ew", pady=4)

        def upload():
            path = filedialog.askopenfilename(
                parent=win, title="选择商品图片",
                filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp"),
                           ("所有文件", "*.*")])
            if not path:
                return
            try:
                raw = open(path, "rb").read()
            except OSError as e:
                messagebox.showerror("读不到图片", str(e), parent=win)
                return
            self.edit_img_btn.configure(state="disabled")
            self.edit_img_tip.configure(text="上传中…")

            def job():
                try:
                    jpg = listing.prepare_image(raw)
                    client = DxzsClient(self.backend.sid, self.backend.cfg["dxzs"],
                                        log=self.backend.log)
                    url = client.upload_image(jpg, os.path.basename(path))
                    self.backend.post("edit_image_uploaded", (spu, url, jpg))
                except Exception as e:  # noqa: BLE001
                    self.backend.post("edit_image_fail", (spu, str(e)))

            threading.Thread(target=job, daemon=True).start()

        def save():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("名称不能为空", "请填写商品名称", parent=win)
                return
            stock_txt = stock_var.get().strip()
            try:
                stock = None if not stock_txt else int(float(stock_txt))
                if stock is not None and stock < 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("库存不对", "库存要么留空，要么填 0 或正整数",
                                       parent=win)
                return
            try:
                price = float(price_var.get())
                if price <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("价格不对", "请填写大于 0 的售价", parent=win)
                return
            st = {"在售": engine.NORMAL, "售罄": engine.SOLD_OUT,
                  "下架": engine.OFF_SHELF}[state_var.get()]
            if st == engine.SOLD_OUT:
                stock = 0
            it["name"] = name
            it["goods"]["name"] = name
            it["target_stock"] = stock
            it["target_state"] = st
            it["target_price"] = listing.to_fen(price)
            it["changed"] = True
            it["action"] = "manual"
            it["status"] = (f'手动：{name} / 库存 {"不限" if stock is None else stock} / '
                            f'{state_var.get()} / ¥{price:.2f}')
            self.selected.add(spu)
            self.refresh_row(str(spu))
            self.update_apply_button()
            self.backend.log(f'手动修改：{it["status"]}')
            if on_saved:
                on_saved()
            close()

        def close():
            self.edit_win = None
            self.edit_spu = None
            self.edit_img_ref = None
            win.destroy()
            if on_closed:
                on_closed()

        btns = tk.Frame(frame, bg=CARD)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=close).pack(side="right")
        ttk.Button(btns, text="保存", style="Primary.TButton",
                   command=save).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", close)
        win.grab_set()
        center_window(win, self.winfo_toplevel())

        urls = it["goods"].get("image_urls_list") or []
        url = urls[0] if urls else (it["goods"].get("image_url") or "")
        if url:
            def load():
                try:
                    from wxds import net
                    st, _, raw = net.request(net.make_opener(), url, timeout=20,
                                             headers={"User-Agent": net.UA_BROWSER})
                    if st == 200 and raw:
                        self.backend.post("edit_image", (spu, raw))
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=load, daemon=True).start()
        else:
            self.edit_img_tip.configure(text="该商品还没有图片")

    def show_edit_image(self, spu, raw):
        if not self.edit_win or self.edit_spu != spu:
            return
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((220, 160))
            photo = ImageTk.PhotoImage(im)
            self.edit_img_ref = photo
            self.edit_img.configure(image=photo, text="", width=220, height=160)
        except Exception:  # noqa: BLE001
            self.edit_img.configure(image="", text="图片无法显示", width=26, height=7)

    def edit_upload_done(self, spu, url, raw):
        if not self.edit_win or self.edit_spu != spu:
            return
        it = self.items.get(spu)
        if it:
            if not it.get("target_state"):
                it["target_state"] = it["cur_state"]
                it["target_stock"] = it["cur_stock"]
            it["goods"]["image_url"] = url
            it["goods"]["image_urls_list"] = [url]
            it["image_changed"] = True
            it["changed"] = True
            if it["action"] not in ("manual",):
                it["action"] = "manual"
            if "图片" not in it["status"]:
                it["status"] += "（图片已更换）"
            self.selected.add(spu)
            self.refresh_row(str(spu))
            self.update_apply_button()
            self.backend.log(f"图片已上传：{it['name']}")
        self.edit_img_btn.configure(state="normal")
        self.edit_img_tip.configure(text="已更换，同步时写回")
        self.show_edit_image(spu, raw)

    def edit_upload_fail(self, spu, msg):
        if not self.edit_win or self.edit_spu != spu:
            return
        self.edit_img_btn.configure(state="normal")
        self.edit_img_tip.configure(text=f"上传失败：{msg}")


    def select_all(self, on, only_changed=False):
        if only_changed:
            self.selected = ({spu for spu, it in self.items.items() if it["changed"]}
                             if on else set())
        else:
            self.selected = ({int(r) for r in self.tree.get_children()} if on else set())
        for row in self.tree.get_children():
            self.refresh_row(row)
        self.tree.heading("sel", text="☑" if on else "全选")
        self.update_apply_button()

    def update_apply_button(self):
        n = len(self.selected)
        self.btn_apply.configure(text=f"同步选中 ({n})",
                                 state="normal" if n else "disabled")

    def apply(self):
        changed = [spu for spu in self.selected
                   if self.items.get(spu) and self.items[spu]["changed"]]
        if not changed:
            messagebox.showinfo(
                "没有可同步的修改",
                "选中的商品都是「已一致」。\n\n"
                "可以双击某一行，或勾选后点「批量修改」，改完再同步。")
            return
        SyncPreview(self, changed)

    def open_backup(self):
        path = os.path.join(config.data_dir(), "backup")
        os.makedirs(path, exist_ok=True)
        os.startfile(path)  # noqa: S606

    # ---------- 数据 ----------
    def set_plan(self, plan):
        self.items = {int(i["spu"]): i for i in plan["items"]}
        self.selected = set()
        self.sort_key, self.sort_desc = None, False
        self._resort()
        s = plan["summary"]
        self.summary.configure(
            text=f'匹配 {s["matched"]} · 待同步 {s["changed"]}（售罄 {s["sell_out"]} / '
                 f'恢复 {s["restock"]} / 改库存 {s["update"]}）· 已下架跳过 '
                 f'{s["skip_off_shelf"]} · 无条码 {s["no_barcode"]} · '
                 f'惠管家缺 {s["hg_missing"]} · 价格不一致 {s["price_warn"]}')
        self.update_apply_button()

    def _insert(self, it):
        spu = int(it["spu"])
        self.tree.insert("", "end", iid=str(spu), tags=(self._tag(it),),
                         values=self._row_values(it))

    def refresh_changed(self):
        if not self.backend.plan:
            return
        self.items = {int(i["spu"]): i for i in self.backend.plan["items"]}
        self.selected = set()
        self.tree.heading("sel", text="全选")
        self._resort()
        self.update_apply_button()

    def reset(self):
        self.items = {}
        self.selected = set()
        self.sort_key, self.sort_desc = None, False
        self.tree.delete(*self.tree.get_children())
        self.summary.configure(text="点「拉取并比对」开始")
        self.update_apply_button()


class SyncPreview(tk.Toplevel):
    """同步前的预览：列出本次会改什么，可逐条取消、可再手动修改。"""

    COLS = [("sel", "", 38, "center"),
            ("name", "商品", 240, "w"),
            ("cur", "当前", 130, "center"),
            ("target", "同步后", 130, "center"),
            ("price", "售价", 160, "center"),
            ("note", "说明", 260, "w")]

    def __init__(self, stock_tab, spus=None):
        super().__init__(stock_tab.winfo_toplevel())
        self.stock_tab = stock_tab
        self.items = stock_tab.items
        self.spus = sorted(spus if spus is not None else stock_tab.selected)
        self.picked = set(self.spus)
        self.title(f"确认同步 · {len(self.spus)} 个商品")
        self.configure(bg=CARD)
        self.transient(stock_tab.winfo_toplevel())
        self.minsize(900, 520)
        self._build()
        center_window(self, stock_tab.winfo_toplevel())
        self.grab_set()

    def _build(self):
        tk.Label(self, text=f"本次将修改 {len(self.spus)} 个商品，确认前可以逐条核对：",
                 bg=CARD, fg=TEXT, font=FONT_B).pack(anchor="w", padx=16, pady=(14, 6))

        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("name", "note")))
        self.tree.tag_configure("sell", background="#ffe2e2")
        self.tree.tag_configure("restore", background="#dcf7e7")
        self.tree.tag_configure("update", background="#fff3c4")
        self.tree.tag_configure("manual", background="#ffe6cc")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._on_click)

        bar = tk.Frame(self, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(bar, text="全选", command=lambda: self._pick_all(True)).pack(side="left")
        ttk.Button(bar, text="清空", command=lambda: self._pick_all(False)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="修改选中", command=self._edit_selected).pack(
            side="left", padx=(16, 0))
        self.btn_ok = ttk.Button(bar, text="确认同步", style="Danger.TButton",
                                 command=self._confirm)
        self.btn_ok.pack(side="right")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))
        self.tree.bind("<Double-1>", lambda _e: self._edit_selected())
        self._refresh()

    def _rows(self):
        for spu in self.spus:
            it = self.items.get(spu)
            if not it:
                continue
            yield spu, it

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for spu, it in self._rows():
            cur = (("不限" if it["cur_stock"] is None else str(it["cur_stock"]))
                   + " " + state_cn(it["cur_state"]))
            target = (("不限" if it["target_stock"] is None else str(it["target_stock"]))
                      + " " + state_cn(it["target_state"]))
            old_price = (it["goods"].get("price") or 0) / 100
            if it.get("target_price") is not None:
                price = f"¥{old_price:.2f} → ¥{it['target_price'] / 100:.2f}"
            else:
                price = f"¥{old_price:.2f}"
            note = it["status"]
            if it.get("image_changed"):
                note += " · 图片已更换"
            if it.get("target_price") is not None and it["target_price"] != int(old_price * 100):
                note += " · 改价"
            self.tree.insert("", "end", iid=str(spu),
                             tags=(self.stock_tab._tag(it),),
                             values=("☑" if spu in self.picked else "☐",
                                     it["name"], cur, target, price, note))
        n = len(self.picked)
        self.btn_ok.configure(text=f"确认同步 ({n})", state="normal" if n else "disabled")

    def _on_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        spu = int(row)
        if spu in self.picked:
            self.picked.discard(spu)
        else:
            self.picked.add(spu)
        self.tree.set(row, "sel", "☑" if spu in self.picked else "☐")
        self.btn_ok.configure(text=f"确认同步 ({len(self.picked)})",
                              state="normal" if self.picked else "disabled")
        return "break"

    def _pick_all(self, on):
        self.picked = set(self.spus) if on else set()
        self._refresh()

    def _edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("手动修改", "先点一行，再点「修改选中」", parent=self)
            return
        self.grab_release()
        self.stock_tab.open_edit(int(sel[0]), on_saved=self._refresh,
                                 on_closed=self._regrab)

    def _regrab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self._refresh()

    def _confirm(self):
        if not self.picked:
            return
        n = len(self.picked)
        if not messagebox.askyesno(
                "确认写回",
                f"将修改 {n} 个商品。\n写回不可撤销，工具会先自动备份。\n继续？",
                parent=self, icon="warning"):
            return
        spus = sorted(self.picked)
        self.destroy()
        self.stock_tab.backend.apply_stock(spus)


class AddTab(ttk.Frame):
    COLS = [
        ("sel", "", 34, "center"),
        ("name", "商品名（惠管家）", 250, "w"),
        ("bc", "条码", 160, "w"),
        ("cat", "惠管家分类", 96, "w"),
        ("unit", "单位", 50, "center"),
        ("qty", "库存", 62, "center"),
        ("price", "售价(元)", 74, "center"),
        ("tcat", "放入分类", 96, "w"),
        ("pic", "图片", 54, "center"),
    ]

    def __init__(self, master, app):
        super().__init__(master, style="Tab.TFrame")
        self.app = app
        self.backend = app.backend
        self.selected = set()
        self.cands = {}
        self.all_cands = []
        self.photo_ref = None
        self.thumbs = {}
        self.only_stock = tk.BooleanVar(value=False)
        self.kw_var = tk.StringVar()
        self.placeholder = self._make_placeholder()
        self._build()
        self.kw_var.trace("w", lambda *a: self.apply_filter())

    def _make_placeholder(self):
        img = Image.new("RGB", (34, 34), (241, 243, 245))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, 33, 33), outline=(220, 224, 228))
        d.line((12, 22, 16, 17, 19, 20, 22, 15, 24, 22), fill=(180, 186, 192), width=2)
        return ImageTk.PhotoImage(img)

    def _build(self):
        bar = ttk.Frame(self, style="Tab.TFrame")
        bar.pack(fill="x", padx=14, pady=(12, 4))
        ttk.Button(bar, text="拉取上架候选", style="Primary.TButton",
                   command=lambda: self.backend.load_candidates(True)).pack(side="left")
        ttk.Button(bar, text="全选", command=lambda: self.select_all(True)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="清空", command=lambda: self.select_all(False)).pack(
            side="left", padx=(8, 0))
        self.btn_add = ttk.Button(bar, text="上架选中 (0)", style="Danger.TButton",
                                  command=self.add, state="disabled")
        self.btn_add.pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="批量修改", command=self.batch_edit).pack(
            side="left", padx=(8, 0))

        self.count_lbl = tk.Label(bar, text="", bg=BG, fg=SUB,
                                  font=("Microsoft YaHei UI", 8))
        self.count_lbl.pack(side="right", padx=(10, 0))
        ttk.Checkbutton(bar, text="只看有库存", variable=self.only_stock,
                        command=self.apply_filter).pack(side="right")
        self.kw_entry = ttk.Entry(bar, textvariable=self.kw_var, width=18)
        self.kw_entry.pack(side="right", padx=(0, 8))
        tk.Label(bar, text="搜索", bg=BG, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).pack(side="right", padx=(0, 4))

        tk.Label(self, text="分类要手动选：选中一行 → 右侧「放入分类」→ 保存；批量勾选后会弹窗统一选。"
                            "称重商品按份上架、库存不限。",
                 bg=BG, fg=SUB, font=("Microsoft YaHei UI", 8), anchor="w").pack(
            fill="x", padx=16, pady=(0, 8))

        body = ttk.Frame(self, style="Tab.TFrame")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        wrap = ttk.Frame(body, style="Card.TFrame")
        wrap.grid(row=0, column=0, sticky="nsew")
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS],
                                 show="tree headings", style="Add.Treeview",
                                 selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=46, anchor="center", stretch=False)
        for key, title, width, anchor in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "name"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        panel = tk.Frame(body, bg=CARD, width=330)
        panel.grid(row=0, column=1, sticky="ns", padx=(12, 0))
        panel.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(5, weight=1)
        tk.Label(panel, text="上架详情", bg=CARD, fg=TEXT, font=FONT_B,
                 anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        self.preview = tk.Label(panel, text="选中商品查看图片", bg="#f7f8fa",
                                fg=SUB, width=28, height=5)
        self.preview.grid(row=1, column=0, padx=14, pady=(0, 10), sticky="ew")

        form = tk.Frame(panel, bg=CARD)
        form.grid(row=2, column=0, sticky="ew", padx=14)
        form.columnconfigure(0, weight=1)
        tk.Label(form, text="名称", bg=CARD, fg=SUB, font=FONT).grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.name_var).grid(row=1, column=0, sticky="ew", pady=(2, 8))
        tk.Label(form, text="售价（元）", bg=CARD, fg=SUB, font=FONT).grid(row=2, column=0, sticky="w")
        self.price_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.price_var).grid(row=3, column=0, sticky="ew", pady=(2, 8))
        tk.Label(form, text="放入分类", bg=CARD, fg=SUB, font=FONT).grid(row=4, column=0, sticky="w")
        self.cat_var = tk.StringVar()
        self.cat_box = ttk.Combobox(form, textvariable=self.cat_var, state="readonly")
        self.cat_box.grid(row=5, column=0, sticky="ew", pady=(2, 8))
        self.info = tk.Label(form, text="", bg=CARD, fg=SUB,
                             font=("Microsoft YaHei UI", 8), justify="left",
                             wraplength=290)
        self.info.grid(row=6, column=0, sticky="ew", pady=(2, 8))
        ttk.Button(form, text="保存到该商品", command=self.save_detail).grid(
            row=7, column=0, sticky="ew")

    # ---------- 交互 ----------
    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        bc = sel[0]
        c = self.cands.get(bc)
        if not c:
            return
        self.name_var.set(c.get("name_edit") or c["name"])
        self.price_var.set(f'{c["price_fen"] / 100:.2f}')
        self.cat_var.set(self._cat_name(c.get("category_id")))
        extra = "称重商品：按份卖，库存不限" if c["weight"] else f'库存 {c["qty"]:g} {c["unit"]}'
        self.info.configure(text=f'{extra}\n条码 {c["barcode"]}\n惠管家分类 {c["hg_category"]}')
        self.load_preview(c)

    def _cat_name(self, cid):
        for c in self.backend.categories:
            if c.get("category_id") == cid:
                return c.get("category_name")
        return ""

    def _cat_id(self, name):
        for c in self.backend.categories:
            if c.get("category_name") == name:
                return c.get("category_id")
        return 0

    def load_preview(self, c):
        photo = c.get("photo") or ""
        if photo.startswith("file:"):
            try:
                with open(photo[5:], "rb") as f:
                    self._show_image(f.read())
            except OSError as e:
                self.preview.configure(image="", text=f"图片无法显示：{e}")
            return
        if not photo:
            self.preview.configure(image="", text="该商品没有图片", width=28, height=5)
            return
        bc = c["barcode"]
        raw = self.backend.image_cache.get(bc)
        if raw is None:
            self.preview.configure(image="", text="图片加载中…", width=28, height=5)

            def job():
                try:
                    data = download_image(photo_url(c["photo"]))
                    self.backend.image_cache[bc] = data
                    self.backend.post("preview", (bc, data))
                except Exception as e:  # noqa: BLE001
                    self.backend.post("preview_fail", (bc, str(e)))

            threading.Thread(target=job, daemon=True).start()
        else:
            self._show_image(raw)

    def _show_image(self, raw):
        try:
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(raw))
            im.thumbnail((240, 125))
            photo = ImageTk.PhotoImage(im)
            self.photo_ref = photo
            self.preview.configure(image=photo, text="", width=240, height=125)
        except Exception as e:  # noqa: BLE001
            self.preview.configure(image="", text=f"图片无法显示：{e}", width=28, height=5)

    def on_click(self, event):
        if self.tree.identify_column(event.x) not in ("#0", "#1"):
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row in self.selected:
            self.selected.discard(row)
        else:
            self.selected.add(row)
        self.tree.set(row, "sel", "☑" if row in self.selected else "☐")
        self.update_add_button()
        return "break"

    def select_all(self, on):
        self.selected = set(self.cands) if on else set()
        for row in self.tree.get_children():
            self.tree.set(row, "sel", "☑" if row in self.selected else "☐")
        self.update_add_button()

    def update_add_button(self):
        n = len(self.selected)
        self.btn_add.configure(text=f"上架选中 ({n})",
                               state="normal" if n else "disabled")

    def save_detail(self):
        sel = self.tree.selection()
        if not sel:
            return
        bc = sel[0]
        c = self.cands.get(bc)
        if not c:
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("名称不能为空", "请填写商品名称")
            return
        try:
            price = float(self.price_var.get())
            if price <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("价格不对", "请填写大于 0 的售价（元）")
            return
        cat_name = self.cat_var.get()
        cid = self._cat_id(cat_name)
        if not cid:
            messagebox.showwarning("请选择分类", "上架前必须选择放入哪个分类")
            return
        c["name_edit"] = name
        c["price_fen"] = listing.to_fen(price)
        c["category_id"] = cid
        self.tree.set(bc, "name", name)
        self.tree.set(bc, "price", f"{price:.2f}")
        self.tree.set(bc, "tcat", cat_name)
        self.backend.log(f"已修改：{name} ¥{price:.2f} → {cat_name}")

    def _ask_category(self, count):
        names = [c.get("category_name") for c in self.backend.categories
                 if c.get("category_name")]
        if not names:
            messagebox.showwarning("没有分类", "还没拉到小程序的分类，请先拉取上架候选")
            return None
        win = tk.Toplevel(self)
        win.title("选择分类")
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        tk.Label(win, text=f"{count} 个商品放入哪个分类？", bg=CARD, fg=TEXT,
                 font=FONT).pack(padx=20, pady=(18, 8), anchor="w")
        var = tk.StringVar(value=names[0])
        ttk.Combobox(win, textvariable=var, values=names, state="readonly",
                     width=28).pack(padx=20, pady=(0, 14))
        holder = {"v": None}
        row = tk.Frame(win, bg=CARD)
        row.pack(fill="x", padx=20, pady=(0, 16))

        def ok():
            holder["v"] = var.get()
            win.destroy()

        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="确定", style="Primary.TButton", command=ok).pack(
            side="right", padx=(0, 8))
        center_window(win, self.winfo_toplevel())
        win.grab_set()
        self.wait_window(win)
        return holder["v"]

    def batch_edit(self):
        sel = sorted(self.selected)
        if not sel:
            messagebox.showinfo("批量修改", "先勾选要批量修改的商品")
            return
        names = [c.get("category_name") for c in self.backend.categories
                 if c.get("category_name")]
        win = tk.Toplevel(self)
        win.title(f"批量修改 · {len(sel)} 个商品")
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        frame = tk.Frame(win, bg=CARD)
        frame.pack(padx=18, pady=14)
        tk.Label(frame, text=f"将修改已勾选的 {len(sel)} 个商品", bg=CARD, fg=TEXT,
                 font=FONT_B).pack(anchor="w")
        tk.Label(frame, text="留空 / 选「不修改」表示保持原样", bg=CARD, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 10))

        form = tk.Frame(frame, bg=CARD)
        form.pack(fill="x")
        tk.Label(form, text="放入分类", bg=CARD, fg=SUB, font=FONT).grid(
            row=0, column=0, sticky="w", pady=4)
        cat_var = tk.StringVar(value="不修改")
        ttk.Combobox(form, textvariable=cat_var, state="readonly", width=24,
                     values=["不修改"] + names).grid(row=0, column=1, sticky="ew", pady=4)
        tk.Label(form, text="售价（元）", bg=CARD, fg=SUB, font=FONT).grid(
            row=1, column=0, sticky="w", pady=4)
        price_var = tk.StringVar()
        ttk.Entry(form, textvariable=price_var, width=26).grid(
            row=1, column=1, sticky="ew", pady=4)

        def apply():
            cat = cat_var.get()
            cid = 0
            if cat != "不修改":
                cid = self._cat_id(cat)
                if not cid:
                    messagebox.showwarning("分类无效", "找不到这个分类", parent=win)
                    return
            fen = None
            if price_var.get().strip():
                try:
                    fen = listing.to_fen(float(price_var.get()))
                    if fen <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showwarning("价格不对", "请填写大于 0 的售价", parent=win)
                    return
            for bc in sel:
                c = self.cands.get(bc)
                if not c:
                    continue
                if cid:
                    c["category_id"] = cid
                    self.tree.set(bc, "tcat", cat)
                if fen is not None:
                    c["price_fen"] = fen
                    self.tree.set(bc, "price", f"{fen / 100:.2f}")
            self.backend.log(f"批量修改 {len(sel)} 个上架商品")
            win.destroy()

        btns = tk.Frame(frame, bg=CARD)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="应用", style="Primary.TButton",
                   command=apply).pack(side="right", padx=(0, 8))
        center_window(win, self.winfo_toplevel())
        win.grab_set()

    def add(self):
        if not self.selected:
            return
        missing = [bc for bc in self.selected if not self.cands[bc].get("category_id")]
        if missing:
            cat = self._ask_category(len(missing))
            if not cat:
                return
            cid = self._cat_id(cat)
            for bc in missing:
                self.cands[bc]["category_id"] = cid
                self.tree.set(bc, "tcat", cat)
            self.backend.log(f"{len(missing)} 个商品放入分类：{cat}")
        n = len(self.selected)
        if not messagebox.askyesno(
                "确认上架",
                f"将在小程序新建 {n} 个商品（含图片上传）。\n\n"
                "建好后不能批量删除，请确认名称、价格、分类无误。\n继续？",
                icon="warning"):
            return
        self.backend.add_products([self.cands[bc] for bc in self.selected])

    # ---------- 数据 ----------
    def set_candidates(self, cands):
        self.all_cands = list(cands)
        self.selected = set()
        names = [c.get("category_name") or "" for c in self.backend.categories]
        self.cat_box.configure(values=[n for n in names if n])
        self.apply_filter()

    def apply_filter(self):
        if not self.all_cands:
            return
        kw = self.kw_var.get().strip().lower()
        only = self.only_stock.get()
        cands = []
        for c in self.all_cands:
            if only and c["qty"] <= 0:
                continue
            if kw and kw not in (c["name"] or "").lower() and kw not in c["barcode"]:
                continue
            cands.append(c)
        self.cands = {c["barcode"]: c for c in cands}
        self.selected &= set(self.cands)
        self.tree.delete(*self.tree.get_children())
        for c in cands:
            tag = "" if c["qty"] > 0 else "nostock"
            img = self.thumbs.get(c["barcode"], self.placeholder)
            self.tree.insert("", "end", iid=c["barcode"], image=img, tags=(tag,),
                             values=(
                                 "☐", c["name"], c["barcode"], c["hg_category"], c["unit"],
                                 f'{c["qty"]:g}', f'{c["price_fen"] / 100:.2f}', "",
                                 "有" if c["photo"] else "无"))
        self.tree.tag_configure("nostock", foreground="#9aa0a6")
        in_stock = sum(1 for c in self.all_cands if c["qty"] > 0)
        self.count_lbl.configure(
            text=f"显示 {len(cands)} / 共 {len(self.all_cands)}（有库存 {in_stock}）")
        self.update_add_button()
        self.load_thumbs(cands)

    def load_thumbs(self, cands):
        todo = [c for c in cands if c.get("photo")][:120]
        if not todo:
            return

        def job():
            for c in todo:
                bc = c["barcode"]
                raw = self.backend.image_cache.get(bc)
                if raw is None:
                    try:
                        photo = c["photo"]
                        if photo.startswith("file:"):
                            with open(photo[5:], "rb") as f:
                                raw = f.read()
                        else:
                            raw = download_image(photo_url(photo))
                        self.backend.image_cache[bc] = raw
                    except Exception:  # noqa: BLE001
                        continue
                self.backend.post("thumb", (bc, raw))
                time.sleep(0.03)

        threading.Thread(target=job, daemon=True).start()

    def set_thumb(self, bc, raw):
        if bc not in self.cands:
            return
        try:
            im = Image.open(io.BytesIO(raw))
            im = im.convert("RGB")
            im.thumbnail((36, 36))
            ph = ImageTk.PhotoImage(im)
            self.thumbs[bc] = ph
            if self.tree.exists(bc):
                self.tree.item(bc, image=ph)
        except Exception:  # noqa: BLE001
            pass

    def remove_added(self, done):
        for bc in done:
            self.all_cands = [c for c in self.all_cands if c["barcode"] != bc]
            self.cands.pop(bc, None)
            self.selected.discard(bc)
        self.apply_filter()

    def reset(self):
        self.all_cands = []
        self.cands = {}
        self.selected = set()
        self.tree.delete(*self.tree.get_children())
        self.count_lbl.configure(text="")
        self.update_add_button()


class SortTab(ttk.Frame):
    """分类内商品排序 + 标签（无/招牌/新品/热销）。"""

    COLS = [("idx", "序", 44, "center"),
            ("name", "商品", 460, "w"),
            ("tag", "标签", 72, "center"),
            ("stock", "库存", 80, "center"),
            ("sales", "销量", 80, "center"),
            ("price", "售价", 80, "center"),
            ("fill", "", 10, "w")]
    STRATEGIES = ["手动（用上移/下移调整）", "名称", "库存从多到少",
                  "销量从高到低", "价格从低到高", "条码"]

    def __init__(self, master, app):
        super().__init__(master, style="Tab.TFrame")
        self.app = app
        self.backend = app.backend
        self.cats = []
        self.order = {}
        self.items = {}
        self.cur_cid = None
        self.dirty = False
        self.tag_changes = {}
        self.thumbs = {}
        self.thumb_raw = {}
        self.thumb_pil = {}
        self.thumb_size_used = {}
        self.row_h = 44
        self.thumb_size = 36
        self._zoom_job = None
        self._pending_h = None
        self.placeholder = self._make_placeholder()
        self._drag_iid = None
        self._drag_y = 0
        self._dragging = False
        self._build()

    def _make_placeholder(self, size=None):
        size = int(size or self.thumb_size)
        img = Image.new("RGB", (size, size), (241, 243, 245))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, size - 1, size - 1), outline=(220, 224, 228))
        m = max(2, size // 3)
        d.line((m // 2, size - m // 2, m, size - m, m + m // 2, size - m // 3,
                size - m, m // 2, size - m // 3, size - m // 2),
               fill=(180, 186, 192), width=2)
        return ImageTk.PhotoImage(img)

    def _build(self):
        bar = ttk.Frame(self, style="Tab.TFrame")
        bar.pack(fill="x", padx=14, pady=(12, 4))
        self.btn_load = ttk.Button(bar, text="拉取商品", style="Primary.TButton",
                                   command=self.start_load)
        self.btn_load.pack(side="left")
        self.load_lbl = tk.Label(bar, text="", bg=BG, fg=SUB,
                                 font=("Microsoft YaHei UI", 8))
        self.load_lbl.pack(side="left", padx=(10, 0))
        tk.Label(bar, text="分类", bg=BG, fg=SUB, font=FONT).pack(side="left", padx=(12, 4))
        self.cat_var = tk.StringVar()
        self.cat_box = ttk.Combobox(bar, textvariable=self.cat_var, state="readonly",
                                    width=14)
        self.cat_box.pack(side="left")
        self.cat_box.bind("<<ComboboxSelected>>", self.on_cat)
        tk.Label(bar, text="策略", bg=BG, fg=SUB, font=FONT).pack(side="left", padx=(12, 4))
        self.strategy = tk.StringVar(value=self.STRATEGIES[0])
        ttk.Combobox(bar, textvariable=self.strategy, state="readonly", width=18,
                     values=self.STRATEGIES).pack(side="left")
        ttk.Button(bar, text="应用策略", command=self.apply_strategy).pack(
            side="left", padx=(8, 0))
        self.btn_save = ttk.Button(bar, text="保存到小程序", style="Danger.TButton",
                                   command=self.save, state="disabled")
        self.btn_save.pack(side="right")

        bar2 = ttk.Frame(self, style="Tab.TFrame")
        bar2.pack(fill="x", padx=14, pady=(0, 4))
        for text, cmd in (("上移", lambda: self.move(-1)),
                          ("下移", lambda: self.move(1)),
                          ("置顶", lambda: self.move(-999)),
                          ("置底", lambda: self.move(999))):
            ttk.Button(bar2, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bar2, text="设置标签", command=self.set_tag).pack(
            side="left", padx=(10, 0))
        ttk.Button(bar2, text="新建分类", command=self.new_category).pack(
            side="left", padx=(10, 0))
        ttk.Button(bar2, text="设置分类", command=self.batch_category).pack(
            side="left", padx=(6, 0))
        tk.Label(bar2, text="拖动排序 · Ctrl+滚轮缩放",
                 bg=BG, fg=SUB, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(12, 0))

        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        ttk.Style().configure("Sort.Treeview", rowheight=self.row_h)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS],
                                 show="tree headings", style="Sort.Treeview",
                                 selectmode="extended")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=self.thumb_size + 12, anchor="center",
                         stretch=False)
        for key, title, width, anchor in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "fill"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _e: self.set_tag())
        self.tree.bind("<Control-MouseWheel>", self.on_zoom)
        self.tree.bind("<ButtonPress-1>", self._on_press)
        self.tree.bind("<B1-Motion>", self._on_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_release)

    # ---------- 拉取状态 ----------
    def start_load(self):
        self.set_loading(True, "准备拉取…")
        self.app.job_text.configure(text="准备拉取商品…")
        self.backend.load_sort_data()

    def set_loading(self, on, text=""):
        try:
            self.btn_load.configure(text="拉取中…" if on else "拉取商品",
                                    state="disabled" if on else "normal")
            self.load_lbl.configure(text=text if on else "")
        except tk.TclError:
            pass

    # ---------- Ctrl+滚轮缩放 ----------
    def on_zoom(self, event):
        target = self.row_h + (8 if event.delta > 0 else -8)
        self._pending_h = max(30, min(160, int(target)))
        if self._zoom_job:
            try:
                self.after_cancel(self._zoom_job)
            except tk.TclError:
                pass
        self._zoom_job = self.after(90, self._apply_zoom)
        return "break"

    def _apply_zoom(self):
        self._zoom_job = None
        h = self._pending_h or self.row_h
        if h == self.row_h:
            return
        self.row_h = h
        self.thumb_size = max(20, h - 8)
        ttk.Style().configure("Sort.Treeview", rowheight=h)
        self.tree.column("#0", width=self.thumb_size + 12)
        self.placeholder = self._make_placeholder(self.thumb_size)
        for iid in self.tree.get_children():
            spu = int(iid)
            im = self.thumb_pil.get(spu)
            if im is None:
                self.tree.item(iid, image=self.placeholder)
                continue
            ph = self._ph_from(im)
            self.thumbs[spu] = ph
            self.thumb_size_used[spu] = self.thumb_size
            self.tree.item(iid, image=ph)
        self.app.job_text.configure(text=f"列表缩放：图片 {self.thumb_size}px")

    def _ph_from(self, im):
        thumb = im.copy()
        thumb.thumbnail((self.thumb_size, self.thumb_size))
        return ImageTk.PhotoImage(thumb)

    def _ph_for(self, spu):
        im = self.thumb_pil.get(spu)
        if im is None:
            return self.placeholder
        if self.thumb_size_used.get(spu) != self.thumb_size:
            self.thumbs[spu] = self._ph_from(im)
            self.thumb_size_used[spu] = self.thumb_size
        return self.thumbs[spu]

    def set_row_height(self, h):
        self._pending_h = h
        self._apply_zoom()

    # ---------- 拖动排序 ----------
    def _on_press(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            self._drag_iid = None
            return
        self._drag_iid = self.tree.identify_row(event.y) or None
        self._drag_y = event.y
        self._dragging = False

    def _on_motion(self, event):
        if not self._drag_iid:
            return
        if not self._dragging:
            if abs(event.y - self._drag_y) < 6:
                return
            self._dragging = True
        target = self.tree.identify_row(event.y)
        if not target or target == self._drag_iid:
            return
        idx = self.tree.index(target)
        self.tree.move(self._drag_iid, "", idx)

    def _on_release(self, _event=None):
        if self._drag_iid and self._dragging and self.cur_cid is not None:
            self.order[self.cur_cid] = [int(x) for x in self.tree.get_children()]
            self.dirty = True
            self._update_save_btn()
            self.backend.log("已拖动调整顺序（未保存）")
        self._drag_iid = None
        self._dragging = False

    # ---------- 数据 ----------
    def set_data(self, cats, order, items):
        self.cats = cats
        self.order = {int(k): list(v) for k, v in order.items()}
        self.items = items
        self.tag_changes = {}
        self.dirty = False
        self.cat_box.configure(values=[c["category_name"] for c in cats])
        if cats:
            self.cat_box.current(0)
            self.cur_cid = cats[0]["category_id"]
        self.btn_save.configure(state="normal")
        self.render()

    def _cat_id(self, name):
        for c in self.cats:
            if c["category_name"] == name:
                return c["category_id"]
        return None

    def on_cat(self, _event=None):
        cid = self._cat_id(self.cat_var.get())
        if cid is None or cid == self.cur_cid:
            return
        if self.dirty and not messagebox.askyesno(
                "排序未保存", "当前分类的排序还没保存，切换会丢弃，继续？"):
            self.cat_var.set(next(c["category_name"] for c in self.cats
                                  if c["category_id"] == self.cur_cid))
            return
        self.cur_cid = cid
        self.dirty = False
        self.render()

    def _tag_of(self, g):
        return self.tag_changes.get(g["spu_id"],
                                    g.get("tag_type") or engine.TAG_NONE)

    def render(self):
        if self.cur_cid is None:
            return
        self.tree.delete(*self.tree.get_children())
        spus = self.order.get(self.cur_cid, [])
        for i, spu in enumerate(spus):
            g = self.items.get(spu)
            if not g:
                continue
            tag = self._tag_of(g)
            self.tree.insert("", "end", iid=str(spu),
                             image=self._ph_for(spu),
                             values=(
                                 i + 1, g.get("name"), engine.TAG_CN.get(tag, tag),
                                 g.get("stock_num", ""), g.get("sales_count", 0),
                                 f'{(g.get("price") or 0) / 100:.2f}', ""))
        self._update_save_btn()
        self.load_thumbs(spus)

    def _update_save_btn(self):
        pending = len(self.tag_changes) + (1 if self.dirty else 0)
        self.btn_save.configure(
            text=f"保存到小程序（{pending}）" if pending else "保存到小程序",
            state="normal")

    def load_thumbs(self, spus):
        todo = []
        for spu in spus[:120]:
            if spu in self.thumbs:
                continue
            g = self.items.get(spu)
            if not g:
                continue
            urls = g.get("image_urls_list") or []
            url = urls[0] if urls else (g.get("image_url") or "")
            if url:
                todo.append((spu, url))
        if not todo:
            return

        def job():
            from wxds import net
            for spu, url in todo:
                raw = self.backend.image_cache.get(url)
                if raw is None:
                    try:
                        st, _, raw = net.request(net.make_opener(), url, timeout=15,
                                                 headers={"User-Agent": net.UA_BROWSER})
                        if st != 200 or not raw:
                            continue
                        self.backend.image_cache[url] = raw
                    except Exception:  # noqa: BLE001
                        continue
                self.backend.post("sort_thumb", (spu, raw))
                time.sleep(0.02)

        threading.Thread(target=job, daemon=True).start()

    def set_thumb(self, spu, raw):
        if spu not in self.items:
            return
        try:
            self.thumb_raw[spu] = raw
            self.thumb_pil[spu] = Image.open(io.BytesIO(raw)).convert("RGB")
            self.thumb_size_used.pop(spu, None)
            ph = self._ph_for(spu)
            if self.tree.exists(str(spu)):
                self.tree.item(str(spu), image=ph)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 排序 ----------
    def _selected_spu(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def move(self, delta):
        spu = self._selected_spu()
        if spu is None or self.cur_cid is None:
            return
        lst = self.order[self.cur_cid]
        idx = lst.index(spu)
        if delta == -999:
            new = 0
        elif delta == 999:
            new = len(lst) - 1
        else:
            new = max(0, min(len(lst) - 1, idx + delta))
        if new == idx:
            return
        lst.pop(idx)
        lst.insert(new, spu)
        self.dirty = True
        if delta in (-999, 999):
            # 置顶/置底：只移动行，不重绘、不滚动，保持当前浏览位置
            self.tree.move(str(spu), "",
                           0 if delta == -999 else "end")
            for i, iid in enumerate(self.tree.get_children(), 1):
                self.tree.set(iid, "idx", i)
            self.tree.selection_set(str(spu))
            self._update_save_btn()
            return
        self.render()
        self.tree.selection_set(str(spu))
        self.tree.see(str(spu))

    def apply_strategy(self):
        if self.cur_cid is None:
            return
        name = self.strategy.get()
        lst = list(self.order[self.cur_cid])
        if name == self.STRATEGIES[0]:
            return
        if name == "名称":
            lst.sort(key=lambda s: self.items[s].get("name") or "")
        elif name == "库存从多到少":
            lst.sort(key=lambda s: -(self.items[s].get("stock_num") or 0))
        elif name == "销量从高到低":
            lst.sort(key=lambda s: -(self.items[s].get("sales_count") or 0))
        elif name == "价格从低到高":
            lst.sort(key=lambda s: self.items[s].get("price") or 0)
        elif name == "条码":
            lst.sort(key=lambda s: engine.extract_barcode(self.items[s].get("desc")))
        self.order[self.cur_cid] = lst
        self.dirty = True
        self.render()
        self.backend.log(f"已按「{name}」重排当前分类，记得保存")

    # ---------- 标签 ----------
    def set_tag(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("设置标签", "先选中要改标签的商品（可多选）")
            return
        first = self.items.get(int(sel[0]))
        current = engine.TAG_CN.get(self._tag_of(first), "无") if first else "无"
        tag_cn = self._pick_tag(current)
        if not tag_cn:
            return
        tag = engine.CN_TAG[tag_cn]
        for iid in sel:
            spu = int(iid)
            g = self.items.get(spu)
            if not g:
                continue
            if (g.get("tag_type") or engine.TAG_NONE) == tag:
                self.tag_changes.pop(spu, None)
            else:
                self.tag_changes[spu] = tag
        self.render()
        self.backend.log(f"已设置 {len(sel)} 个商品的标签为「{tag_cn}」（未保存）")

    def _pick_tag(self, current):
        win = tk.Toplevel(self)
        win.title("设置标签")
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        tk.Label(win, text="选择标签", bg=CARD, fg=TEXT, font=FONT_B).pack(
            anchor="w", padx=18, pady=(14, 8))
        var = tk.StringVar(value=current)
        for cn in ("无", "招牌", "新品", "热销"):
            tk.Radiobutton(win, text=cn, variable=var, value=cn, bg=CARD,
                           font=FONT, anchor="w").pack(fill="x", padx=24)
        holder = {"v": None}

        def ok():
            holder["v"] = var.get()
            win.destroy()

        row = tk.Frame(win, bg=CARD)
        row.pack(fill="x", padx=18, pady=12)
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="确定", style="Primary.TButton", command=ok).pack(
            side="right", padx=(0, 8))
        center_window(win, self.winfo_toplevel())
        win.grab_set()
        self.wait_window(win)
        return holder["v"]

    # ---------- 分类 ----------
    def new_category(self):
        win = tk.Toplevel(self)
        win.title("新建分类")
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        tk.Label(win, text="分类名称", bg=CARD, fg=TEXT, font=FONT_B).pack(
            anchor="w", padx=18, pady=(14, 6))
        var = tk.StringVar()
        ttk.Entry(win, textvariable=var, width=28).pack(padx=18, fill="x")

        def ok():
            name = var.get().strip()
            if not name:
                messagebox.showwarning("名称不能为空", "请输入分类名称", parent=win)
                return
            win.destroy()
            self.backend.create_category(name)

        row = tk.Frame(win, bg=CARD)
        row.pack(fill="x", padx=18, pady=12)
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="创建", style="Primary.TButton", command=ok).pack(
            side="right", padx=(0, 8))
        center_window(win, self.winfo_toplevel())
        win.grab_set()
        self.wait_window(win)

    def batch_category(self):
        sel = [int(x) for x in self.tree.selection()]
        if not sel:
            messagebox.showinfo("设置分类", "先选中要改分类的商品（可多选）")
            return
        if not self.cats:
            messagebox.showinfo("设置分类", "请先拉取商品")
            return
        win = tk.Toplevel(self)
        win.title("批量设置分类")
        win.configure(bg=CARD)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        tk.Label(win, text=f"把选中的 {len(sel)} 个商品移动到：", bg=CARD,
                 fg=TEXT, font=FONT_B).pack(anchor="w", padx=18, pady=(14, 6))
        names = [c["category_name"] for c in self.cats]
        var = tk.StringVar(value=self.cat_var.get() or names[0])
        ttk.Combobox(win, textvariable=var, values=names, state="readonly",
                     width=26).pack(padx=18, fill="x")

        def ok():
            name = var.get()
            cid = next((c["category_id"] for c in self.cats
                        if c["category_name"] == name), None)
            if cid is None:
                return
            win.destroy()
            self.backend.set_category(sel, cid, name)

        row = tk.Frame(win, bg=CARD)
        row.pack(fill="x", padx=18, pady=12)
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="移动", style="Primary.TButton", command=ok).pack(
            side="right", padx=(0, 8))
        center_window(win, self.winfo_toplevel())
        win.grab_set()
        self.wait_window(win)

    def on_category_added(self, data):
        cats = data.get("cats") or []
        if cats:
            self.cats = cats
            names = [c["category_name"] for c in cats]
            self.cat_box.configure(values=names)
            idx = next((i for i, c in enumerate(cats)
                        if c["category_name"] == data.get("name")), None)
            if idx is not None:
                self.cat_box.current(idx)
                self.cur_cid = cats[idx]["category_id"]
                self.dirty = False
                self.render()
        messagebox.showinfo("新建分类", f'已新建分类「{data.get("name")}」。')

    def on_category_set(self, data):
        cid = data.get("cid")
        moved = set(data.get("spus") or [])
        for c, lst in list(self.order.items()):
            if c == cid:
                continue
            self.order[c] = [s for s in lst if s not in moved]
        if cid not in self.order:
            self.order[cid] = []
        for s in data.get("spus") or []:
            if s in self.items and s not in self.order[cid]:
                self.order[cid].append(s)
        if self.cur_cid is not None:
            self.render()
        msg = f'已把 {data.get("ok", 0)} 个商品移动到「{data.get("cname")}」'
        fails = data.get("fails") or []
        if fails:
            msg += f'\n失败 {len(fails)} 个：\n' + "\n".join(
                f"· {n}：{m}" for n, m in fails[:6])
        messagebox.showinfo("设置分类", msg)

    # ---------- 保存 ----------
    def save(self):
        if self.cur_cid is None:
            return
        ordered = self.order[self.cur_cid] if self.dirty else []
        tags = dict(self.tag_changes)
        if not ordered and not tags:
            messagebox.showinfo("没有改动", "排序和标签都没有改动")
            return
        msg = []
        if ordered:
            msg.append("保存当前分类的排序")
        if tags:
            msg.append(f"修改 {len(tags)} 个商品的标签")
        if not messagebox.askyesno(
                "确认保存", "将：\n· " + "\n· ".join(msg) +
                "\n\n排序会覆盖该分类在小程序里的顺序，确定继续？", icon="warning"):
            return
        self.btn_save.configure(state="disabled")
        self.backend.save_sort(self.cur_cid, ordered, tags)

    def on_save_done(self, data):
        self.dirty = False
        self.tag_changes.clear()
        self.render()
        parts = []
        if data.get("tags"):
            parts.append(f'标签 {data["tags"]} 个（失败 {len(data["tag_fail"])}）')
        if data.get("sort_ok"):
            parts.append("排序已保存")
        elif data.get("sort_msg"):
            parts.append("排序失败：" + data["sort_msg"])
        msg = "；".join(parts) or "没有写入"
        fails = data.get("tag_fail") or []
        if fails:
            msg += "\n\n失败明细：\n" + "\n".join(f"· {n}：{m}" for n, m in fails[:6])
        messagebox.showinfo("保存完成", msg)
        self.app.job_text.configure(text="就绪")

    def reset(self):
        self.cats = []
        self.order = {}
        self.items = {}
        self.cur_cid = None
        self.dirty = False
        self.tag_changes = {}
        self.thumbs = {}
        self.tree.delete(*self.tree.get_children())
        self.cat_box.configure(values=[])
        self.cat_var.set("")
        self.btn_save.configure(text="保存到小程序", state="disabled")


class TabBook(ttk.Frame):
    """等宽页签容器，替代 ttk.Notebook（Notebook 页签宽度会随内容变化）。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.bar = tk.Frame(self, bg=BG)
        self.bar.pack(fill="x")
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True)
        self._items = []
        self._cur = None

    def add(self, child, text=""):
        btn = ttk.Button(self.bar, text=text, style="TabBtn.TButton",
                         command=lambda c=child: self.select(c))
        btn.pack(side="left", padx=(0, 4), pady=(0, 4))
        child.place(in_=self.body, x=0, y=0, relwidth=1, relheight=1)
        self._items.append((child, btn))
        if self._cur is None:
            self.select(child)

    def select(self, child=None):
        if child is None:
            return self._cur
        self._cur = child
        child.tkraise()
        for c, b in self._items:
            b.configure(style="TabBtnSel.TButton" if c is child else "TabBtn.TButton")

    def tab(self, child, state="normal"):
        for c, b in self._items:
            if c is child:
                b.configure(state=state)

    def index(self, child):
        for i, (c, _b) in enumerate(self._items):
            if c is child:
                return i
        return -1


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        cfg0 = config.load()
        self.shop_only = (cfg0.get("app_mode") == "shop")
        title = "微信下单助手 · 小程序管理" if self.shop_only else APP_TITLE
        self.title(f"{title}  v{__version__}")
        try:
            self.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "assets", "icon.ico"))
        except Exception:  # noqa: BLE001
            pass
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1360, max(1080, sw - 60))
        h = min(860, max(700, sh - 100))
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(1080, 700)
        self.configure(bg=BG)
        self.events = queue.Queue()
        self.stop_flag = False
        self.add_win = None
        self.backend = Backend(self)
        self._style()
        self._build()
        self.after(100, self._drain)
        self.after(1500, self._probe_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        capture.kill_backends()
        if self.backend.sid:
            self.sid_var.set(self.backend.sid)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=FONT)
        style.configure("TFrame", background=BG)
        style.configure("Tab.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TButton", padding=(10, 6), relief="flat")
        style.configure("Primary.TButton", background=GREEN, foreground="white",
                        borderwidth=0, padding=(14, 7), font=FONT_B)
        style.map("Primary.TButton",
                  background=[("active", GREEN_DARK), ("disabled", "#b9c0c8")])
        style.configure("Danger.TButton", background=RED, foreground="white",
                        borderwidth=0, padding=(14, 7), font=FONT_B)
        style.map("Danger.TButton",
                  background=[("active", "#c93c40"), ("disabled", "#d9b6b7")])
        style.configure("TabBtn.TButton", padding=(26, 8), font=FONT,
                        background="#e6e8eb", foreground=SUB, borderwidth=0)
        style.map("TabBtn.TButton", background=[("active", "#dde1e5")])
        style.configure("TabBtnSel.TButton", padding=(26, 8), font=FONT_B,
                        background=CARD, foreground=GREEN, borderwidth=0)
        style.map("TabBtnSel.TButton", background=[("active", CARD)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 9), font=FONT)
        style.map("TNotebook.Tab",
                  background=[("selected", CARD), ("!selected", "#e6e8eb")],
                  foreground=[("selected", GREEN), ("!selected", SUB)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        rowheight=27, borderwidth=0, font=FONT)
        style.configure("Treeview.Heading", background="#f7f8fa", foreground=SUB,
                        font=("Microsoft YaHei UI", 8, "bold"), padding=(4, 6))
        style.configure("Add.Treeview", rowheight=44)
        style.map("Treeview", background=[("selected", "#e8f7ef")],
                  foreground=[("selected", TEXT)])
        style.configure("TEntry", padding=4)
        style.configure("TCombobox", padding=3)
        style.configure("TProgressbar", background=GREEN, troughcolor="#eef0f3",
                        borderwidth=0, thickness=6)

    def _build(self):
        header = tk.Frame(self, bg=CARD, height=100)
        header.pack(fill="x")
        header.pack_propagate(False)

        row1 = tk.Frame(header, bg=CARD)
        row1.pack(fill="x")
        tk.Label(row1, text="微信下单助手小程序管理", bg=CARD, fg=TEXT, font=FONT_T).pack(
            side="left", padx=(18, 10))
        tk.Label(row1, text=f"v{__version__}", bg=CARD, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).pack(side="left", pady=(12, 0))
        self.status = tk.Label(row1, text="● 未检测", bg=CARD, fg=SUB, font=FONT)
        self.status.pack(side="left", padx=(12, 0), pady=(10, 0))
        self.sid_state = tk.Label(row1, text="● SID 未检测", bg=CARD, fg=SUB,
                                  font=FONT, cursor="hand2")
        self.sid_state.pack(side="left", padx=(14, 0), pady=(10, 0))
        self.sid_state.bind("<Button-1>", lambda _e: self._probe_sid())

        row2 = tk.Frame(header, bg=CARD)
        row2.pack(fill="x", padx=18, pady=(4, 0))
        tk.Label(row2, text="店铺", bg=CARD, fg=SUB, font=FONT).pack(side="left")
        self.store_var = tk.StringVar()
        self.store_box = ttk.Combobox(row2, textvariable=self.store_var,
                                      state="readonly", width=14)
        self.store_box.pack(side="left")
        self.store_box.bind("<<ComboboxSelected>>", self.on_store_change)
        ttk.Button(row2, text="＋", width=3, command=self.add_store).pack(
            side="left", padx=(4, 0))
        ttk.Button(row2, text="设置", command=self.edit_store).pack(
            side="left", padx=(4, 0))

        self.btn_check = ttk.Button(row2, text="检测并保存", style="Primary.TButton",
                                    command=self.on_check_sid)
        self.btn_check.pack(side="right")
        self.btn_capture = ttk.Button(row2, text="抓取 SID", command=self.on_capture)
        self.btn_capture.pack(side="right", padx=(6, 0))
        self.sid_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.sid_var, width=28).pack(
            side="right", padx=(0, 2))
        tk.Label(row2, text="SID", bg=CARD, fg=SUB, font=FONT).pack(
            side="right", padx=(10, 4))

        self.nb = TabBook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(8, 6))
        self.stock_tab = StockTab(self.nb.body, self)
        self.add_tab = AddTab(self.nb.body, self)
        self.sort_tab = SortTab(self.nb.body, self)
        if self.shop_only:
            self.nb.add(self.sort_tab, text="排序标签")
        else:
            self.nb.add(self.stock_tab, text="库存同步")
            self.nb.add(self.add_tab, text="上架商品")
            self.nb.add(self.sort_tab, text="排序标签")
        if self.backend.stores:
            self._refresh_store_box(self.backend.store_idx)
        self.apply_store_mode()

        footer = tk.Frame(self, bg=CARD, height=46)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.progress = ttk.Progressbar(footer, length=260, mode="determinate",
                                        style="TProgressbar")
        self.progress.pack(side="left", padx=(16, 10), pady=14)
        self.job_text = tk.Label(footer, text="就绪", bg=CARD, fg=SUB, font=FONT)
        self.job_text.pack(side="left")
        self.btn_stop = ttk.Button(footer, text="停止", command=self._stop)
        self.btn_stop.pack(side="right", padx=14)
        ttk.Button(footer, text="日志", command=self._toggle_log).pack(side="right")

        self.log_frame = tk.Frame(self, bg="#14161a", height=150)
        self.log_text = tk.Text(self.log_frame, bg="#14161a", fg="#b8f0c6",
                                font=("Consolas", 9), relief="flat", height=8,
                                insertbackground="#b8f0c6")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_visible = False

    # ---------- 事件循环 ----------
    def _drain(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _handle(self, kind, data):
        if kind == "log":
            self._append_log(data)
        elif kind == "progress":
            done, total, text = data
            if total:
                self.progress.configure(maximum=total, value=done)
                self.job_text.configure(text=f"{text} {done}/{total}")
                if getattr(self, "add_win", None):
                    try:
                        self.add_bar.configure(maximum=total, value=done)
                        self.add_lbl.configure(text=text)
                    except tk.TclError:
                        pass
            else:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
                self.job_text.configure(text=text or "处理中…")
        elif kind == "add_start":
            self._open_add_progress(data)
        elif kind == "sort_progress":
            done, total, text = data
            self.progress.stop()
            self.progress.configure(mode="determinate",
                                    maximum=max(int(total or 0), 1),
                                    value=int(done or 0))
            self.job_text.configure(text=text)
            self.sort_tab.set_loading(True, text)
        elif kind == "sort_data":
            self.sort_tab.set_loading(False)
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.job_text.configure(text="就绪")
            self.sort_tab.set_data(*data)
        elif kind == "sort_done":
            self.sort_tab.on_save_done(data)
        elif kind == "category_added":
            self.job_text.configure(text="就绪")
            self.sort_tab.on_category_added(data)
        elif kind == "category_set":
            self.job_text.configure(text="就绪")
            self.sort_tab.on_category_set(data)
        elif kind == "sort_thumb":
            self.sort_tab.set_thumb(*data)
        elif kind == "store_changed":
            self.on_store_changed(data)
        elif kind == "busy":
            if not data:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
                self.job_text.configure(text="就绪")
                self.btn_stop.configure(state="disabled")
            else:
                self.btn_stop.configure(state="normal")
        elif kind == "sid_probe":
            if data.get("sid") and data["sid"] != self.backend.sid:
                return
            if data.get("ok"):
                label = "店员" if data.get("kind") == "clerk" else "店主"
                self.sid_state.configure(
                    text=f"● SID 有效 · {label} · 尾号 {data['sid'][-6:]}", fg=GREEN)
            elif data.get("kind") == "none":
                self.sid_state.configure(text="● SID 未设置", fg=RED)
            else:
                self.sid_state.configure(text="● SID 未通过检测", fg=RED)
        elif kind == "sid":
            ok, msg = data
            self.btn_check.configure(state="normal", text="检测并保存")
            if ok:
                self.status.configure(
                    text=f"● 有效 · 店主 · 尾号 {self.backend.sid[-6:]}", fg=GREEN)
                self.sid_state.configure(
                    text=f"● SID 有效 · 店主 · 尾号 {self.backend.sid[-6:]}", fg=GREEN)
                messagebox.showinfo("检测结果", "SID 有效（店主店），已保存。")
            else:
                self.status.configure(text="● SID 无效", fg=RED)
                messagebox.showerror(
                    "检测结果", msg + "\n\n请点「抓取 SID」重新抓取。")
        elif kind == "capture":
            if data == "waiting":
                self.status.configure(
                    text="● 抓取中：请打开微信 经营账本→商品管理 或 经营码店员助手", fg=AMBER)
            elif data == "watching":
                self.status.configure(
                    text="● 记录中：请打开店员助手的商品页操作一下", fg=AMBER)
            elif data == "fallback":
                self.status.configure(
                    text="● 已打开抓取工具：点「开始抓取」，再打开微信 经营账本 或 经营码店员助手",
                    fg=AMBER)
            else:
                self.status.configure(text="● 正在启动抓包…", fg=AMBER)
            self.btn_capture.configure(text="取消抓取")
        elif kind == "capture_seen":
            self.status.configure(text=f"● 抓取中 · 已看到 {data} 条微信请求", fg=AMBER)
        elif kind == "captured":
            self.sid_var.set(data)
            self._copy_sid(data)
            self.status.configure(
                text=f"● 有效 · 店主 · 尾号 {data[-6:]}", fg=GREEN)
            self.btn_capture.configure(text="抓取 SID")
            messagebox.showinfo("抓取成功",
                                "SID 已抓到并保存（已复制到剪贴板）。")
        elif kind == "clerk_captured":
            self.btn_capture.configure(text="抓取 SID")
            self.btn_check.configure(state="normal", text="检测并保存")
            self._copy_sid(data.get("sid") or "")
            self.status.configure(text="● 已抓到店员 SID，请命名店铺", fg=GREEN)
            self.on_clerk_captured(data)
        elif kind == "capture_fail":
            self.btn_capture.configure(text="抓取 SID")
            self.status.configure(text="● 抓取失败", fg=RED)
            messagebox.showerror(
                "抓取失败",
                data + "\n\n也可以直接双击 tools\\sid\\SID抓取后端.exe 用原工具抓取，"
                       "再把 SID 粘贴到输入框。")
        elif kind == "capture_done":
            self.btn_capture.configure(text="抓取 SID")
            if self.backend.sid:
                self.sid_var.set(self.backend.sid)
            if not self.backend.capture_found:
                self.status.configure(text="● 抓取结束：没有抓到有效 SID", fg=RED)
        elif kind == "watch_done":
            seen = data.get("seen", 0)
            path = data.get("path", "")
            for ln in data.get("tail") or []:
                self.log("  " + ln)
            self.status.configure(text=f"● 记录完成 · {seen} 条请求", fg=GREEN)
            messagebox.showinfo(
                "记录完成",
                f"共记录 {seen} 条微信请求。\n记录文件：\n{path}\n\n"
                "直接回我一声，我在这台机器上读这个文件。")
        elif kind == "stock_plan":
            self.stock_tab.set_plan(data)
        elif kind == "apply_done":
            self.stock_tab.refresh_changed()
            messagebox.showinfo(
                "同步完成",
                f'成功 {data["ok"]}，失败 {data["fail"]}。\n\n备份：\n{data["backup"]}')
        elif kind == "candidates":
            self.add_tab.set_candidates(data)
            self.nb.select(self.add_tab)
        elif kind == "add_done":
            self._close_add_progress()
            self.add_tab.remove_added(data["done"])
            msg = f'成功 {data["ok"]}，失败 {data["fail"]}。'
            fails = data.get("fails") or []
            if fails:
                msg += "\n\n失败明细：\n" + "\n".join(
                    f"· {n}：{m}" for n, m in fails[:8])
            messagebox.showinfo("上架完成", msg)
        elif kind == "preview":
            bc, raw = data
            if self.add_tab.cands.get(bc) and self.add_tab.tree.selection() == (bc,):
                self.add_tab._show_image(raw)
        elif kind == "thumb":
            bc, raw = data
            self.add_tab.set_thumb(bc, raw)
        elif kind == "edit_image":
            self.stock_tab.show_edit_image(*data)
        elif kind == "edit_image_uploaded":
            self.stock_tab.edit_upload_done(*data)
        elif kind == "edit_image_fail":
            self.stock_tab.edit_upload_fail(*data)
        elif kind == "preview_fail":
            bc, msg = data
            if self.add_tab.tree.selection() == (bc,):
                self.add_tab.preview.configure(image="", text=f"图片加载失败\n{msg}")
        elif kind == "error":
            self.status.configure(text="● 出错", fg=RED)
            self.sort_tab.set_loading(False)
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            messagebox.showerror("出错了", data)
        elif kind == "backup":
            self.last_backup = data

    def _append_log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
        self.log_text.insert("end", line)
        if int(self.log_text.index("end-1c").split(".")[0]) > 600:
            self.log_text.delete("1.0", "200.0")
        self.log_text.see("end")

    def _toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.pack(fill="x", side="bottom")
        else:
            self.log_frame.pack_forget()

    # ---------- 店铺 ----------
    def on_store_change(self, _event=None):
        idx = self.store_box.current()
        if idx < 0:
            return
        self.backend.use_store(idx)

    def on_store_changed(self, st):
        self.stock_tab.reset()
        self.add_tab.reset()
        self.sort_tab.reset()
        self.sid_var.set(self.backend.sid or "")
        self.store_var.set(st.get("name") or "")
        self.apply_store_mode()
        self.status.configure(
            text="● 已切换店铺" if self.backend.sid else "● 未设置 SID", fg=SUB)
        self.after(400, self._probe_sid)

    def apply_store_mode(self):
        if self.shop_only:
            return
        st = (self.backend.stores[self.backend.store_idx]
              if self.backend.stores else {"hg_sync": True})
        hg = bool(st.get("hg_sync", True))
        state = "normal" if hg else "disabled"
        self.nb.tab(self.stock_tab, state=state)
        self.nb.tab(self.add_tab, state=state)
        if not hg:
            cur = self.nb.select()
            if not cur or cur in (self.stock_tab, self.add_tab):
                self.nb.select(self.sort_tab)

    def _refresh_store_box(self, idx):
        names = [s.get("name") or "未命名" for s in self.backend.stores]
        self.store_box.configure(values=names)
        if 0 <= idx < len(names):
            self.store_box.current(idx)
            self.store_var.set(names[idx])

    def add_store(self):
        data = self._store_dialog("添加店铺")
        if not data:
            return
        self.backend.stores.append(data)
        config.save_stores(self.backend.stores)
        idx = len(self.backend.stores) - 1
        self._refresh_store_box(idx)
        self.backend.use_store(idx)

    def edit_store(self):
        if not self.backend.stores:
            return
        cur = self.backend.stores[self.backend.store_idx]
        data = self._store_dialog("店铺设置", cur, allow_delete=True)
        if not data:
            return
        if data.get("__delete__"):
            self.delete_current_store()
            return
        cur.update(data)
        config.save_stores(self.backend.stores)
        self.backend.sid = (data.get("sid") or "").strip()
        if self.backend.sid:
            try:
                config.write_sid(self.backend.sid)
            except OSError:
                pass
        self.sid_var.set(self.backend.sid or "")
        self._refresh_store_box(self.backend.store_idx)
        self.apply_store_mode()

    def delete_current_store(self):
        if len(self.backend.stores) <= 1:
            messagebox.showinfo("不能删除", "至少要保留一个店铺")
            return
        idx = self.backend.store_idx
        name = self.backend.stores[idx].get("name") or "未命名"
        if not messagebox.askyesno(
                "删除店铺", f"确定删除店铺「{name}」？\n只删除本机配置，不影响小程序里的数据。",
                icon="warning"):
            return
        del self.backend.stores[idx]
        config.save_stores(self.backend.stores)
        new_idx = min(idx, len(self.backend.stores) - 1)
        self._refresh_store_box(new_idx)
        self.backend.use_store(new_idx)
        self.backend.log(f"已删除店铺：{name}")

    def _store_dialog(self, title, init=None, allow_delete=False):
        init = init or {}
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)
        frame = tk.Frame(win, bg=CARD)
        frame.pack(padx=18, pady=14)
        tk.Label(frame, text="店铺名称", bg=CARD, fg=SUB, font=FONT).grid(
            row=0, column=0, sticky="w", pady=4)
        name_var = tk.StringVar(value=init.get("name") or "")
        ttk.Entry(frame, textvariable=name_var, width=30).grid(
            row=0, column=1, sticky="ew", pady=4)
        tk.Label(frame, text="SID", bg=CARD, fg=SUB, font=FONT).grid(
            row=1, column=0, sticky="w", pady=4)
        sid_var = tk.StringVar(value=init.get("sid") or "")
        ttk.Entry(frame, textvariable=sid_var, width=32).grid(
            row=1, column=1, sticky="ew", pady=4)
        hg_var = tk.BooleanVar(value=bool(init.get("hg_sync", True)))
        tk.Checkbutton(frame, text="这个店需要从惠管家同步库存（不勾=只做排序/标签）",
                       variable=hg_var, bg=CARD, font=FONT, anchor="w").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        holder = {"v": None}

        def ok():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("名称不能为空", "请填写店铺名称", parent=win)
                return
            holder["v"] = {"name": name, "sid": sid_var.get().strip(),
                           "hg_sync": bool(hg_var.get())}
            win.destroy()

        def do_delete():
            if messagebox.askyesno(
                    "删除店铺", f"确定删除「{init.get('name') or ''}」？\n"
                    "只删除本机配置，不影响小程序里的数据。",
                    parent=win, icon="warning"):
                holder["v"] = {"__delete__": True}
                win.destroy()

        row = tk.Frame(frame, bg=CARD)
        row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        if allow_delete:
            ttk.Button(row, text="删除店铺", style="Danger.TButton",
                       command=do_delete).pack(side="left")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="确定", style="Primary.TButton", command=ok).pack(
            side="right", padx=(0, 8))
        center_window(win, self)
        win.grab_set()
        self.wait_window(win)
        return holder["v"]

    def on_clerk_captured(self, info):
        shop = str(info.get("shop_id") or "")
        mch = str(info.get("mch_id") or "")
        for i, s in enumerate(self.backend.stores):
            if shop and str(s.get("shop_id") or "") == shop:
                s.update({"sid": info["sid"], "auth_type": 4,
                          "v": info.get("v") or s.get("v") or "0.11.35"})
                if mch:
                    s["mch_id"] = mch
                self.backend._dedupe_stores()
                config.save_stores(self.backend.stores)
                idx = next((j for j, t in enumerate(self.backend.stores)
                            if str(t.get("shop_id") or "") == shop), 0)
                self._refresh_store_box(idx)
                self.backend.use_store(idx)
                self.status.configure(
                    text=f"● 有效 · 店员 · 尾号 {self.backend.sid[-6:]}", fg=GREEN)
                self.sid_state.configure(
                    text=f"● SID 有效 · 店员 · 尾号 {self.backend.sid[-6:]}", fg=GREEN)
                messagebox.showinfo(
                    "SID 已更新", "店员 SID 已保存到这个店铺，并已复制到剪贴板。")
                return
        name = self._clerk_dialog(info, shop, mch)
        if not name:
            self.status.configure(text="● 已抓到店员 SID，但未保存", fg=AMBER)
            return
        self.backend.stores.append({
            "name": name, "sid": info["sid"],
            "hg_sync": bool(getattr(self, "_clerk_hg", False)),
            "auth_type": 4, "v": info.get("v") or "0.11.35",
            "mch_id": mch, "shop_id": shop})
        config.save_stores(self.backend.stores)
        idx = len(self.backend.stores) - 1
        self._refresh_store_box(idx)
        self.backend.use_store(idx)
        self.status.configure(
            text=f"● 有效 · 店员 · 尾号 {self.backend.sid[-6:]}", fg=GREEN)
        self.sid_state.configure(
            text=f"● SID 有效 · 店员 · 尾号 {self.backend.sid[-6:]}", fg=GREEN)
        messagebox.showinfo(
            "店铺已添加", f"已新建店铺「{name}」。\n\nSID 已保存并复制到剪贴板。")

    def _clerk_dialog(self, info, shop, mch):
        win = tk.Toplevel(self)
        win.title("抓到店员 SID")
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)
        frame = tk.Frame(win, bg=CARD)
        frame.pack(padx=18, pady=14)
        tk.Label(frame, text="抓到「经营码店员助手」的 SID", bg=CARD, fg=TEXT,
                 font=FONT_B).pack(anchor="w")
        tk.Label(frame, text=f"shop_id: {shop or '-'}    mch_id: {mch or '-'}",
                 bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).pack(
            anchor="w", pady=(2, 10))
        tk.Label(frame, text="店铺名称", bg=CARD, fg=SUB, font=FONT).pack(
            anchor="w", pady=(0, 2))
        name_var = tk.StringVar(value=("店员店 " + shop[-6:]) if shop else "店员店")
        ttk.Entry(frame, textvariable=name_var, width=30).pack(fill="x")
        hg_var = tk.BooleanVar(value=False)
        tk.Checkbutton(frame, text="这个店也需要从惠管家同步库存",
                       variable=hg_var, bg=CARD, font=FONT, anchor="w").pack(
            anchor="w", pady=(8, 0))
        holder = {"v": None}

        def ok():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("名称不能为空", "请填写店铺名称", parent=win)
                return
            holder["v"] = name
            win.destroy()

        row = tk.Frame(frame, bg=CARD)
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(row, text="保存为新店铺", style="Primary.TButton", command=ok).pack(
            side="right", padx=(0, 8))
        center_window(win, self)
        win.grab_set()
        self.wait_window(win)
        if holder["v"]:
            self._clerk_hg = bool(hg_var.get())
        return holder["v"]

    def _probe_sid(self):
        if self.backend.busy or self.backend.capturing:
            return
        if not self.backend.sid:
            self.sid_state.configure(text="● SID 未设置", fg=RED)
            return
        if "有效" not in self.sid_state.cget("text"):
            self.sid_state.configure(text="● SID 检测中…", fg=AMBER)
        self.backend.probe_sid()

    def _probe_tick(self):
        self._probe_sid()
        self.after(120000, self._probe_tick)

    def on_check_sid(self):
        sid = self.sid_var.get().strip()
        if not sid:
            messagebox.showwarning("没有 SID", "请先粘贴或抓取一个 SID。")
            return
        self.btn_check.configure(state="disabled", text="检测中…")
        self.status.configure(text="● 检测中…", fg=AMBER)
        self.backend.check_sid(sid)

    def on_watch(self):
        if not messagebox.askyesno(
                "记录请求", "会记录 150 秒微信请求（不会抓 SID、不会改任何数据）。\n"
                "开始后请在电脑微信里打开店员助手的商品页操作一下。\n\n继续？"):
            return
        if self.backend.start_watch(150):
            self.status.configure(text="● 记录中…", fg=AMBER)

    def on_capture(self):
        if self.backend.capturing:
            self.backend.stop_capture()
            return
        if not self.backend.capture_ready():
            messagebox.showerror("缺少组件",
                                 "找不到 tools\\sid\\SID抓取后端.exe，抓取功能不可用。")
            return
        if not messagebox.askyesno(
                "抓取 SID",
                "抓取时会临时把系统代理指向本机，抓到后自动还原。\n"
                "如果异常退出导致上不了网，去 Windows 设置 → 网络 → 代理 关掉即可。\n\n"
                "开始后请在电脑微信打开「经营账本 → 商品管理」或「经营码店员助手」刷新一下。\n\n继续？",
                icon="warning"):
            return
        self.backend.start_capture()
        self.btn_capture.configure(text="停止抓取")
        self.backend.log("开始抓取（最长 180 秒），请打开店员助手的商品页刷新…")

    def _copy_sid(self, sid):
        try:
            self.clipboard_clear()
            self.clipboard_append(sid)
        except tk.TclError:
            pass

    def _open_add_progress(self, total):
        if getattr(self, "add_win", None):
            return
        win = tk.Toplevel(self)
        win.title("正在上架")
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)
        self.add_win = win
        tk.Label(win, text=f"正在上架 {total} 个商品", bg=CARD, fg=TEXT,
                 font=FONT_B).pack(anchor="w", padx=20, pady=(18, 6))
        self.add_lbl = tk.Label(win, text="准备中…", bg=CARD, fg=SUB, font=FONT,
                                anchor="w", width=44)
        self.add_lbl.pack(anchor="w", padx=20)
        self.add_bar = ttk.Progressbar(win, maximum=max(total, 1), length=380)
        self.add_bar.pack(padx=20, pady=(8, 10))
        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", padx=20, pady=(0, 14))
        ttk.Button(bar, text="取消剩余", command=self._cancel_add).pack(side="right")
        center_window(win, self)
        win.protocol("WM_DELETE_WINDOW", self._cancel_add)

    def _cancel_add(self):
        self._stop()
        self._close_add_progress()

    def _close_add_progress(self):
        w = getattr(self, "add_win", None)
        if w:
            try:
                w.grab_release()
                w.destroy()
            except tk.TclError:
                pass
        self.add_win = None

    def _stop(self):
        self.stop_flag = True
        self.job_text.configure(text="正在停止…")

    def _on_close(self):
        if self.backend.busy and not messagebox.askyesno(
                "任务进行中", "还有任务在运行，确定退出？"):
            return
        if self.backend.capturing:
            self.backend.capture_stop = True
            cap = self.backend.capture_obj
            if cap is not None:
                try:
                    cap.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.backend.capturing = False
            self.backend.capture_obj = None
        self._close_add_progress()
        capture.kill_backends()
        self.destroy()


def _demo(app):
    """开发自检：用离线快照填满界面并截图（不联网、不写数据）。"""
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        goods = json.load(open(os.path.join(root, "out", "dnzs_goods.json"),
                               encoding="utf-8"))["goods"]
        hg = json.load(open(os.path.join(root, "out", "hg_stock.json"),
                            encoding="utf-8"))["rows"]
        plan = engine.build_plan(goods, hg, app.backend.cfg["policies"])
        app.stock_tab.set_plan(plan)
        app.backend.plan = plan
        fake = [
            {"goods_id": 1, "name": "空礼盒", "barcode": "8260921000011",
             "hg_category": "礼盒", "unit": "无", "qty": 55, "price_yuan": 5.0,
             "weight": False, "photo": "", "price_fen": 500, "category_id": 0,
             "name_edit": "空礼盒", "image_url": ""},
            {"goods_id": 2, "name": "李好辣香辣小鸭爪50g", "barcode": "6972751030565",
             "hg_category": "零食", "unit": "包", "qty": 10, "price_yuan": 8.8,
             "weight": False, "photo": "file:" + os.path.join(root, "out", "cand_sample.jpg"),
             "price_fen": 880,
             "category_id": 0, "name_edit": "李好辣香辣小鸭爪50g", "image_url": ""},
            {"goods_id": 3, "name": "山姆高钙鲜牛奶1.89L", "barcode": "6907992107219",
             "hg_category": "饮料", "unit": "瓶", "qty": 2, "price_yuan": 24.0,
             "weight": False, "photo": "file:" + os.path.join(root, "out", "cand2.jpg"),
             "price_fen": 2400,
             "category_id": 0, "name_edit": "山姆高钙鲜牛奶1.89L", "image_url": ""},
        ]
        app.backend.categories = [
            {"category_id": 306023230, "category_name": "快乐小饮料"},
            {"category_id": 306028349, "category_name": "山姆会员店"},
        ]
        app.add_tab.set_candidates(fake)
    except Exception as e:  # noqa: BLE001
        print("demo error", e)

    def shoot(path):
        app.update()
        try:
            from PIL import ImageGrab
            x, y = app.winfo_rootx(), app.winfo_rooty()
            w, h = app.winfo_width(), app.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
            print("saved", path)
        except Exception as e:  # noqa: BLE001
            print("shot error", e)

    app.attributes("-topmost", True)
    app.sid_var.set("")
    app.after(1200, lambda: shoot(os.path.join(root, "out", "gui_stock.png")))
    app.after(2200, lambda: app.nb.select(app.add_tab))
    app.after(2400, lambda: _select_first(app))
    app.after(3200, lambda: shoot(os.path.join(root, "out", "gui_add.png")))
    app.after(4000, app.destroy)


def _select_first(app):
    rows = app.add_tab.tree.get_children()
    if rows:
        app.add_tab.tree.selection_set(rows[1] if len(rows) > 1 else rows[0])
        app.add_tab.tree.focus(rows[1] if len(rows) > 1 else rows[0])
        app.add_tab.on_select()


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("wxds.gui")
        except Exception:  # noqa: BLE001
            pass
    app = App()
    if "--selftest" in sys.argv:
        app.withdraw()
        app.update()
        print("gui ok", __version__)
        app.destroy()
        return
    if "--demo" in sys.argv:
        app.after(500, lambda: _demo(app))
    app.mainloop()


if __name__ == "__main__":
    main()






