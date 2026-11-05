# -*- coding: utf-8 -*-
"""上架候选：从惠管家商品档案 + 库存里挑出小程序还没有的商品。"""
import io
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import engine

MAX_IMAGE_BYTES = 2 * 1024 * 1024


def to_fen(yuan):
    try:
        return int((Decimal(str(yuan)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return 0


def to_yuan(fen):
    return float(Decimal(str(fen or 0)) / 100)


def is_weight_goods(row, keywords):
    unit = row.get("unitName") or row.get("goodsUnitName") or ""
    if engine.is_weight(unit, keywords):
        return True
    if str(row.get("priceTypeStr") or "").strip() == "称重":
        return True
    return str(row.get("isWeigh") or "") in ("1", "True", "true")


def existing_barcodes(mini_goods):
    out = set()
    for g in mini_goods:
        bc = engine.extract_barcode(g.get("desc"))
        if bc:
            out.add(engine.norm_barcode(bc))
    return out


def build_candidates(archive, stock_rows, mini_goods, policies=None, include_zero=False):
    policies = policies or {}
    kws = policies.get("weight_keywords") or ["kg", "千克", "公斤", "斤", "克", "g"]
    used = existing_barcodes(mini_goods)
    stock = {}
    for r in stock_rows:
        bc = engine.norm_barcode(r.get("barCode"))
        if bc:
            stock[bc] = r

    out, seen = [], set()
    for a in archive:
        bc = str(a.get("barCode") or "").strip()
        if not bc:
            continue
        key = engine.norm_barcode(bc)
        if key in used or key in seen:
            continue
        if a.get("isDeleted") in (True, 1, "1", "true", "True"):
            continue
        row = stock.get(key)
        qty = float(row.get("quantity") or 0) if row else 0.0
        if not include_zero and qty <= 0:
            continue
        seen.add(key)
        unit = a.get("unitName") or a.get("goodsUnitName") or ""
        weight = is_weight_goods(a, kws)
        out.append({
            "goods_id": a.get("goodsId") or a.get("id"),
            "goods_code": a.get("goodsCode"),
            "name": (a.get("goodsName") or "").strip(),
            "barcode": bc,
            "hg_category": a.get("categoryName") or "",
            "unit": unit,
            "qty": qty,
            "price_yuan": float(a.get("salePrice") or 0),
            "weight": weight,
            "photo": a.get("photo") or a.get("compressImageUrl") or "",
            "created_at": a.get("createdAt") or "",
        })
    out.sort(key=lambda x: (-x["qty"], x["name"]))
    return out


def prepare_image(raw, max_side=1000, quality=88):
    """把惠管家的图转成合规 JPEG（≤2MB）。"""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    for q in (quality, 80, 70, 60):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_IMAGE_BYTES:
            return data
    raise ValueError("图片压不到 2MB 以内")


def build_goods(cand, category_id, image_url="", name=None, price_fen=None,
                desc=None):
    """构造 goods/add 的 goods 对象。"""
    name = (name or cand["name"]).strip()
    price_fen = to_fen(cand["price_yuan"]) if price_fen is None else int(price_fen)
    goods = {
        "name": name,
        "desc": desc or f'【WXDS-{cand["barcode"]}】',
        "category_id": int(category_id),
        "price": price_fen,
        "goods_state": engine.NORMAL,
        "image_urls_list": [image_url] if image_url else [],
        "image_url": image_url or "",
        "prop_item_list": [],
        "sku_item_list": [],
        "tag_type": "EN_GOODS_TAG_NONE",
        "is_support_zero_price": True,
        "is_required_goods": False,
        "is_modify_prop": False,
        "is_modify_spec": False,
        "discount_price_one_order_limit": 0,
    }
    if cand["weight"]:
        if cand["qty"] <= 0:
            goods["goods_state"] = engine.SOLD_OUT
            goods["stock_num"] = 0
    else:
        n = max(0, int(cand["qty"]))
        goods["stock_num"] = n
        if n <= 0:
            goods["goods_state"] = engine.SOLD_OUT
    return goods
