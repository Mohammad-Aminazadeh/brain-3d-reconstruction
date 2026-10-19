from brainrec.store import Manifest


def test_roundtrip(tmp_path):
    p = tmp_path / "sub" / "m.json"
    Manifest(p).set("k", {"done": [1, 2]})
    assert Manifest(p).get("k") == {"done": [1, 2]}
    assert Manifest(p).get("missing", 7) == 7
