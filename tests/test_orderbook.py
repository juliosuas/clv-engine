import pytest

from clv_engine.fees import FeeModel
from clv_engine.orderbook import complement, fill_for_shares, fill_for_stake

PM = FeeModel("polymarket", rate=0.05)


def test_fill_walks_depth():
    f = fill_for_stake([(0.6, 100), (0.5, 100)], 80, PM)  # unsorted input
    assert f.complete
    assert f.shares == pytest.approx(150)
    assert f.avg_price == pytest.approx(80 / 150)
    assert f.fee == pytest.approx(round(100 * 0.05 * 0.25 + 50 * 0.05 * 0.24, 5))
    assert f.effective_price == pytest.approx((80 + f.fee) / 150)


def test_partial_fill_flagged():
    f = fill_for_stake([(0.5, 10)], 100, PM)
    assert not f.complete and f.shares == pytest.approx(10)


def test_fill_for_shares():
    f = fill_for_shares([(0.4, 5), (0.45, 100)], 20, PM)
    assert f.complete and f.notional == pytest.approx(5 * 0.4 + 15 * 0.45)


def test_complement_converts_no_bids_to_yes_asks():
    assert complement([(0.48, 10), (0.47, 5)]) == [(0.52, 10), (0.53, 5)]
