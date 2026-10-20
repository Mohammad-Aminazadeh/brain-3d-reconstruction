import json
import math

import numpy as np
import pytest

from brainrec.registration import Transform2D, px_to_um, um_to_px
from brainrec.registration.transform import SCHEMA

MPP = (0.2205, 0.2205)
RNG = np.random.default_rng(7)
PTS = RNG.uniform(0, 30_000, size=(50, 2))  # µm, about the size of a section


def test_identity():
    t = Transform2D.identity()
    assert np.allclose(t.apply(PTS), PTS)
    assert t.scale == 1.0 and t.angle_deg == 0.0 and t.translation_um == (0.0, 0.0)


def test_similarity_parameters_and_direction():
    t = Transform2D.similarity(math.radians(90), scale=2.0, tx_um=10.0, ty_um=0.0)
    assert t.angle_deg == pytest.approx(90.0)
    assert t.scale == pytest.approx(2.0)
    # y points down: +90 degrees turns the x axis onto +y, i.e. clockwise on screen
    assert np.allclose(t.apply(np.array([[1.0, 0.0]])), [[10.0, 2.0]])


def test_inverse_round_trip():
    t = Transform2D.similarity(0.3, 1.07, 1200.0, -450.0)
    assert t.inverse().compose(t).allclose(Transform2D.identity())
    assert np.allclose(t.inverse().apply(t.apply(PTS)), PTS)


def test_composition_order():
    a = Transform2D.similarity(0.4, 1.1, 100.0, 0.0)
    b = Transform2D.similarity(-1.2, 0.9, 0.0, 500.0)
    assert np.allclose(a.compose(b).apply(PTS), a.apply(b.apply(PTS)))  # b first, then a
    assert not a.compose(b).allclose(b.compose(a))  # order matters


def test_chain_of_many_stays_a_valid_similarity():
    pairs = [
        Transform2D.similarity(0.01 * k, 1.0 + 0.001 * k, 10.0 * k, -5.0 * k) for k in range(140)
    ]
    chained = Transform2D.identity()
    for p in pairs:
        chained = chained.compose(p)  # validation would raise if round-off broke the structure
    expected = PTS
    for p in reversed(pairs):
        expected = p.apply(expected)
    assert np.allclose(chained.apply(PTS), expected, atol=1e-6)


def test_family_rules():
    r = Transform2D.rigid(0.5, 1.0, 2.0)
    assert r.compose(Transform2D.rigid(-0.2)).family == "rigid"
    assert r.compose(Transform2D.similarity(0.0, 1.5)).family == "similarity"
    assert r.inverse().family == "rigid"
    with pytest.raises(ValueError, match="scale 1"):
        Transform2D.from_matrix([[2.0, 0.0, 0.0], [0.0, 2.0, 0.0]], family="rigid")


@pytest.mark.parametrize(
    "matrix, message",
    [
        ([[1.0, 0.0, 0.0], [0.0, 1.2, 0.0]], "not a similarity"),  # anisotropic scale
        ([[1.0, 0.3, 0.0], [0.0, 1.0, 0.0]], "not a similarity"),  # shear
        ([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0]], "not a similarity"),  # mirror in y
        ([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]], "not a similarity"),  # degenerate
        ([[np.nan, 0.0, 0.0], [0.0, 1.0, 0.0]], "non-finite"),
        ([[1.0, 0.0], [0.0, 1.0]], "shape"),
        ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.1, 0.0, 1.0]], "last row"),
    ],
)
def test_invalid_matrices_rejected(matrix, message):
    with pytest.raises(ValueError, match=message):
        Transform2D.from_matrix(np.array(matrix))


def test_unknown_family_rejected():
    with pytest.raises(ValueError, match="family"):
        Transform2D(np.eye(2, 3), family="affine")  # type: ignore[arg-type]


def test_matrix_is_read_only_copy():
    t = Transform2D.similarity(0.2, 1.0, 5.0, 5.0)
    m = t.matrix
    m[0, 2] = 999.0
    assert t.translation_um == (5.0, 5.0)


def test_json_round_trip(tmp_path):
    t = Transform2D.similarity(-0.071, 0.9973, 1515.7, -1061.1)
    path = tmp_path / "sub" / "pair_2.json"
    t.save(path)
    d = json.loads(path.read_text())
    assert set(d) == {"schema", "family", "matrix_um"} and d["schema"] == SCHEMA
    back = Transform2D.load(path)
    assert back.family == "similarity" and np.array_equal(back.matrix, t.matrix)


def test_unknown_schema_rejected():
    with pytest.raises(ValueError, match="schema"):
        Transform2D.from_dict({"schema": "other/9", "family": "rigid", "matrix_um": np.eye(2, 3)})


def test_half_pixel_convention():
    # centre of the first 32x pixel is 0.5 * 32 * mpp µm from the corner ...
    assert np.allclose(px_to_um(np.array([[0.0, 0.0]]), 32, MPP), [[16 * 0.2205, 16 * 0.2205]])
    # ... which is pixel coordinate 3.5 at 4x and -0.25 at 64x (C1)
    centre = px_to_um(np.array([[0.0, 0.0]]), 32, MPP)
    assert np.allclose(um_to_px(centre, 4, MPP), [[3.5, 3.5]])
    assert np.allclose(um_to_px(centre, 64, MPP), [[-0.25, -0.25]])
    assert np.allclose(um_to_px(px_to_um(PTS, 32, MPP), 32, MPP), PTS)


@pytest.mark.parametrize("mpp", [MPP, (0.25, 0.30)])
@pytest.mark.parametrize("src, dst", [(4, 4), (32, 4), (64, 32), (32, 64), (4, 64)])
def test_to_pixels_consistent_across_levels(src, dst, mpp):
    t = Transform2D.similarity(0.12, 1.03, 2500.0, -800.0)
    m = t.to_pixels(src, dst, mpp)
    src_px = um_to_px(PTS, src, mpp)
    dst_px = src_px @ m[:2, :2].T + m[:2, 2]
    assert np.allclose(px_to_um(dst_px, dst, mpp), t.apply(PTS), atol=1e-6)


def test_identity_to_pixels_between_levels():
    m = Transform2D.identity().to_pixels(32, 4, MPP)
    # one 32x pixel spans 8 pixels at 4x; centres line up with the half-pixel shift
    assert np.allclose(m, [[8.0, 0.0, 3.5], [0.0, 8.0, 3.5], [0.0, 0.0, 1.0]])


@pytest.mark.parametrize("src, dst", [(32, 32), (64, 32), (32, 4)])
def test_from_pixels_inverts_to_pixels(src, dst):
    t = Transform2D.similarity(-0.05, 0.92, -300.0, 1800.0)
    back = Transform2D.from_pixels(t.to_pixels(src, dst, MPP), src, dst, MPP)
    assert back.allclose(t)
    assert Transform2D.from_pixels(t.to_pixels(src, dst, MPP)[:2], src, dst, MPP).allclose(t)


def test_repr_mentions_parameters():
    r = repr(Transform2D.similarity(math.radians(3.0), 1.01, 1.0, 2.0))
    assert "similarity" in r and "3.0000 deg" in r and "1.010000" in r
