from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DepartmentCard(BaseModel):
    source_key: str
    slug: str
    name: str
    url: str


class TeacherCard(BaseModel):
    source_id: int
    name: str
    url: str
    description: str | None = None
    overall_rating: float | None = None
    explanation_rating: float | None = None
    attitude_rating: float | None = None
    grades_rating: float | None = None
    reviews_count: int | None = None


class ReviewRecord(BaseModel):
    source_id: int
    parent_source_id: int | None = None
    kind: str | None = None
    author_name: str | None = None
    author_url: str | None = None
    body: str
    vote_rating: int | None = None
    published_at: datetime | None = None
    date_raw: str | None = None


class TeacherPage(BaseModel):
    source_id: int
    name: str
    url: str
    department_name: str | None = None
    department_url: str | None = None
    description: str | None = None
    title: str | None = None
    overall_rating: float | None = None
    explanation_rating: float | None = None
    attitude_rating: float | None = None
    grades_rating: float | None = None
    declared_reviews_count: int | None = None
    reviews: list[ReviewRecord] = Field(default_factory=list)


class EvidenceMetric(BaseModel):
    value: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    context: list[str] = Field(default_factory=list)

    @field_validator("evidence")
    @classmethod
    def nonempty_spans(cls, value: list[str]) -> list[str]:
        return [x.strip() for x in value if x and x.strip()]


class ReviewAnalysis(BaseModel):
    language: str = "ru"
    subject: str | None = None
    semester: str | None = None
    activity_types: list[str] = Field(default_factory=list)
    metrics: dict[str, EvidenceMetric] = Field(default_factory=dict)
    overall_confidence: float = Field(ge=0, le=1)
    notes: list[str] = Field(default_factory=list)

    @field_validator("metrics")
    @classmethod
    def known_metric_names(cls, value: dict[str, EvidenceMetric]) -> dict[str, EvidenceMetric]:
        unknown = set(value) - set(ANALYSIS_METRICS)
        if unknown:
            raise ValueError(f"unknown metrics: {sorted(unknown)}")
        return value


ANALYSIS_METRICS = [
    "explanation_quality", "teaching_quality", "helpfulness", "communication", "fairness", "respectfulness", "predictability",
    "workload", "homework_load", "lab_difficulty", "coursework_difficulty", "exam_difficulty", "credit_difficulty", "retake_difficulty",
    "attendance_strictness", "deadline_strictness", "grading_leniency", "ease_to_pass", "ease_to_get_good_grade", "automatic_grade_signal",
    "automatic_credit_signal", "not_oppressive", "rescues_students", "semester_points_closure", "resubmission_ease",
    "cheating_opportunity", "anti_cheating_strictness", "plagiarism_enforcement", "oral_defense_strictness", "copying_tolerance",
    "solution_reuse_tolerance", "exam_proctoring_strictness", "lab_authorship_check", "code_authorship_check", "cheating_ease",
    "allowed_notes", "allowed_internet", "allowed_materials", "open_book_exam",
]
