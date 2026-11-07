"""Configuration loading.

Resolution order for the base file: explicit path > $PPS_ATS_CONFIG > packaged default
(wheel installs) > ``config/default.toml`` found by walking up from this file (editable
installs / source checkouts). An override file only needs the keys it changes.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class LimitsSettings(BaseModel):
    max_file_size_mb: float = 25
    max_docx_uncompressed_mb: float = 200
    max_pages: int = 30


class TextSettings(BaseModel):
    min_text_chars: int = 30
    low_text_chars: int = 300
    low_word_count: int = 60
    garbled_ratio: float = 0.10
    min_alpha_ratio: float = 0.45


class ImageSettings(BaseModel):
    image_heavy_ratio: float = 0.5


class LayoutSettings(BaseModel):
    header_band_ratio: float = 0.07
    footer_band_ratio: float = 0.07
    min_gutter_width_pt: float = 14
    detect_tables: bool = True


class ClassifierSettings(BaseModel):
    resume_min_score: float = 4.0
    nonresume_min_score: float = 3.0
    weak_resume_score: float = 1.5
    substantial_words: int = 100
    max_resume_pages: int = 4
    negative_phrases: dict[str, float] = Field(default_factory=dict)


class SectionSettings(BaseModel):
    min_content_chars: int = 15
    max_heading_words: int = 6
    required: list[str] = Field(default_factory=list)
    canonical: dict[str, list[str]] = Field(default_factory=dict)
    placeholder_patterns: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- Milestone 2


class PlaceholderRule(BaseModel):
    id: str
    pattern: str
    ignore_case: bool = True
    mode: str = "anywhere"  # "anywhere" | "standalone" (the whole line must match)
    finding_class: str = "definite_defect"
    severity: str = "high"
    confidence: float = 0.9
    label: str
    correction: str


class PlaceholderSettings(BaseModel):
    rules: list[PlaceholderRule] = Field(default_factory=list)
    # "Label:" fields that need a value (on the same line or the next line).
    value_labels: list[str] = Field(default_factory=list)
    min_value_chars: int = 3


class CompletenessSettings(BaseModel):
    required: list[str] = Field(default_factory=lambda: ["contact", "education", "skills"])
    recommended: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    # A missing required section is a definite defect only when the document's structure was
    # recognised (at least this many other sections detected); otherwise it is uncertain.
    min_detected_for_definite: int = 3


class FormattingSettings(BaseModel):
    min_font_share: float = 0.03
    max_font_families: int = 3
    body_family_min_share: float = 0.10
    max_distinct_sizes: int = 6
    min_body_font_pt: float = 8.0
    very_small_font_pt: float = 6.5
    min_small_text_chars: int = 15
    line_spacing_tolerance: float = 0.35
    min_lines_for_spacing: int = 3
    large_gap_lines: float = 4.0
    trailing_whitespace_ratio: float = 0.45
    max_side_margin_pt: float = 100.0
    max_top_margin_pt: float = 120.0
    min_margin_pt: float = 18.0
    max_left_edge_clusters: int = 5
    narrow_gutter_pt: float = 24.0
    max_columns: int = 2
    table_min_rows: int = 3
    large_image_area_ratio: float = 0.15
    garbled_line_ratio: float = 0.3
    docx_large_spacing_pt: float = 36.0
    docx_max_line_spacing_values: int = 2
    icon_font_patterns: list[str] = Field(default_factory=list)


class IdentitySettings(BaseModel):
    filename_noise_tokens: list[str] = Field(default_factory=list)
    student_id_patterns: list[str] = Field(default_factory=list)
    filename_id_pattern: str = r"^(?:\d{5,15}|[a-z]{1,5}\d{4,13})$"
    min_name_token_len: int = 3
    name_search_lines: int = 6
    name_stopwords: list[str] = Field(default_factory=list)


class ContentSettings(BaseModel):
    vague_min_words: int = 12
    objective_min_words: int = 12
    objective_max_words: int = 120
    filler_min_hits: int = 3
    duplicate_min_chars: int = 25
    near_duplicate_ratio: float = 0.92
    min_description_chars: int = 15
    action_verbs: list[str] = Field(default_factory=list)
    weak_openers: list[str] = Field(default_factory=list)
    filler_phrases: list[str] = Field(default_factory=list)
    contribution_markers: list[str] = Field(default_factory=list)
    technology_markers: list[str] = Field(default_factory=list)
    degree_keywords: list[str] = Field(default_factory=list)
    institution_keywords: list[str] = Field(default_factory=list)
    certification_issuers: list[str] = Field(default_factory=list)
    incomplete_endings: list[str] = Field(default_factory=list)


class LanguageSettings(BaseModel):
    enabled: bool = True
    dictionary_language: str = "en"
    max_findings: int = 40
    min_word_length: int = 3
    extra_allowed_words: list[str] = Field(default_factory=list)
    skip_sections: list[str] = Field(default_factory=lambda: ["contact"])


class AnalysisSettings(BaseModel):
    max_findings_per_rule: int = 25
    placeholders: PlaceholderSettings = Field(default_factory=PlaceholderSettings)
    completeness: CompletenessSettings = Field(default_factory=CompletenessSettings)
    formatting: FormattingSettings = Field(default_factory=FormattingSettings)
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    content: ContentSettings = Field(default_factory=ContentSettings)
    language: LanguageSettings = Field(default_factory=LanguageSettings)


# --------------------------------------------------------------------------- Milestone 3


class CriterionSettings(BaseModel):
    label: str
    max_points: float = 10.0
    floor: float = 1.0  # lowest mark for a gradeable resume (rubric bands start at 1)
    ceiling: float = 10.0  # highest mark deterministic evidence alone may award
    ceiling_note: str = ""


class BandSettings(BaseModel):
    name: str  # "excellent" | "good" | "needs_improvement"
    min_score: float


class ScoringRuleSettings(BaseModel):
    points: float | None = None  # nominal deduction per finding (overrides class/severity)
    group: str | None = None  # dedupe/cap group (default: the finding category)
    max_count: int | None = None  # occurrences counted at most
    criterion: str | None = None  # re-route to another criterion ("none" = ignore)


class ScoringGroupSettings(BaseModel):
    max_points: float  # cap per criterion for this group


class AIScoringSettings(BaseModel):
    enabled: bool = False
    max_adjustment: float = 1.0  # AI may move a criterion by at most this many marks
    min_confidence: float = 0.7  # below this the AI proposal is recorded but not applied
    review_disagreement: float = 1.5  # |proposal| >= this => manual review
    redact_personal_data: bool = True


class ATSScoringSettings(BaseModel):
    base: float = 100.0
    severity_points: dict[str, float] = Field(default_factory=dict)  # for ats_impact findings
    rule_points: dict[str, float] = Field(default_factory=dict)
    max_per_rule: float = 15.0
    bands: dict[str, float] = Field(default_factory=lambda: {"good": 80.0, "fair": 60.0})


class ScoringSettings(BaseModel):
    rubric_version: str = "CA1-provisional-1"
    rounding: float = 0.5
    boundary_margin: float = 0.25
    scale_by_confidence: bool = True
    criteria: dict[str, CriterionSettings] = Field(default_factory=dict)
    bands: list[BandSettings] = Field(default_factory=list)
    class_points: dict[str, dict[str, float]] = Field(default_factory=dict)
    groups: dict[str, ScoringGroupSettings] = Field(default_factory=dict)
    default_group_cap: float = 3.0
    rules: dict[str, ScoringRuleSettings] = Field(default_factory=dict)
    # A section-level finding covers line-level findings (fnmatch patterns) in that section.
    supersedes: dict[str, list[str]] = Field(default_factory=dict)
    ai: AIScoringSettings = Field(default_factory=AIScoringSettings)
    ats: ATSScoringSettings = Field(default_factory=ATSScoringSettings)


# --------------------------------------------------------------------------- Milestone 4


class ReportSettings(BaseModel):
    annotate_pdf: bool = True  # write an annotated copy of each analysed PDF
    findings_report: str = "docx"  # separate findings PDF for: "docx" | "all" | "none"
    clip_margin_x: float = 2.0  # widen the finding's span box when searching for its text
    clip_margin_y: float = 1.0
    excel_text_limit: int = 500  # max characters per Excel text cell


class BatchSettings(BaseModel):
    workers: int = 1  # documents processed in parallel (separate processes)
    max_workers: int = 4  # hard upper bound for --workers
    max_tasks_per_child: int = 50  # recycle worker processes to bound memory
    ignore_names: list[str] = Field(
        default_factory=lambda: ["desktop.ini", "thumbs.db", ".ds_store"]
    )
    ignore_prefixes: list[str] = Field(default_factory=lambda: ["~$", "._"])
    # Output folders whose path contains one of these are treated as cloud-synced.
    synced_markers: list[str] = Field(
        default_factory=lambda: ["onedrive", "dropbox", "google drive", "icloud"]
    )


class Settings(BaseModel):
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    text: TextSettings = Field(default_factory=TextSettings)
    images: ImageSettings = Field(default_factory=ImageSettings)
    layout: LayoutSettings = Field(default_factory=LayoutSettings)
    classifier: ClassifierSettings = Field(default_factory=ClassifierSettings)
    sections: SectionSettings = Field(default_factory=SectionSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    reports: ReportSettings = Field(default_factory=ReportSettings)
    batch: BatchSettings = Field(default_factory=BatchSettings)
    source: str = "built-in"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default_config_text() -> tuple[str, str] | None:
    try:
        packaged = resources.files("pps_ats").joinpath("_default_config.toml")
        if packaged.is_file():
            return packaged.read_text(encoding="utf-8"), "packaged default"
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "default.toml"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8"), str(candidate)
    return None


def load_settings(path: str | Path | None = None) -> Settings:
    data: dict[str, Any] = {}
    sources: list[str] = []
    default = _default_config_text()
    if default is not None:
        data = tomllib.loads(default[0])
        sources.append(default[1])

    override_path = path or os.environ.get("PPS_ATS_CONFIG")
    if override_path:
        override_file = Path(override_path)
        if not override_file.is_file():
            raise FileNotFoundError(f"Config file not found: {override_file}")
        data = _deep_merge(data, tomllib.loads(override_file.read_text(encoding="utf-8")))
        sources.append(str(override_file))

    settings = Settings.model_validate(data)
    settings.source = " + ".join(sources) or "built-in"
    return settings


@lru_cache(maxsize=1)
def default_settings() -> Settings:
    return load_settings()
