"""滤膜采购组合优化 API（FastAPI）。

POST /api/v1/quote
  输入：目录 URL、滤膜型号、需求片数
  输出：库存内总价最低的整包组合（各包装采购包数、实到片数、总价、详情来源）

POST /api/v1/rules/publish
  输入：同站多张详情页 URL + 人工核准的型号/每包片数/含税包价/可购包数
        + 四项字段的 CSS 选择器
  输出：逐页核对结果；全部吻合才原子启用并持久保存新规则，否则保留原有规则

GET /api/v1/rules
  查看各站点当前启用的规则版本

规则版本语义：
  - 报价开始时固定规则版本（快照），采集中途发布新版也不会混用新旧字段；
  - 已启用的规则解析失败（缺字段/多值冲突）时不给确定报价（scrape_failed）。

报价状态语义：
  ok                  找到确定的最优组合
  insufficient_stock  库存总量不足，不给出确定最低价
  scrape_failed       匹配详情页抓取/解析失败，不给出确定最低价
  no_matching_products 目录中没有匹配型号的商品
  catalog_unreachable 目录列表页抓取失败
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

import requests
import soupsieve
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from optimizer import Offer, optimize
from rules import DEFAULT_RULE, FIELD_LABELS, FIELD_ORDER, RuleStore
from scraper import (
    USER_AGENT,
    ScrapeError,
    crawl_catalog,
    extract_field_candidates,
    fetch_soup,
    normalize_model,
)

app = FastAPI(title="滤膜采购组合优化 API", version="1.1.0")

# 规则存储：默认 ./data/rules.json，容器中用 RULES_STORE_PATH 指向持久卷
RULE_STORE = RuleStore(os.environ.get("RULES_STORE_PATH", "data/rules.json"))


class QuoteRequest(BaseModel):
    catalog_url: str = Field(..., description="供应商目录列表页 URL")
    model: str = Field(..., min_length=1, description="滤膜型号，如 MF-50")
    demand: int = Field(..., ge=1, description="需求片数")


class FieldSelectors(BaseModel):
    """四项字段各自的 CSS 选择器。"""

    model: str = Field(..., min_length=1, description="型号")
    pack_size: str = Field(..., min_length=1, description="每包片数")
    pack_price: str = Field(..., min_length=1, description="含税包价")
    stock_packs: str = Field(..., min_length=1, description="可购包数")


class ExpectedValues(BaseModel):
    """人工核准的详情页字段值（含税包价单位：元）。"""

    model: str = Field(..., min_length=1)
    pack_size: int = Field(..., ge=1)
    pack_price: Decimal = Field(..., ge=0)
    stock_packs: int = Field(..., ge=0)


class PageToVerify(BaseModel):
    url: str = Field(..., min_length=1, description="详情页 URL")
    expected: ExpectedValues


class PublishRequest(BaseModel):
    pages: list[PageToVerify] = Field(..., description="同站多张详情页及其核准值")
    selectors: FieldSelectors


def _dump(model: BaseModel) -> dict:
    """pydantic v1/v2 兼容。"""
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def fen_to_yuan(fen: int) -> str:
    sign = "-" if fen < 0 else ""
    fen = abs(fen)
    return f"{sign}{fen // 100}.{fen % 100:02d}"


def _to_fen(d: Decimal) -> int:
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@app.get("/health")
def health():
    return {"status": "up"}


@app.get("/api/v1/rules")
def list_rules():
    snap = RULE_STORE.snapshot()
    return {
        "default": {
            "rule_version": DEFAULT_RULE.version,
            "source": DEFAULT_RULE.source,
            "selectors": DEFAULT_RULE.selectors,
            "note": "未发布规则的站点使用内置默认规则（启发式 + 正则回退）",
        },
        "sites": {
            site: {
                "rule_version": rv.version,
                "source": rv.source,
                "selectors": rv.selectors,
                "created_at": rv.created_at,
            }
            for site, rv in snap.sites().items()
        },
    }


def _expected_canonical(field: str, expected: ExpectedValues):
    """核准值转为规范比较值：model 归一化；价格转分；其余为 int。"""
    if field == "model":
        return normalize_model(expected.model)
    if field == "pack_price":
        return _to_fen(expected.pack_price)
    return getattr(expected, field)


def _expected_display(field: str, expected: ExpectedValues):
    if field == "pack_price":
        return fen_to_yuan(_to_fen(expected.pack_price))
    return getattr(expected, field)


def _verify_page(session: requests.Session, page: PageToVerify, selectors: dict) -> dict:
    """用候选规则核对一张详情页：页面失败/缺字段/多值冲突/核对不符 即不通过。"""
    try:
        soup = fetch_soup(session, page.url)
    except ScrapeError as exc:
        return {"url": page.url, "ok": False, "error": "fetch_failed", "detail": str(exc)}

    fields: dict[str, dict] = {}
    page_ok = True
    for field in FIELD_ORDER:
        selector = selectors[field]
        expected_val = _expected_canonical(field, page.expected)
        entry: dict = {
            "selector": selector,
            "expected": _expected_display(field, page.expected),
        }
        cands = extract_field_candidates(soup, selector, field)
        if not cands:
            entry.update(ok=False, error="missing",
                         detail=f"选择器未取到{FIELD_LABELS[field]}（无匹配节点或值无法解析）")
        elif len(cands) > 1:
            entry.update(ok=False, error="conflict",
                         actual=[raw for raw, _ in cands],
                         detail=f"选择器命中多个冲突的{FIELD_LABELS[field]}")
        else:
            raw, val = cands[0]
            # 型号按归一化包含匹配（节点可能带"型号："等前缀），其余精确相等
            match = bool(expected_val) and expected_val in val if field == "model" else val == expected_val
            entry.update(ok=bool(match), actual=raw)
            if not match:
                entry["error"] = "mismatch"
                entry["detail"] = f"抽取值与人工核准的{FIELD_LABELS[field]}不符"
        page_ok = page_ok and entry["ok"]
        fields[field] = entry

    return {"url": page.url, "ok": page_ok, "fields": fields}


@app.post("/api/v1/rules/publish")
def publish_rules(req: PublishRequest):
    if not req.pages:
        raise HTTPException(status_code=400, detail="至少提交一张详情页")

    # 所有 URL 必须同站（同 host:port）
    netlocs = set()
    for p in req.pages:
        u = urlparse(p.url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise HTTPException(status_code=400, detail=f"非法 URL: {p.url}")
        netlocs.add(u.netloc)
    if len(netlocs) != 1:
        raise HTTPException(status_code=400, detail="所有详情页 URL 必须属于同一站点（同 host）")
    site = netlocs.pop()

    selectors = _dump(req.selectors)
    # CSS 选择器语法预检
    for field in FIELD_ORDER:
        try:
            soupsieve.compile(selectors[field])
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"字段 {field}（{FIELD_LABELS[field]}）的 CSS 选择器无效: {selectors[field]!r}",
            )

    # 逐页抓取并用候选规则核对
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    results = [_verify_page(session, p, selectors) for p in req.pages]

    if not all(r["ok"] for r in results):
        # 页面失败/缺字段/多值冲突/核对不符：保留该站原有规则
        current = RULE_STORE.snapshot().resolve(req.pages[0].url)
        return {
            "status": "rejected",
            "activated": False,
            "site": site,
            "rule_version": current.version,
            "message": "存在页面核对失败，保留该站原有规则",
            "pages": results,
        }

    # 全部吻合：原子启用并持久保存
    rv = RULE_STORE.publish(site, selectors)
    return {
        "status": "published",
        "activated": True,
        "site": site,
        "rule_version": rv.version,
        "message": f"全部 {len(results)} 张页面核对通过，规则 v{rv.version} 已原子启用并持久保存",
        "pages": results,
    }


@app.post("/api/v1/quote")
def quote(req: QuoteRequest):
    # 报价开始时固定规则版本：整个抓取过程只使用此快照，
    # 即使中途发布了新版规则，本次报价也不会混用新旧字段
    rules = RULE_STORE.snapshot()
    pinned = rules.resolve(req.catalog_url)

    try:
        crawl = crawl_catalog(req.catalog_url, req.model, rules=rules)
    except ScrapeError as exc:
        return {
            "status": "catalog_unreachable",
            "message": f"目录列表页抓取失败：{exc}",
            "model": req.model,
            "demand": req.demand,
            "rule_version": pinned.version,
            "rule_source": pinned.source,
            "solution": None,
        }

    base = {
        "model": req.model,
        "demand": req.demand,
        "rule_version": pinned.version,
        "rule_source": pinned.source,
        "list_pages": crawl.list_pages,
        "matched_detail_pages": [o.detail_url for o in crawl.matched],
        "duplicate_detail_links_skipped": crawl.duplicate_details,
    }

    # 匹配详情抓取失败（含已启用规则解析失败）：不给出确定的最低价
    if crawl.failed_details:
        return {
            **base,
            "status": "scrape_failed",
            "message": "部分匹配详情页抓取或解析失败，无法给出确定的最低价",
            "failed_detail_pages": crawl.failed_details,
            "solution": None,
        }

    if not crawl.matched:
        resp = {
            **base,
            "status": "no_matching_products",
            "message": "目录中未找到匹配该型号的商品",
            "solution": None,
        }
        if crawl.unverified_failed:
            resp["message"] += "（另有部分详情页无法抓取，未能核对）"
            resp["unverified_failed_pages"] = crawl.unverified_failed
        return resp

    # 兜底扫描时有详情页未能核对：可能漏掉更优的匹配，不给确定最低价
    if crawl.unverified_failed:
        return {
            **base,
            "status": "scrape_failed",
            "message": "部分详情页无法抓取、未能核对是否还有更优匹配，无法给出确定的最低价",
            "failed_detail_pages": crawl.unverified_failed,
            "solution": None,
        }

    offers = [Offer(o.pack_size, o.price_fen, o.stock_packs) for o in crawl.matched]
    total_available = sum(o.pack_size * o.stock_packs for o in offers)

    try:
        plan = optimize(offers, req.demand)
    except ValueError as exc:
        return {
            **base,
            "status": "demand_too_large",
            "message": str(exc),
            "total_available_pieces": total_available,
            "solution": None,
        }

    # 库存不足：不给出确定的最低价
    if plan is None:
        return {
            **base,
            "status": "insufficient_stock",
            "message": f"库存不足：可购总量 {total_available} 片 < 需求 {req.demand} 片，无法给出确定的最低价",
            "total_available_pieces": total_available,
            "solution": None,
        }

    items = []
    for info, packs in zip(crawl.matched, plan.packs):
        if packs == 0:
            continue
        items.append(
            {
                "detail_url": info.detail_url,
                "title": info.title,
                "model": info.model,
                "pack_size": info.pack_size,
                "pack_price": fen_to_yuan(info.price_fen),
                "stock_packs": info.stock_packs,
                "purchased_packs": packs,
                "delivered_pieces": packs * info.pack_size,
                "subtotal_price": fen_to_yuan(packs * info.price_fen),
            }
        )

    return {
        **base,
        "status": "ok",
        "message": "已找到库存内总价最低的整包组合",
        "solution": {
            "items": items,
            "total_pieces": plan.total_pieces,
            "demand": req.demand,
            "overage": plan.total_pieces - req.demand,
            "total_price": fen_to_yuan(plan.total_price_fen),
            "currency": "CNY",
        },
        "total_available_pieces": total_available,
    }
