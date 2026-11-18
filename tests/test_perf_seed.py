"""Fast smoke test for the PERF-0 seeder: a tiny DB, so the slow benchmark's fixture cannot rot unnoticed."""

from pathlib import Path

import pytest

from perf_seed import GAMES, PRINTINGS, assert_config_targets, seed, write_config

from tcgwatch.config import load_config
from tcgwatch.store import Store


def test_seed_tiny_shape_and_config(tmp_path):
    db = tmp_path / "tiny.db"
    counts = seed(db, products_per_game=3, days=4, watch=5)

    assert counts["cards"] == 3 * len(GAMES)
    assert counts["printings"] == 3 * len(GAMES) * len(PRINTINGS)
    assert counts["price_rows"] == counts["printings"] * 4
    assert counts["watched"] == 5

    with Store(db, readonly=True) as store:
        assert store.snapshot_days("op") == 4
        assert len(store.watched("op")) + len(store.watched("riftbound")) + len(store.watched("pokemon")) == 5

    cfg = load_config(write_config(tmp_path, db))
    assert list(cfg.games) == [key for key, *_ in GAMES]
    assert cfg.database == db


def test_seed_refuses_to_overwrite(tmp_path):
    db = tmp_path / "tiny.db"
    seed(db, products_per_game=1, days=1, watch=0)
    with pytest.raises(FileExistsError):
        seed(db, products_per_game=1, days=1, watch=0)


def test_relative_outdir_config_points_at_seeded_db(tmp_path, monkeypatch):
    """Regression: a relative OUTDIR used to yield OUTDIR/OUTDIR/tcgwatch.db (an empty DB)."""
    monkeypatch.chdir(tmp_path)
    outdir = Path("relative-output")
    db = outdir / "tcgwatch.db"
    seed(db, products_per_game=1, days=1, watch=0)
    cfg_path = write_config(outdir, db)

    cfg = load_config(cfg_path)
    assert cfg.database == (tmp_path / "relative-output" / "tcgwatch.db").resolve()
    assert cfg.database.exists()
    assert_config_targets(cfg_path, db)


def test_config_target_guard_rejects_other_db(tmp_path):
    db = tmp_path / "seeded" / "tcgwatch.db"
    seed(db, products_per_game=1, days=1, watch=0)
    other = tmp_path / "other" / "tcgwatch.db"
    other.parent.mkdir()
    cfg_path = write_config(tmp_path, other)  # config targets a DB that was never seeded
    with pytest.raises(AssertionError):
        assert_config_targets(cfg_path, db)
