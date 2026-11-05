# -*- coding: utf-8 -*-
"""配置与路径。配置写在程序目录的 config.json，数据写在 data/。"""
import copy
import json
import os
import sys

DEFAULTS = {
    "hg": {
        "base": "https://portal.smartpos.top",
        "username": "",
        "password": "",
        "branch_id": "",
        "partition": "za5",
    },
    "dxzs": {
        "base": "https://payapp.weixin.qq.com",
        "v": "1.33.9",
        "auth_type": 3,
    },
    "sid_file": "sid.txt",
    "policies": {
        "off_shelf_policy": "keep",
        "weight_keywords": ["kg", "千克", "公斤", "斤", "克", "g"],
        "max_stock_delta_warn": 500,
        "plan_ttl_minutes": 60,
    },
    "rate": {
        "write_delay_ms": 1200,
        "retry": 3,
        "retry_delay_ms": 2000,
    },
    "server": {
        "port": 8899,
        "auto_open": True,
    },
    "payload_mode": "full",
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    override = os.environ.get("WXDS_DATA_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    d = os.path.join(app_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path():
    return os.path.join(app_dir(), "config.json")


def load():
    path = config_path()
    raw = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    return _merge(DEFAULTS, raw)


def save(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def sid_path(cfg=None):
    cfg = cfg or load()
    p = cfg.get("sid_file") or "sid.txt"
    if not os.path.isabs(p):
        p = os.path.join(data_dir(), p)
    return p


def read_sid(cfg=None):
    cfg = cfg or load()
    p = sid_path(cfg)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def write_sid(sid, cfg=None):
    cfg = cfg or load()
    p = sid_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(sid.strip())
    os.replace(tmp, p)


DEFAULT_STORE_NAME = "微信下单助手美食集合店"

_MOJIBAKE_MARKS = ("鏂", "缇", "椋", "鍚", "搴", "锛", "銆", "锟")


def repair_text(s):
    """修复“UTF-8 被当成 GBK 读”造成的乱码店铺名。"""
    if not isinstance(s, str) or not s:
        return s
    if not any(m in s for m in _MOJIBAKE_MARKS):
        return s
    try:
        return s.encode("gbk").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        t = "".join(ch for ch in s if not (0xE000 <= ord(ch) <= 0xF8FF))
        try:
            return t.encode("gbk").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s


def stores_path():
    return os.path.join(data_dir(), "stores.json")


def load_stores(cfg=None):
    cfg = cfg or load()
    p = stores_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
            stores = data.get("stores") if isinstance(data, dict) else data
            if isinstance(stores, list) and stores:
                changed = False
                for s in stores:
                    if isinstance(s, dict):
                        fixed = repair_text(s.get("name") or "")
                        if fixed != s.get("name"):
                            s["name"] = fixed
                            changed = True
                if changed:
                    save_stores(stores)
                return stores
        except (OSError, ValueError):
            pass
    stores = [{"name": DEFAULT_STORE_NAME, "sid": read_sid(cfg), "hg_sync": True}]
    save_stores(stores)
    return stores


def read_last_store():
    try:
        with open(stores_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return int(data.get("last_store") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def save_stores(stores, last_store=None):
    p = stores_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if last_store is None:
        last_store = read_last_store()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stores": stores, "last_store": int(last_store)}, f,
                  ensure_ascii=False, indent=1)
    os.replace(tmp, p)
