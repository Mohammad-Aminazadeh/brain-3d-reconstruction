import pytest

from brainrec.catalog import Catalog


def _touch(d, names):
    for n in names:
        (d / n).write_text("")


def test_orders_numerically(tmp_path):
    _touch(tmp_path, ["a_10_H - x.ndpi", "a_2_H - x.ndpi", "a_1_H - x.ndpi", "note.txt"])
    assert [s.id for s in Catalog.scan(tmp_path).slices] == [1, 2, 10]


def test_range_selects_inclusive(tmp_path):
    _touch(tmp_path, [f"a_{i}_H - x.ndpi" for i in range(1, 6)])
    assert [s.id for s in Catalog.scan(tmp_path, (2, 4)).slices] == [2, 3, 4]


def test_missing_in_range_raises(tmp_path):
    _touch(tmp_path, ["a_1_H - x.ndpi", "a_3_H - x.ndpi"])
    with pytest.raises(ValueError, match="missing"):
        Catalog.scan(tmp_path, (1, 3))


def test_duplicate_raises(tmp_path):
    _touch(tmp_path, ["a_1_H - x.ndpi", "b_1_H - y.ndpi"])
    with pytest.raises(ValueError, match="duplicate"):
        Catalog.scan(tmp_path)


def test_empty_raises(tmp_path):
    with pytest.raises(ValueError, match="no slices"):
        Catalog.scan(tmp_path)
