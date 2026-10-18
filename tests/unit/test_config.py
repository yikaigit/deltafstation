"""配置层测试：深合并、点号取值、路径解析、环境变量覆盖。"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.config import Config, DEFAULTS, _deep_merge, load_config


class TestDeepMerge:
    def test_override_scalar(self):
        merged = _deep_merge({"a": 1, "b": 2}, {"b": 3})
        assert merged == {"a": 1, "b": 3}

    def test_nested_merge_keeps_siblings(self):
        base = {"x": {"p": 1, "q": 2}, "y": 9}
        merged = _deep_merge(base, {"x": {"q": 20}})
        assert merged == {"x": {"p": 1, "q": 20}, "y": 9}

    def test_does_not_mutate_inputs(self):
        base = {"x": {"p": 1}}
        override = {"x": {"p": 2}}
        _deep_merge(base, override)
        assert base == {"x": {"p": 1}}
        assert override == {"x": {"p": 2}}

    def test_none_override_is_noop(self):
        assert _deep_merge({"a": 1}, None) == {"a": 1}


class TestConfigGet:
    def test_dotted_access(self, cfg):
        assert cfg.get("backtest.is_ratio") == pytest.approx(0.7)
        assert cfg.get("costs.lot_size") == 100

    def test_missing_returns_default(self, cfg):
        assert cfg.get("no.such.key") is None
        assert cfg.get("no.such.key", "fallback") == "fallback"

    def test_scalar_branch_returns_default(self, cfg):
        # 走到非 dict 节点上再取子键，应安全返回默认值
        assert cfg.get("app.name.deeper", "x") == "x"

    def test_version_property(self, cfg):
        assert cfg.version == DEFAULTS["app"]["version"]


class TestPaths:
    def test_paths_resolved_under_root(self, tmp_path):
        config = load_config(root=tmp_path)
        assert config.paths.lake == tmp_path / "data" / "lake"
        assert config.paths.meta_db == tmp_path / "data" / "meta.db"

    def test_ensure_creates_directories(self, tmp_path):
        config = load_config(root=tmp_path)
        config.paths.ensure()
        for path in (config.paths.lake, config.paths.snapshots, config.paths.results, config.paths.quality):
            assert path.is_dir()
        assert config.paths.meta_db.parent.is_dir()


class TestEnvOverride:
    def test_host_port_from_env(self, cfg, monkeypatch):
        monkeypatch.setenv("DELTAF_HOST", "127.0.0.1")
        monkeypatch.setenv("DELTAF_PORT", "8123")
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8123

    @pytest.mark.parametrize(
        "value,expected",
        [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("false", False)],
    )
    def test_debug_flag_parsing(self, cfg, monkeypatch, value, expected):
        monkeypatch.setenv("DELTAF_DEBUG", value)
        assert cfg.debug is expected

    def test_debug_falls_back_to_yaml(self, cfg, monkeypatch):
        monkeypatch.delenv("DELTAF_DEBUG", raising=False)
        assert cfg.debug is bool(cfg.get("app.debug"))


class TestLoadFromFile:
    def test_partial_yaml_merges_defaults(self, tmp_path):
        path = tmp_path / "custom.yaml"
        path.write_text("app:\n  version: 9.9.9\n", encoding="utf-8")
        config = load_config(path=path, root=tmp_path)
        assert config.version == "9.9.9"
        # 未写的字段仍取默认值
        assert config.get("costs.commission") == DEFAULTS["costs"]["commission"]

    def test_missing_file_uses_defaults(self, tmp_path):
        config = load_config(path=tmp_path / "absent.yaml", root=tmp_path)
        assert isinstance(config, Config)
        assert config.get("backtest.default_top_n") == 20
