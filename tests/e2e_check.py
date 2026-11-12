"""端到端检查：本地 HTTP 服务托管样本站，调用 FastAPI 完整链路。

运行：python3 tests/e2e_check.py

覆盖两部分：
1. 原报价 API（v1 样本站，内置默认规则）；
2. 规则发布 API + 改版样例站（v2）：逐页核对、原子启用、版本固定、
   已启用规则解析失败不给确定报价、站点间隔离。
"""
import functools
import http.server
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

# 规则存储指向临时目录，必须在导入 main 之前设置
RULES_DIR = tempfile.mkdtemp(prefix="rules-store-")
os.environ["RULES_STORE_PATH"] = str(pathlib.Path(RULES_DIR) / "rules.json")

from fastapi.testclient import TestClient  # noqa: E402

from main import RULE_STORE, app  # noqa: E402
from scraper import crawl_catalog  # noqa: E402

SITE_DIR = ROOT / "sample_site"
SITE_V2_DIR = ROOT / "sample_site_v2"

# 改版后详情页的新节点选择器（人工整理，待核对发布）
V2_SELECTORS = {
    "model": "span.sku",
    "pack_size": "span.pack-spec",
    "pack_price": "span.price-now",
    "stock_packs": "span.stock-left",
}
# v2 站 MF-50 三个详情页的人工核准值（改版后新价）
V2_MF50_PAGES = [
    ("details/mf50-50.html", {"model": "MF-50", "pack_size": 50, "pack_price": "125.00", "stock_packs": 28}),
    ("details/mf50-100.html", {"model": "MF-50", "pack_size": 100, "pack_price": "235.00", "stock_packs": 12}),
    ("details/mf50-25.html", {"model": "MF-50", "pack_size": 25, "pack_price": "72.00", "stock_packs": 40}),
]


def start_site_server(directory: str) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def check(name: str, cond: bool, extra: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"检查失败: {name}")


def publish_payload(base: str, pages=V2_MF50_PAGES, selectors=V2_SELECTORS):
    return {
        "pages": [{"url": f"{base}/{path}", "expected": exp} for path, exp in pages],
        "selectors": selectors,
    }


def main():
    httpd1, base1 = start_site_server(str(SITE_DIR))
    client = TestClient(app)
    catalog = f"{base1}/index.html"
    print(f"样本站 v1: {catalog}")

    # 1) 正常求解：MF-50 需求 180 → 2 包 100 片装，¥450
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 180})
    assert r.status_code == 200, r.text
    d = r.json()
    check("MF-50/180 状态 ok", d["status"] == "ok")
    check("总价 ¥450.00", d["solution"]["total_price"] == "450.00")
    check("实到 200 片", d["solution"]["total_pieces"] == 200)
    check("超量 20 片", d["solution"]["overage"] == 20)
    items = d["solution"]["items"]
    check("仅一种包装", len(items) == 1)
    check("100片装 ×2", items[0]["pack_size"] == 100 and items[0]["purchased_packs"] == 2)
    check("详情来源 URL", items[0]["detail_url"].endswith("/details/mf50-100.html"))
    check("未发布规则的站点用内置 v1", d["rule_version"] == 1 and d["rule_source"] == "builtin")

    # 2) 分页与跨页去重：3 个列表页都被抓取，mf50-50 只计一次
    check("抓取 3 个列表页", len(d["list_pages"]) == 3, str(d["list_pages"]))
    check("跨页重复跳过 1 次", d["duplicate_detail_links_skipped"] == 1)
    check("匹配详情 3 个", len(d["matched_detail_pages"]) == 3, str(d["matched_detail_pages"]))

    # 3) 混购 + 库存约束：需求 175 → 100+50+25 各 1 包，¥413
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 175})
    d = r.json()
    check("MF-50/175 状态 ok", d["status"] == "ok")
    check("混购 3 种包装", len(d["solution"]["items"]) == 3)
    check("总价 ¥413.00", d["solution"]["total_price"] == "413.00")
    check("实到 175 片（零超量）", d["solution"]["total_pieces"] == 175 and d["solution"]["overage"] == 0)

    # 4) 库存上限：需求 1000 → 恰好买光 10 包 100 片装
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 1000})
    d = r.json()
    check("MF-50/1000 状态 ok", d["status"] == "ok")
    check("总价 ¥2250.00", d["solution"]["total_price"] == "2250.00")
    check("实到 1000 片", d["solution"]["total_pieces"] == 1000)

    # 5) 库存不足：需求 4000 > 可购总量 3750 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 4000})
    d = r.json()
    check("MF-50/4000 状态 insufficient_stock", d["status"] == "insufficient_stock")
    check("无方案、无总价", d["solution"] is None)
    check("报告可购总量 3750", d["total_available_pieces"] == 3750)

    # 6) 匹配详情抓取失败：BROKEN-1 的详情页 404 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "BROKEN-1", "demand": 10})
    d = r.json()
    check("BROKEN-1 状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    check("报告失败详情页", d["failed_detail_pages"][0].endswith("/details/broken-404.html"))

    # 7) 无匹配型号（兜底扫描时 broken-404 无法核对，应在响应中如实标注）
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NOPE-9", "demand": 10})
    d = r.json()
    check("NOPE-9 状态 no_matching_products", d["status"] == "no_matching_products")
    check("无方案", d["solution"] is None)
    check(
        "标注未能核对的页面",
        any(u.endswith("/details/broken-404.html") for u in d.get("unverified_failed_pages", [])),
    )

    # 8) 其他型号正常匹配：NY-66 需求 150 → 2 包 100 片装 ¥360
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NY-66", "demand": 150})
    d = r.json()
    check("NY-66/150 状态 ok", d["status"] == "ok")
    check("总价 ¥360.00", d["solution"]["total_price"] == "360.00")

    # 9) 目录不可达
    r = client.post("/api/v1/quote", json={"catalog_url": f"{base1}/no-such-page.html", "model": "MF-50", "demand": 10})
    d = r.json()
    check("目录不存在状态 catalog_unreachable", d["status"] == "catalog_unreachable")

    # 10) 兜底扫描语义：列表文案不含型号时逐个核对详情页
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "details").mkdir()
        (tmp_path / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/d1.html">特价滤膜 甲</a></li>'
            '<li class="product-item"><a class="product-link" href="details/d2.html">特价滤膜 乙</a></li>'
            "</ul></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "details" / "d1.html").write_text(
            '<html><body><h1 class="product-title">特价滤膜 甲</h1>'
            '<table><tr><td class="product-model">FALL-1</td>'
            '<td class="pack-size">40</td>'
            '<td class="pack-price">¥90.00</td>'
            '<td class="stock-packs">8</td></tr></table></body></html>',
            encoding="utf-8",
        )
        # d2.html 故意不存在（404）
        httpd_tmp, base_tmp = start_site_server(tmp)

        # 10a) 详情页型号匹配（FALL-1 只在详情页出现）→ 命中，但 d2 未能核对 → 不给确定最低价
        r = client.post("/api/v1/quote", json={"catalog_url": f"{base_tmp}/index.html", "model": "FALL-1", "demand": 40})
        d = r.json()
        check("兜底命中但存在未核对页 → scrape_failed", d["status"] == "scrape_failed")
        check("无方案", d["solution"] is None)
        check("失败页为 d2", d["failed_detail_pages"][0].endswith("/details/d2.html"))

        # 10b) 全部不匹配 → no_matching_products 并标注未核对页
        r = client.post("/api/v1/quote", json={"catalog_url": f"{base_tmp}/index.html", "model": "ABSENT-7", "demand": 40})
        d = r.json()
        check("兜底无匹配 → no_matching_products", d["status"] == "no_matching_products")
        check("标注未核对页 d2", any(u.endswith("/details/d2.html") for u in d.get("unverified_failed_pages", [])))
        httpd_tmp.shutdown()

    # ================= 规则发布 API（改版样例站 v2） =================
    # v2 站点目录下同时放一份旧版页面（legacy/），用于演示"已启用规则解析失败"
    merged = tempfile.TemporaryDirectory()
    merged_path = pathlib.Path(merged.name)
    shutil.copytree(SITE_V2_DIR, merged_path, dirs_exist_ok=True)
    shutil.copytree(SITE_DIR, merged_path / "legacy")
    httpd2, base2 = start_site_server(str(merged_path))
    catalog_v2 = f"{base2}/index.html"
    catalog_legacy = f"{base2}/legacy/index.html"
    print(f"样本站 v2（改版）: {catalog_v2}")

    # 11) 发布前：默认规则抓新版详情页，部分页面缺字段 → 不给确定报价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog_v2, "model": "MF-50", "demand": 180})
    d = r.json()
    check("改版后默认规则报价 → scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    check("仍用内置 v1", d["rule_version"] == 1)
    check(
        "缺旧节点的页面被列入失败",
        any(u.endswith("/details/mf50-50.html") for u in d["failed_detail_pages"]),
    )

    # 12) 核对不符：核准价写错（¥999.00）→ 拒绝发布，保留原规则
    bad_pages = [
        ("details/mf50-50.html", {"model": "MF-50", "pack_size": 50, "pack_price": "125.00", "stock_packs": 28}),
        ("details/mf50-100.html", {"model": "MF-50", "pack_size": 100, "pack_price": "999.00", "stock_packs": 12}),
    ]
    r = client.post("/api/v1/rules/publish", json=publish_payload(base2, pages=bad_pages))
    d = r.json()
    check("核准价错误 → rejected", d["status"] == "rejected" and d["activated"] is False)
    check("保留原规则 v1", d["rule_version"] == 1)
    bad_page = next(p for p in d["pages"] if p["url"].endswith("mf50-100.html"))
    check("该页含税包价 mismatch", bad_page["fields"]["pack_price"]["error"] == "mismatch")
    check("其余字段核对通过", bad_page["fields"]["model"]["ok"] and bad_page["fields"]["stock_packs"]["ok"])

    # 13) 缺字段：选择器取不到节点 → 拒绝发布
    r = client.post(
        "/api/v1/rules/publish",
        json=publish_payload(base2, selectors={**V2_SELECTORS, "stock_packs": "span.no-such-class"}),
    )
    d = r.json()
    check("选择器取不到节点 → rejected", d["status"] == "rejected")
    check(
        "全部页面可购包数 missing",
        all(p["fields"]["stock_packs"]["error"] == "missing" for p in d["pages"]),
    )

    # 14) 多值冲突：选择器过宽命中多个不同值 → 拒绝发布
    r = client.post(
        "/api/v1/rules/publish",
        json=publish_payload(base2, selectors={f: "span" for f in V2_SELECTORS}),
    )
    d = r.json()
    check("选择器过宽 → rejected", d["status"] == "rejected")
    check("型号多值冲突", d["pages"][0]["fields"]["model"]["error"] == "conflict")

    # 15) 页面失败：详情页 404 → 拒绝发布
    pages_404 = V2_MF50_PAGES[:1] + [("details/no-such.html", V2_MF50_PAGES[0][1])]
    r = client.post("/api/v1/rules/publish", json=publish_payload(base2, pages=pages_404))
    d = r.json()
    check("含 404 页 → rejected", d["status"] == "rejected")
    check("失败页标注 fetch_failed", d["pages"][1]["error"] == "fetch_failed")

    # 16) 跨站 URL → 400
    r = client.post(
        "/api/v1/rules/publish",
        json={
            "pages": [
                {"url": f"{base2}/details/mf50-50.html", "expected": V2_MF50_PAGES[0][1]},
                {"url": f"{base1}/details/mf50-50.html", "expected": V2_MF50_PAGES[0][1]},
            ],
            "selectors": V2_SELECTORS,
        },
    )
    check("跨站 URL → 400", r.status_code == 400)

    # 17) 全部吻合：原子启用并持久保存
    r = client.post("/api/v1/rules/publish", json=publish_payload(base2))
    d = r.json()
    check("全部核对通过 → published", d["status"] == "published" and d["activated"] is True)
    check("启用 v2", d["rule_version"] == 2)
    check("逐页核对结果全部 ok", all(p["ok"] for p in d["pages"]) and len(d["pages"]) == 3)
    price_field = d["pages"][1]["fields"]["pack_price"]
    check("逐页含选择器与抽取值", price_field["selector"] == "span.price-now" and price_field["actual"] == "¥235.00")

    # 18) 持久化：规则文件已落盘，可独立核对
    rules_file = pathlib.Path(os.environ["RULES_STORE_PATH"])
    check("规则文件已持久化", rules_file.exists())
    persisted = json.loads(rules_file.read_text(encoding="utf-8"))
    site_key = next(iter(persisted["sites"]))
    check("持久化内容为 v2", persisted["sites"][site_key]["versions"][-1]["version"] == 2)

    # 19) 发布后报价：用新规则解析新节点，价格/库存为改版后数据
    r = client.post("/api/v1/quote", json={"catalog_url": catalog_v2, "model": "MF-50", "demand": 180})
    d = r.json()
    check("发布后 MF-50/180 状态 ok", d["status"] == "ok")
    check("使用规则 v2", d["rule_version"] == 2 and d["rule_source"] == "published")
    check("新价总价 ¥470.00（2×¥235）", d["solution"]["total_price"] == "470.00")
    check("新库存 12 包", d["solution"]["items"][0]["stock_packs"] == 12)

    # 20) 规则泛化：未参与核对的 NY-66 页面同样按新规则解析
    r = client.post("/api/v1/quote", json={"catalog_url": catalog_v2, "model": "NY-66", "demand": 150})
    d = r.json()
    check("NY-66/150 状态 ok", d["status"] == "ok")
    check("总价 ¥370.00（2×¥185）", d["solution"]["total_price"] == "370.00")

    # 21) 已启用规则解析失败：同站旧版页面没有新节点 → 不给确定报价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog_legacy, "model": "MF-50", "demand": 180})
    d = r.json()
    check("旧版页面 + 已启用新规则 → scrape_failed", d["status"] == "scrape_failed")
    check("无方案、无总价", d["solution"] is None)
    check("失败页为旧版详情页", all("/legacy/" in u for u in d["failed_detail_pages"]))

    # 22) 站点隔离：v1 站未发布规则，仍用内置默认规则正常报价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 180})
    d = r.json()
    check("v1 站不受影响", d["status"] == "ok" and d["solution"]["total_price"] == "450.00")
    check("v1 站仍为内置 v1", d["rule_version"] == 1)

    # 23) 规则列表可查询
    r = client.get("/api/v1/rules")
    d = r.json()
    check("GET /rules 含默认规则", d["default"]["rule_version"] == 1)
    check("GET /rules 含已发布站点", any(v["rule_version"] == 2 for v in d["sites"].values()))

    # 24) 版本固定：报价开始时的快照决定整次抓取使用的规则。
    #     模拟"采集中途发布新版"：直接在存储层注入一个选择器已失效的 v3
    #     （绕过 API 核对，仅用于制造可观测的版本差异）。
    snap_v2 = RULE_STORE.snapshot()
    broken = {**V2_SELECTORS, "pack_price": "span.no-such-price"}
    RULE_STORE.publish(site_key, broken)
    old = crawl_catalog(catalog_v2, "MF-50", rules=snap_v2)
    check("进行中的报价沿用 v2 快照 → 全部解析成功", len(old.matched) == 3 and not old.failed_details)
    new = crawl_catalog(catalog_v2, "MF-50", rules=RULE_STORE.snapshot())
    check("新报价用 v3（失效选择器）→ 解析失败", len(new.failed_details) == 3 and not new.matched)
    r = client.post("/api/v1/quote", json={"catalog_url": catalog_v2, "model": "MF-50", "demand": 180})
    d = r.json()
    check("v3 解析失败 → 不给确定报价", d["status"] == "scrape_failed" and d["solution"] is None)
    check("报告当前规则 v3", d["rule_version"] == 3)

    httpd1.shutdown()
    httpd2.shutdown()
    merged.cleanup()
    print("\n全部端到端检查通过 ✓")


if __name__ == "__main__":
    main()
