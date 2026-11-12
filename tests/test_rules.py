"""规则存储 / 严格规则解析 / 快照固定 单元测试：python3 -m unittest discover -s tests -v"""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

from bs4 import BeautifulSoup  # noqa: E402

import rules as rules_mod  # noqa: E402
from rules import DEFAULT_RULE, RuleStore  # noqa: E402
from scraper import ScrapeError, extract_field_candidates, parse_detail_page  # noqa: E402

V2_SELECTORS = {
    "model": "span.sku",
    "pack_size": "span.pack-spec",
    "pack_price": "span.price-now",
    "stock_packs": "span.stock-left",
}

V2_PAGE = """
<html><body><main class="pdp">
  <h1 class="pdp-title">MCE 微孔滤膜 MF-50（100片经济装）</h1>
  <div class="pdp-meta">
    <span class="sku">MF-50</span>
    <span class="pack-spec">每包 100 片</span>
    <span class="price-now">¥235.00</span>
    <span class="stock-left">现货 12 包</span>
  </div>
</main></body></html>
"""

LEGACY_PAGE = """
<html><body>
  <h1 class="product-title">MCE 微孔滤膜 MF-50（100片经济装）</h1>
  <table class="specs">
    <tr><td class="product-model">MF-50</td><td class="pack-size">100</td>
    <td class="pack-price">¥225.00</td><td class="stock-packs">10</td></tr>
  </table>
</body></html>
"""


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html.encode(), "html.parser")


class TestRuleStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "rules.json"
        self.store = RuleStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_rule_when_nothing_published(self):
        snap = self.store.snapshot()
        rv = snap.resolve("http://shop.example.com/details/a.html")
        self.assertEqual(rv.version, 1)
        self.assertIs(rv, DEFAULT_RULE)
        self.assertFalse(rv.strict)

    def test_publish_assigns_incrementing_versions(self):
        rv2 = self.store.publish("shop.example.com", V2_SELECTORS)
        self.assertEqual(rv2.version, 2)
        rv3 = self.store.publish("shop.example.com", V2_SELECTORS)
        self.assertEqual(rv3.version, 3)
        self.assertEqual(self.store.snapshot().resolve("http://shop.example.com/x").version, 3)

    def test_snapshot_pins_version_across_publish(self):
        """报价开始时固定的快照不受中途发布影响，不会混用新旧字段。"""
        snap_before = self.store.snapshot()
        self.store.publish("shop.example.com", V2_SELECTORS)
        # 旧快照仍解析为 v1（内置默认），新快照为 v2
        self.assertEqual(snap_before.resolve("http://shop.example.com/x").version, 1)
        self.assertEqual(self.store.snapshot().resolve("http://shop.example.com/x").version, 2)
        # 其他站点不受发布影响
        self.assertEqual(self.store.snapshot().resolve("http://other.example.com/x").version, 1)

    def test_publish_persists_and_reloads(self):
        self.store.publish("shop.example.com", V2_SELECTORS)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("shop.example.com", data["sites"])
        reloaded = RuleStore(self.path)
        rv = reloaded.snapshot().resolve("http://shop.example.com/details/a.html")
        self.assertEqual(rv.version, 2)
        self.assertEqual(rv.selectors, V2_SELECTORS)
        self.assertTrue(rv.strict)

    def test_publish_is_atomic_when_persist_fails(self):
        """落盘失败时保留原有规则，内存状态不变。"""
        with mock.patch.object(rules_mod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.publish("shop.example.com", V2_SELECTORS)
        rv = self.store.snapshot().resolve("http://shop.example.com/x")
        self.assertEqual(rv.version, 1)  # 仍是内置默认
        self.assertFalse(self.path.exists())  # 没有残留文件

    def test_publish_requires_all_four_selectors(self):
        with self.assertRaises(ValueError):
            self.store.publish("shop.example.com", {"model": "span.sku"})


class TestExtractFieldCandidates(unittest.TestCase):
    def test_single_value(self):
        cands = extract_field_candidates(soup(V2_PAGE), "span.price-now", "pack_price")
        self.assertEqual(cands, [("¥235.00", 23500)])

    def test_missing_when_no_match_or_unparseable(self):
        self.assertEqual(extract_field_candidates(soup(V2_PAGE), "span.no-such", "model"), [])
        # 节点存在但文本无法解析出数字，同样视为缺字段
        html = '<span class="pack-spec">每包若干片</span>'
        self.assertEqual(extract_field_candidates(soup(html), "span.pack-spec", "pack_size"), [])

    def test_conflict_on_multiple_distinct_values(self):
        html = '<span class="price">¥100.00</span><span class="price">¥100.00</span><span class="price">¥235.00</span>'
        cands = extract_field_candidates(soup(html), "span.price", "pack_price")
        self.assertEqual([v for _, v in cands], [10000, 23500])  # 相同值去重，不同值保留

    def test_number_with_unit_text(self):
        cands = extract_field_candidates(soup(V2_PAGE), "span.stock-left", "stock_packs")
        self.assertEqual(cands, [("现货 12 包", 12)])


class TestStrictRuleParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuleStore(pathlib.Path(self.tmp.name) / "rules.json")
        self.rule = self.store.publish("shop.example.com", V2_SELECTORS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_strict_parse_ok(self):
        offer = parse_detail_page("http://shop.example.com/d/1", soup(V2_PAGE), self.rule)
        self.assertEqual(offer.model, "MF-50")
        self.assertEqual(offer.pack_size, 100)
        self.assertEqual(offer.price_fen, 23500)
        self.assertEqual(offer.stock_packs, 12)
        self.assertEqual(offer.title, "MCE 微孔滤膜 MF-50（100片经济装）")

    def test_strict_parse_missing_field_fails(self):
        # 旧版页面没有新节点：已启用规则解析失败
        with self.assertRaises(ScrapeError):
            parse_detail_page("http://shop.example.com/d/legacy", soup(LEGACY_PAGE), self.rule)

    def test_strict_parse_conflict_fails(self):
        html = """
        <span class="sku">MF-50</span>
        <span class="pack-spec">每包 100 片</span>
        <span class="price-now">¥235.00</span><span class="price-now">¥225.00</span>
        <span class="stock-left">现货 12 包</span>
        """
        with self.assertRaises(ScrapeError):
            parse_detail_page("http://shop.example.com/d/2", soup(html), self.rule)

    def test_no_rule_falls_back_to_heuristic(self):
        # 不传规则：内置启发式解析旧版页面（兼容旧行为）
        offer = parse_detail_page("http://shop.example.com/d/legacy", soup(LEGACY_PAGE), None)
        self.assertEqual(offer.pack_size, 100)
        self.assertEqual(offer.price_fen, 22500)


if __name__ == "__main__":
    unittest.main()
