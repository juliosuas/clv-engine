import pytest

from clv_engine.devig import american_to_decimal, devig, multiplicative, overround, power, shin


def test_even_two_way():
    for m in ("multiplicative", "power", "shin"):
        assert devig([1.9, 1.9], m) == pytest.approx([0.5, 0.5])


@pytest.mark.parametrize("method", ["multiplicative", "power", "shin"])
def test_sums_to_one_three_way(method):
    p = devig([1.48, 4.6, 5.6], method)
    assert sum(p) == pytest.approx(1.0)
    assert all(0 < x < 1 for x in p)


def test_power_and_shin_shade_longshot_vs_multiplicative():
    odds = [1.25, 6.0, 13.0]
    m, pw, sh = multiplicative(odds), power(odds), shin(odds)
    assert pw[2] < m[2] and sh[2] < m[2]  # longshot gets less fair prob
    assert pw[0] > m[0] and sh[0] > m[0]  # favourite gets more


def test_no_margin_is_identity():
    assert power([2.0, 2.0]) == pytest.approx([0.5, 0.5])
    assert overround([2.0, 2.0]) == pytest.approx(0.0)


def test_american():
    assert american_to_decimal(150) == pytest.approx(2.5)
    assert american_to_decimal(-200) == pytest.approx(1.5)
    with pytest.raises(ValueError):
        american_to_decimal(50)


def test_bad_inputs():
    with pytest.raises(ValueError):
        devig([1.0, 2.0])
    with pytest.raises(ValueError):
        devig([2.0, 2.0], "nope")
