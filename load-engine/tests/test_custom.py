"""Tests for the custom-profile store (incl. path-traversal protection)."""

import json
import os

import pytest

from app.profiles.custom import CustomProfileError, CustomProfileStore, resample_shape


@pytest.fixture
def store(tmp_path):
    return CustomProfileStore(str(tmp_path))


def test_save_and_get_roundtrip(store):
    store.save("bakery", [0.2, 0.5, 1.0, 0.4], "test")
    shape = store.get_shape("bakery", 96)
    assert len(shape) == 96
    assert abs(shape.max() - 1.0) < 1e-9  # normalised to per-unit peak


def test_get_shape_rejects_path_traversal(store):
    with pytest.raises(CustomProfileError):
        store.get_shape("../../../etc/passwd", 96)
    with pytest.raises(CustomProfileError):
        store.get_shape("..\\..\\evil", 96)


def test_delete_rejects_path_traversal(tmp_path):
    # A sibling .json outside the profiles dir must not be deletable via "..".
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"name": "secret", "values": [1, 2]}))
    profiles_dir = tmp_path / "profiles"
    store = CustomProfileStore(str(profiles_dir))

    with pytest.raises(CustomProfileError):
        store.delete("../secret")
    assert outside.exists()  # untouched


def test_unknown_name_raises_not_traversal(store):
    with pytest.raises(CustomProfileError):
        store.get_shape("does_not_exist", 96)


def test_resample_is_periodic_and_length_correct():
    out = resample_shape([0.0, 1.0, 0.0, 1.0], 96)
    assert len(out) == 96
