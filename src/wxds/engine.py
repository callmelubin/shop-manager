# -*- coding: utf-8 -*-
"""匹配、算差异、生成写回载荷。纯函数，方便单独测试。"""
import copy
import re
import time
from decimal import Decimal, InvalidOperation

NORMAL = "EN_GOODS_STATE_NORMAL"
SOLD_OUT = "EN_GOODS_STATE_SOLD_OUT"
OFF_SHELF = "EN_GOODS_STATE_OUT_OFF_SHELF"

STATE_CN = {NORMAL: "在售", SOLD_OUT: "售罄", OFF_SHELF: "下架"}

ACTION_CN = {"sell_out": "售罄", "restock": "恢复", "update": "改库存",
             "skip_off_shelf": "跳过", "manual": "人工", "no_barcode": "无条码",
             "hg_missing": "惠管家缺", "noop": "一致"}

TAG_NONE = "EN_GOODS_TAG_NONE"
TAG_SPECIALTY = "EN_GOODS_TAG_SPECIALTY"
TAG_NEW = "EN_GOODS_TAG_NEW"
TAG_HOT = "EN_GOODS_TAG_HOT"
TAG_CN = {TAG_NONE: "无", TAG_SPECIALTY: "招牌", TAG_NEW: "新品", TAG_HOT: "热销"}
CN_TAG = {v: k for k, v in TAG_CN.items()}

MIN_KEYS = (
    "spu_id", "sku_id", "name", "goods_state", "image_urls_list", "desc", "price",
    "discount_price_one_order_limit", "prop_item_list", "sku_item_list", "tag_type",
    "is_support_zero_price", "image_url", "category_id", "is_required_goods",
    "is_modify_prop", "is_modify_spec",
)


def norm_barcode(s):
    s = re.sub(r"\s+", "", str(s or ""))
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def extract_barcode(desc):
    """从商品描述里取【wxds-条码】，大小写/空格容错。"""
    m = re.search(r"【\s*wxds\s*-\s*(\d+)\s*】", str(desc or ""), re.I)
    return m.group(1) if m else ""


def is_weight(unit, keywords):
    u = str(unit or "").strip().lower()
    return bool(u) and any(k.lower() in u for k in keywords)


def _qty(row):
    try:
        return Decimal(str(row.get("quantity") or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _state_of(g):
    return g.get("goods_state") or NORMAL


def build_plan(goods, hg_rows, policies):
    kws = policies.get("weight_keywords") or ["kg", "千克", "公斤", "斤", "克", "g"]
    off_policy = policies.get("off_shelf_policy") or (
        "all" if policies.get("touch_off_shelf") else "keep")
    warn_delta = int(policies.get("max_stock_delta_warn") or 500)

    exact, normalized, dups = {}, {}, []
    for r in hg_rows:
        bc = str(r.get("barCode") or "").strip()
        if not bc:
            continue
        if bc in exact:
            dups.append({"barcode": bc, "a": exact[bc].get("goodsName"),
                         "b": r.get("goodsName")})
        exact[bc] = r
        normalized.setdefault(norm_barcode(bc), r)

    items, used_bc = [], set()
    for g in goods:
        bc = extract_barcode(g.get("desc"))
        item = {
            "spu": g.get("spu_id"),
            "name": g.get("name") or "",
            "category": g.get("_category_name") or "",
            "barcode": bc,
            "hg_barcode": "",
            "unit": "",
            "weight": False,
            "hg_qty": None,
            "cur_stock": g.get("stock_num"),
            "cur_state": _state_of(g),
            "target_stock": None,
            "target_state": None,
            "action": "noop",
            "status": "",
            "changed": False,
            "price_warn": None,
            "goods": g,
        }
        items.append(item)
        if not bc:
            item["action"] = "no_barcode"
            item["status"] = "描述里没有条码"
            continue
        used_bc.add(norm_barcode(bc))
        row = exact.get(bc) or normalized.get(norm_barcode(bc))
        if row is None:
            item["action"] = "hg_missing"
            item["status"] = "惠管家没有这个条码"
            continue

        q = _qty(row)
        unit = row.get("unitName") or row.get("goodsUnitName") or ""
        weight = is_weight(unit, kws)
        cur_stock = _int_or_none(g.get("stock_num"))
        cur_state = _state_of(g)
        item.update({
            "hg_barcode": row.get("barCode") or "",
            "unit": unit, "weight": weight, "hg_qty": float(q),
            "cur_stock": cur_stock,
        })

        hg_price = row.get("salePrice")
        cur_price = g.get("price")
        if hg_price not in (None, "") and cur_price is not None:
            try:
                if abs(float(hg_price) * 100 - float(cur_price)) >= 1:
                    item["price_warn"] = {"hg": float(hg_price),
                                          "cur": round(float(cur_price) / 100, 2)}
            except (TypeError, ValueError):
                pass

        if weight:
            if q > 0:
                target_stock, target_state = None, NORMAL
            else:
                target_stock, target_state = 0, SOLD_OUT
        else:
            if q > 0:
                n = int(q)
                if n <= 0:
                    item["action"] = "manual"
                    item["status"] = f"惠管家库存 {q} 不足 1，需人工"
                    continue
                target_stock, target_state = n, NORMAL
            else:
                target_stock, target_state = 0, SOLD_OUT
        item["target_stock"] = target_stock
        item["target_state"] = target_state

        skus = g.get("sku_item_list") or []
        if len(skus) > 1:
            item["action"] = "manual"
            item["status"] = f"多规格（{len(skus)} 个 SKU），不自动改"
            continue

        if cur_state == OFF_SHELF:
            restore_ok = (off_policy == "all" or
                          (off_policy == "restore_in_stock"
                           and (target_stock is None or target_stock > 0)))
            if not restore_ok:
                item["action"] = "skip_off_shelf"
                item["status"] = "已手工下架，保持不动"
                continue

        if weight:
            changed = (cur_state != target_state
                       or (target_stock is None and g.get("stock_num") not in (None, ""))
                       or (target_stock == 0 and cur_stock != 0))
        elif target_stock == 0:
            changed = (cur_state != SOLD_OUT) or (cur_stock != 0)
        else:
            changed = (cur_state != NORMAL) or (cur_stock != target_stock)

        if not changed:
            item["status"] = "已一致"
            item["action"] = "noop"
            continue

        if cur_state == SOLD_OUT and target_state == NORMAL:
            item["action"] = "restock"
        elif target_state == SOLD_OUT:
            item["action"] = "sell_out"
        else:
            item["action"] = "update"
        item["changed"] = True

        if target_stock is None:
            item["status"] = f"在售 · 不限库存（惠管家 {q:g}{unit}）"
        else:
            item["status"] = f"库存 {cur_stock if cur_stock is not None else '空'} → {target_stock}"
        if target_state == SOLD_OUT:
            item["status"] += " · 售罄"
        if target_stock and cur_stock is not None and abs(target_stock - cur_stock) >= warn_delta:
            item["status"] += " ⚠ 变动过大"

    hg_only = []
    for r in hg_rows:
        bc = str(r.get("barCode") or "").strip()
        if not bc or norm_barcode(bc) in used_bc:
            continue
        hg_only.append({
            "name": r.get("goodsName") or "",
            "barcode": bc,
            "qty": float(_qty(r)),
            "unit": r.get("unitName") or "",
            "price": r.get("salePrice"),
        })

    summary = {
        "mini_total": len(goods),
        "hg_total": len(hg_rows),
        "matched": sum(1 for i in items if i["action"] not in ("no_barcode", "hg_missing")),
        "changed": sum(1 for i in items if i["changed"]),
        "sell_out": sum(1 for i in items if i["action"] == "sell_out"),
        "restock": sum(1 for i in items if i["action"] == "restock"),
        "update": sum(1 for i in items if i["action"] == "update"),
        "noop": sum(1 for i in items if i["action"] == "noop"),
        "skip_off_shelf": sum(1 for i in items if i["action"] == "skip_off_shelf"),
        "manual": sum(1 for i in items if i["action"] == "manual"),
        "no_barcode": sum(1 for i in items if i["action"] == "no_barcode"),
        "hg_missing": sum(1 for i in items if i["action"] == "hg_missing"),
        "price_warn": sum(1 for i in items if i["price_warn"]),
        "hg_only": len(hg_only),
        "hg_only_in_stock": sum(1 for x in hg_only if x["qty"] > 0),
        "weight": sum(1 for i in items if i["weight"]),
    }
    return {
        "items": items,
        "summary": summary,
        "hg_only": sorted(hg_only, key=lambda x: -x["qty"]),
        "duplicates": dups,
        "built_at": time.time(),
    }


def build_payload(goods, target_stock, target_state, mode="full", price=None):
    g = copy.deepcopy(goods)
    g.pop("_category_name", None)
    g.pop("page_num", None)
    if mode == "minimal":
        g = {k: g.get(k) for k in MIN_KEYS if k in g}
    g["goods_state"] = target_state
    if price is not None:
        g["price"] = int(price)
    g["is_modify_prop"] = False
    g.setdefault("is_modify_spec", False)
    g.setdefault("is_required_goods", False)
    if target_stock is None:
        g.pop("stock_num", None)
        for s in g.get("sku_item_list") or []:
            s.pop("stock_num", None)
    else:
        g["stock_num"] = int(target_stock)
        for s in g.get("sku_item_list") or []:
            s["stock_num"] = int(target_stock)
    if mode == "minimal" and target_stock is not None:
        g["stock_num"] = int(target_stock)
    return g


def public_item(it):
    out = {k: v for k, v in it.items() if k != "goods"}
    g = it.get("goods") or {}
    imgs = g.get("image_urls_list") or []
    out["image"] = imgs[0] if imgs else (g.get("image_url") or "")
    out["cur_state_cn"] = STATE_CN.get(it["cur_state"], it["cur_state"])
    out["target_state_cn"] = STATE_CN.get(it.get("target_state"), "")
    out["hg_qty"] = it.get("hg_qty")
    out["cur_stock"] = it.get("cur_stock")
    return out
