"""组合优化器单元测试：python3 -m unittest discover -s tests -v"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

from optimizer import Offer, optimize  # noqa: E402


class TestOptimize(unittest.TestCase):
    def test_sample_site_demand_180(self):
        # 样本站 MF-50：50片/¥120(30包)、100片/¥225(10包)、25片/¥68(50包)
        offers = [Offer(50, 12000, 30), Offer(100, 22500, 10), Offer(25, 6800, 50)]
        plan = optimize(offers, 180)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.packs, [0, 2, 0])      # 2 包 100 片装
        self.assertEqual(plan.total_pieces, 200)
        self.assertEqual(plan.total_price_fen, 45000)

    def test_tie_prefers_less_overage(self):
        # 同价 ¥200：2×50片=100片 与 1×120片=120片，需求 100 → 选超量少的
        offers = [Offer(50, 10000, 10), Offer(120, 20000, 10)]
        plan = optimize(offers, 100)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.total_price_fen, 20000)
        self.assertEqual(plan.total_pieces, 100)
        self.assertEqual(plan.packs, [2, 0])

    def test_stock_bound_forces_mix(self):
        # 100片装只有 1 包库存，必须混购 50片装
        offers = [Offer(100, 20000, 1), Offer(50, 12000, 10)]
        plan = optimize(offers, 150)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.packs, [1, 1])
        self.assertEqual(plan.total_pieces, 150)
        self.assertEqual(plan.total_price_fen, 32000)

    def test_insufficient_stock_returns_none(self):
        self.assertIsNone(optimize([Offer(50, 12000, 1)], 100))
        self.assertIsNone(optimize([], 10))

    def test_use_up_exact_stock(self):
        offers = [Offer(100, 22500, 10), Offer(50, 12000, 30), Offer(25, 6800, 50)]
        plan = optimize(offers, 1000)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.packs, [10, 0, 0])     # 恰好 1000 片，¥2250
        self.assertEqual(plan.total_pieces, 1000)

    def test_zero_stock_offer_ignored(self):
        offers = [Offer(10, 100, 0), Offer(50, 12000, 5)]
        plan = optimize(offers, 50)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.packs, [0, 1])

    def test_mixed_remainder_optimal(self):
        # 需求 175：1×100+1×50+1×25 = 175 片 ¥413，优于 2×100=200片 ¥450、1×100+3×25=175片 ¥429
        offers = [Offer(50, 12000, 30), Offer(100, 22500, 10), Offer(25, 6800, 50)]
        plan = optimize(offers, 175)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.packs, [1, 1, 1])
        self.assertEqual(plan.total_pieces, 175)
        self.assertEqual(plan.total_price_fen, 41300)


if __name__ == "__main__":
    unittest.main()
