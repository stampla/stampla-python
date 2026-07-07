"""Configuration: what an archive looks like and how to name it.

Configuration is layered: built-in defaults, overridden by a TOML file,
overridden by command-line options. Tables merge key by key; arrays and
scalars replace their default wholesale. Unknown keys are rejected so a
typo cannot silently disable an option.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stampla.naming import DEFAULT_RAW_EXTENSIONS, Grammar
from stampla.pattern import DEFAULT_PATTERN, NamingPattern, PatternError

_LAYOUT_TOKENS = frozenset({"yyyy", "mm", "dd"})

DEFAULT_MUTABLE_EXTENSIONS = frozenset(
    {
        # image formats commonly edited in place
        "dng",
        "tif",
        "tiff",
        "jpg",
        "jpeg",
        "psd",
        # sidecars change with every edit by design
        "xmp",
        "pp3",
        "acr",
        "nksc",
        "fp2",
        "fp3",
        "vrd",
        "spd",
    }
)

DEFAULT_VIDEO_EXTENSIONS = frozenset(
    {
        "mov",
        "mp4",
        "m4v",
        "avi",
        "mkv",
        "braw",
        "nev",
        "r3d",
        "mts",
        "m2ts",
        "3gp",
        "wmv",
        "asf",
        "mpg",
        "mpeg",
    }
)

DEFAULT_DATE_CHAIN_PHOTO = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "XMP:DateCreated",
)

# MakerNotes values are local wall-clock time; QuickTime values are usually
# UTC but some formats (e.g. BRAW) store local time there and offer nothing
# else, hence QuickTime last.
DEFAULT_DATE_CHAIN_VIDEO = (
    "DateTimeOriginal",
    "CreateDate",
    "QuickTime:CreateDate",
)


class ConfigError(ValueError):
    """Invalid or unreadable configuration."""


def _layout_tokens_of(layout: str) -> list[str]:
    return re.findall(r"\{([^}]*)\}", layout)


@dataclass(frozen=True)
class Tree:
    """One archive subtree holding one kind of media."""

    path: str
    media: Literal["photo", "video"]
    layout: str = "{yyyy}/{yyyy}-{mm}"

    def __post_init__(self) -> None:
        # Windows path semantics catch "/x", "\\x", "C:\\x" and "C:x" on
        # every platform; Path.is_absolute() would miss "/x" on Windows.
        if not self.path or PureWindowsPath(self.path).anchor:
            raise ConfigError(f"tree path must be relative to the archive root: {self.path!r}")
        if self.media not in ("photo", "video"):
            raise ConfigError(f"tree media must be 'photo' or 'video': {self.media!r}")
        unknown = {token for token in _layout_tokens_of(self.layout) if token not in _LAYOUT_TOKENS}
        if unknown:
            raise ConfigError(
                f"unknown layout token(s) {sorted(unknown)} in {self.layout!r};"
                f" available: {sorted(_LAYOUT_TOKENS)}"
            )


@dataclass(frozen=True)
class SidecarDirRule:
    """Sidecars kept in a subdirectory next to their masters.

    A file ``<subdir>/<master-name><strip>`` belongs to the master named
    ``<master-name>`` one level up.
    """

    subdir: str
    strip: str

    def __post_init__(self) -> None:
        if not self.subdir or "/" in self.subdir or "\\" in self.subdir:
            raise ConfigError(f"sidecar subdir must be a plain directory name: {self.subdir!r}")
        if not self.strip.startswith("."):
            raise ConfigError(f"sidecar strip must start with a dot: {self.strip!r}")


@dataclass(frozen=True)
class DamConfig:
    """Integration with a DAM that renames its managed masters itself."""

    token_tag: str = "XMP-photoshop:TransmissionReference"
    trees: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.token_tag:
            raise ConfigError("dam token_tag must not be empty")


@dataclass(frozen=True)
class Config:
    """Complete, validated configuration."""

    trees: tuple[Tree, ...] = (
        Tree(path="Photos", media="photo"),
        Tree(path="Video", media="video"),
    )
    pattern: NamingPattern = DEFAULT_PATTERN
    legacy_patterns: tuple[NamingPattern, ...] = ()
    timezone: str = "UTC"
    date_chain_photo: tuple[str, ...] = DEFAULT_DATE_CHAIN_PHOTO
    date_chain_video: tuple[str, ...] = DEFAULT_DATE_CHAIN_VIDEO
    raw_extensions: frozenset[str] = DEFAULT_RAW_EXTENSIONS
    video_extensions: frozenset[str] = DEFAULT_VIDEO_EXTENSIONS
    mutable_extensions: frozenset[str] = DEFAULT_MUTABLE_EXTENSIONS
    sidecar_dirs: tuple[SidecarDirRule, ...] = (SidecarDirRule(subdir="NKSC_PARAM", strip=".nksc"),)
    excludes: tuple[str, ...] = ()
    #: card files matching these globs are ignored (and listed) on import
    import_ignore: tuple[str, ...] = ()
    #: drop a JPEG whose RAW twin is in the same group; standalone JPEGs
    #: still import, so a JPEG-only photo can never be lost
    skip_jpeg_twins: bool = False
    dam: DamConfig | None = None
    root: str | None = None

    def __post_init__(self) -> None:
        if not self.trees:
            raise ConfigError("at least one tree is required")
        paths = [tree.path for tree in self.trees]
        if len(set(paths)) != len(paths):
            raise ConfigError("tree paths must be unique")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"unknown timezone {self.timezone!r}") from exc
        if not self.date_chain_photo or not self.date_chain_video:
            raise ConfigError("date chains must not be empty")
        if self.dam is not None:
            known = set(paths)
            for tree_path in self.dam.trees:
                if tree_path not in known:
                    raise ConfigError(f"dam tree {tree_path!r} is not a configured tree")

    @property
    def grammar(self) -> Grammar:
        return Grammar(
            patterns=(self.pattern, *self.legacy_patterns),
            raw_extensions=self.raw_extensions,
        )

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: Path) -> Config:
    """Load configuration from a TOML file layered over the defaults."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    try:
        return config_from_dict(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def config_from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from parsed TOML data, strictly validated."""
    known_sections = {
        "trees",
        "pattern",
        "dates",
        "extensions",
        "sidecar_dirs",
        "excludes",
        "import",
        "dam",
        "root",
    }
    _reject_unknown_keys(data, known_sections, context="top level")

    kwargs: dict[str, Any] = {}
    if "root" in data:
        kwargs["root"] = _expect(data["root"], str, "root")
    if "trees" in data:
        kwargs["trees"] = tuple(
            _tree_from_dict(entry, index) for index, entry in enumerate(data["trees"])
        )
    if "pattern" in data:
        current, legacy = _patterns_from_dict(_expect(data["pattern"], dict, "pattern"))
        kwargs["pattern"] = current
        kwargs["legacy_patterns"] = legacy
    if "dates" in data:
        kwargs.update(_dates_from_dict(_expect(data["dates"], dict, "dates")))
    if "extensions" in data:
        kwargs.update(_extensions_from_dict(_expect(data["extensions"], dict, "extensions")))
    if "sidecar_dirs" in data:
        kwargs["sidecar_dirs"] = tuple(
            _sidecar_rule_from_dict(entry, index)
            for index, entry in enumerate(data["sidecar_dirs"])
        )
    if "excludes" in data:
        kwargs["excludes"] = tuple(_string_list(data["excludes"], "excludes"))
    if "import" in data:
        kwargs.update(_import_from_dict(_expect(data["import"], dict, "import")))
    if "dam" in data:
        kwargs["dam"] = _dam_from_dict(_expect(data["dam"], dict, "dam"))
    return Config(**kwargs)


def _tree_from_dict(data: Any, index: int) -> Tree:
    table = _expect(data, dict, f"trees[{index}]")
    _reject_unknown_keys(table, {"path", "media", "layout"}, context=f"trees[{index}]")
    try:
        return Tree(**table)
    except TypeError as exc:
        raise ConfigError(f"trees[{index}]: {exc}") from exc


def _patterns_from_dict(data: dict[str, Any]) -> tuple[NamingPattern, tuple[NamingPattern, ...]]:
    _reject_unknown_keys(
        data,
        {"name", "datetime_format", "digest", "digest_length", "separator", "legacy"},
        context="pattern",
    )
    data = dict(data)
    legacy_entries = data.pop("legacy", [])
    try:
        current = NamingPattern(**data)
        legacy = tuple(
            NamingPattern(**_expect(entry, dict, f"pattern.legacy[{index}]"))
            for index, entry in enumerate(legacy_entries)
        )
    except (PatternError, TypeError) as exc:
        raise ConfigError(f"pattern: {exc}") from exc
    return current, legacy


def _dates_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(data, {"timezone", "photo", "video"}, context="dates")
    result: dict[str, Any] = {}
    if "timezone" in data:
        result["timezone"] = _expect(data["timezone"], str, "dates.timezone")
    if "photo" in data:
        result["date_chain_photo"] = tuple(_string_list(data["photo"], "dates.photo"))
    if "video" in data:
        result["date_chain_video"] = tuple(_string_list(data["video"], "dates.video"))
    return result


def _extensions_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(data, {"raw", "video", "mutable"}, context="extensions")
    result: dict[str, Any] = {}
    if "raw" in data:
        result["raw_extensions"] = frozenset(_string_list(data["raw"], "extensions.raw"))
    if "video" in data:
        result["video_extensions"] = frozenset(_string_list(data["video"], "extensions.video"))
    if "mutable" in data:
        result["mutable_extensions"] = frozenset(
            _string_list(data["mutable"], "extensions.mutable")
        )
    return result


def _import_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(data, {"ignore", "skip_jpeg_twins"}, context="import")
    result: dict[str, Any] = {}
    if "ignore" in data:
        result["import_ignore"] = tuple(_string_list(data["ignore"], "import.ignore"))
    if "skip_jpeg_twins" in data:
        value = data["skip_jpeg_twins"]
        if not isinstance(value, bool):
            raise ConfigError("import.skip_jpeg_twins must be a boolean")
        result["skip_jpeg_twins"] = value
    return result


def _sidecar_rule_from_dict(data: Any, index: int) -> SidecarDirRule:
    table = _expect(data, dict, f"sidecar_dirs[{index}]")
    _reject_unknown_keys(table, {"subdir", "strip"}, context=f"sidecar_dirs[{index}]")
    try:
        return SidecarDirRule(**table)
    except TypeError as exc:
        raise ConfigError(f"sidecar_dirs[{index}]: {exc}") from exc


def _dam_from_dict(data: dict[str, Any]) -> DamConfig:
    _reject_unknown_keys(data, {"token_tag", "trees"}, context="dam")
    kwargs: dict[str, Any] = {}
    if "token_tag" in data:
        kwargs["token_tag"] = _expect(data["token_tag"], str, "dam.token_tag")
    if "trees" in data:
        kwargs["trees"] = tuple(_string_list(data["trees"], "dam.trees"))
    return DamConfig(**kwargs)


def _reject_unknown_keys(data: dict[str, Any], known: set[str], context: str) -> None:
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in {context}: {sorted(unknown)}")


def _expect(value: Any, kind: type, context: str) -> Any:
    if not isinstance(value, kind):
        raise ConfigError(f"{context} must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _string_list(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{context} must be a list of strings")
    return value
