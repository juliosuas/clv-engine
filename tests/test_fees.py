import pytest

from clv_engine.fees import FeeModel, kalshi_fee, polymarket_fee


@pytest.mark.parametrize("price,expected", [(0.01, 0.0495), (0.10, 0.45), (0.30, 1.05), (0.50, 1.25), (0.70, 1.05), (0.95, 0.2375)])
def test_polymarket_sports_table_100_shares(price, expected):
    # docs.polymarket.com/trading/fees "Sports" table (100 shares), table rounds to cents
    assert polymarket_fee(100, price, 0.05) == pytest.approx(expected, abs=1e-5)
    assert round(polymarket_fee(100, price, 0.05), 2) == round(expected + 1e-9, 2)


def test_polymarket_fee_symmetric_and_zero_rate():
    assert polymarket_fee(37, 0.3) == polymarket_fee(37, 0.7)
    assert polymarket_fee(100, 0.5, 0.0) == 0.0


def test_kalshi_round_up_to_cent():
    assert kalshi_fee(100, 0.5) == pytest.approx(1.75)  # 0.07*100*0.25, no float creep to 1.76
    assert kalshi_fee(1, 0.5) == pytest.approx(0.02)  # 0.0175 -> 0.02
    assert kalshi_fee(10, 0.3) == pytest.approx(0.15)  # 0.147 -> 0.15
    assert kalshi_fee(100, 0.5, multiplier=0.5) == pytest.approx(0.88)  # 0.875 -> 0.88
    assert kalshi_fee(1, 0.5, rounding="micro") == pytest.approx(0.0175)


def test_kalshi_multilevel_rounds_once():
    fm = FeeModel("kalshi")
    # 0.07*(10*.4*.6 + 10*.41*.59) = 0.33733 -> 0.34 (one order, rounded once)
    assert fm.fill_fee([(0.40, 10), (0.41, 10)]) == pytest.approx(0.34)


def test_polymarket_multilevel_sums_levels():
    fm = FeeModel("polymarket", rate=0.05)
    assert fm.fill_fee([(0.5, 10), (0.6, 10)]) == pytest.approx(0.125 + 0.12)
