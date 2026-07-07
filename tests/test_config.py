"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stampla.config import (
    Config,
    ConfigError,
    DamConfig,
    SidecarDirRule,
    Tree,
    config_from_dict,
    load_config,
)
from stampla.naming import DEFAULT_RAW_EXTENSIONS

EXAMPLE = Path(__file__).parent.parent / "examples" / "config.toml"


class TestDefaults:
    def test_default_config_is_valid(self) -> None:
        config = Config()
        assert config.pattern.name == "md5-8"
        assert config.raw_extensions == DEFAULT_RAW_EXTENSIONS
        assert config.dam is None

    def test_grammar_includes_legacy_patterns(self) -> None:
        config = config_from_dict(
            {"pattern": {"name": "md5-12", "digest_length": 12, "legacy": [{"name": "md5-8"}]}}
        )
        assert [p.name for p in config.grammar.patterns] == ["md5-12", "md5-8"]

    def test_empty_dict_gives_defaults(self) -> None:
        assert config_from_dict({}) == Config()


class TestExampleFile:
    def test_example_config_loads(self) -> None:
        config = load_config(EXAMPLE)
        assert config.root == "/mnt/archive"
        assert [tree.media for tree in config.trees] == ["photo", "video"]
        assert config.timezone == "Europe/Warsaw"
        assert config.dam is not None
        assert config.dam.trees == ("Photos",)
        assert config.sidecar_dirs == (SidecarDirRule(subdir="NKSC_PARAM", strip=".nksc"),)


class TestValidation:
    def test_unknown_top_level_key(self) -> None:
        with pytest.raises(ConfigError, match=r"unknown key.*'patern'"):
            config_from_dict({"patern": {}})

    def test_unknown_nested_key(self) -> None:
        with pytest.raises(ConfigError, match="dates"):
            config_from_dict({"dates": {"timezona": "UTC"}})

    def test_bad_timezone(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            config_from_dict({"dates": {"timezone": "Mars/Olympus_Mons"}})

    def test_bad_media(self) -> None:
        with pytest.raises(ConfigError, match="media"):
            config_from_dict({"trees": [{"path": "Photos", "media": "audio"}]})

    @pytest.mark.parametrize("path", ["/Photos", "\\Photos", "C:/Photos", "C:\\Photos", "C:Photos"])
    def test_non_relative_tree_path(self, path: str) -> None:
        with pytest.raises(ConfigError, match="relative"):
            config_from_dict({"trees": [{"path": path, "media": "photo"}]})

    def test_duplicate_tree_paths(self) -> None:
        with pytest.raises(ConfigError, match="unique"):
            config_from_dict(
                {
                    "trees": [
                        {"path": "Photos", "media": "photo"},
                        {"path": "Photos", "media": "video"},
                    ]
                }
            )

    def test_unknown_layout_token(self) -> None:
        with pytest.raises(ConfigError, match="layout"):
            config_from_dict(
                {"trees": [{"path": "P", "media": "photo", "layout": "{year}/{month}"}]}
            )

    def test_invalid_pattern_reported_with_context(self) -> None:
        with pytest.raises(ConfigError, match="pattern"):
            config_from_dict({"pattern": {"name": "bad", "digest": "crc32"}})

    def test_dam_tree_must_exist(self) -> None:
        with pytest.raises(ConfigError, match="dam tree"):
            config_from_dict({"dam": {"trees": ["Nonexistent"]}})

    def test_empty_date_chain(self) -> None:
        with pytest.raises(ConfigError, match="date chain"):
            config_from_dict({"dates": {"photo": []}})

    def test_date_chain_must_be_strings(self) -> None:
        with pytest.raises(ConfigError, match=r"dates\.photo"):
            config_from_dict({"dates": {"photo": [1, 2]}})

    def test_sidecar_strip_needs_dot(self) -> None:
        with pytest.raises(ConfigError, match="strip"):
            config_from_dict({"sidecar_dirs": [{"subdir": "NKSC_PARAM", "strip": "nksc"}]})

    def test_dam_defaults(self) -> None:
        dam = DamConfig()
        assert dam.token_tag == "XMP-photoshop:TransmissionReference"


class TestLoadErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(tmp_path / "missing.toml")

    def test_invalid_toml(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.toml"
        broken.write_text("this is = not [ valid")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(broken)

    def test_error_includes_file_path(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text('[dates]\ntimezone = "Nowhere/Nothing"\n')
        with pytest.raises(ConfigError, match=r"bad\.toml"):
            load_config(bad)


class TestMergeSemantics:
    def test_partial_dates_section_keeps_other_defaults(self) -> None:
        config = config_from_dict({"dates": {"timezone": "Europe/Warsaw"}})
        assert config.timezone == "Europe/Warsaw"
        assert config.date_chain_photo == Config().date_chain_photo

    def test_partial_extensions_section(self) -> None:
        config = config_from_dict({"extensions": {"raw": ["nef"]}})
        assert config.raw_extensions == frozenset({"nef"})
        assert config.mutable_extensions == Config().mutable_extensions

    def test_tzinfo_property(self) -> None:
        config = config_from_dict({"dates": {"timezone": "Europe/Warsaw"}})
        assert str(config.tzinfo) == "Europe/Warsaw"


def make_tree(**overrides: Any) -> Tree:
    kwargs: dict[str, Any] = {"path": "Photos", "media": "photo"}
    kwargs.update(overrides)
    return Tree(**kwargs)


class TestTree:
    def test_layout_defaults(self) -> None:
        assert make_tree().layout == "{yyyy}/{yyyy}-{mm}"

    def test_day_layout_token_is_valid(self) -> None:
        assert make_tree(layout="{yyyy}/{mm}/{dd}").layout == "{yyyy}/{mm}/{dd}"
