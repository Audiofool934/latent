import json

import pytest

from latent.errors import ConfigurationError
from latent.models import RemoteAsset
from latent.storage import DEFAULT_CACHE_BUDGETS, CacheManager, StateStore


def put_preview(store, cache, name):
    asset = RemoteAsset(
        provider="test", remote_id=name, remote_path=f"/archive/{name}.ARW",
        name=f"{name}.ARW", size_bytes=10, write_time="2026-10-28T00:00:00Z",
        file_hashes={"test": name},
    )
    return cache.put(
        asset_id=store.upsert_asset(asset), variant="preview", fingerprint=asset.fingerprint,
        data=b"x" * 10, width=1, height=1,
    )[0]


def test_persisted_override_survives_reopen_and_controls_eviction(tmp_path, monkeypatch):
    monkeypatch.setitem(DEFAULT_CACHE_BUDGETS, "preview", 15)
    (tmp_path / "cache-budgets.json").write_text(json.dumps({"preview": 25}))
    with StateStore(tmp_path) as store:
        cache = CacheManager(store)
        assert cache.budgets["contact"] == 3 * 1024**3
        assert cache.budgets["temporary"] == 1024**3
        first = put_preview(store, cache, "first")
        second = put_preview(store, cache, "second")
        assert first.path.is_file() and second.path.is_file()
    with StateStore(tmp_path) as store:
        third = put_preview(store, CacheManager(store), "third")
        assert not first.path.exists()
        assert second.path.is_file() and third.path.is_file()
        assert store.cache_total("preview") == 20


def test_missing_settings_uses_defaults_and_explicit_budgets_take_precedence(tmp_path):
    with StateStore(tmp_path) as store:
        assert CacheManager(store).budgets == DEFAULT_CACHE_BUDGETS
        (tmp_path / "cache-budgets.json").write_text(json.dumps({"preview": 25}))
        cache = CacheManager(store, {"preview": 15})
        first = put_preview(store, cache, "first")
        second = put_preview(store, cache, "second")
        assert not first.path.exists() and second.path.is_file()
        (tmp_path / "cache-budgets.json").write_text("invalid")
        assert CacheManager(store, {"preview": 15}).budgets == {"preview": 15}


@pytest.mark.parametrize("settings", [
    "{", "[]", "null", '{"unknown": 20}', '{"preview": true}',
    '{"preview": "20"}', '{"preview": 20.0}', '{"preview": -1}',
])
def test_invalid_settings_preserve_existing_cache(tmp_path, settings):
    with StateStore(tmp_path) as store:
        entry = put_preview(store, CacheManager(store), "original")
        (tmp_path / "cache-budgets.json").write_text(settings)
        with pytest.raises(ConfigurationError, match="cache budgets"):
            CacheManager(store)
        assert entry.path.read_bytes() == b"x" * 10
        assert store.cache_total("preview") == 10


def test_unreadable_settings_do_not_fall_back_to_smaller_defaults(tmp_path):
    with StateStore(tmp_path) as store:
        entry = put_preview(store, CacheManager(store), "original")
        (tmp_path / "cache-budgets.json").mkdir()
        with pytest.raises(ConfigurationError, match="Cannot read cache budgets"):
            CacheManager(store)
        assert entry.path.is_file()
