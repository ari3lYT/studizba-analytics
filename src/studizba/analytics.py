from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Config
from .db import Database
from .glm import ANALYZER_VERSION
from .models import ANALYSIS_METRICS


METRIC_VERSION = "bayes-decay-v2"
PRIOR_MEAN = 0.5
PRIOR_STRENGTH = 4.0
HALF_LIFE_DAYS = 730.0

DERIVED = {
    "teaching_quality": [("teaching_quality", 1), ("explanation_quality", 1), ("helpfulness", 0.5), ("communication", 0.5)],
    "comfort": [("respectfulness", 1), ("helpfulness", 0.8), ("communication", 0.6), ("not_oppressive", 0.8), ("attendance_strictness", -0.35)],
    "fairness": [("fairness", 1), ("predictability", 0.5), ("respectfulness", 0.3)],
    "workload": [("workload", 1), ("homework_load", 0.8), ("coursework_difficulty", 0.5)],
    "ease_to_pass": [("ease_to_pass", 1), ("grading_leniency", 0.7), ("credit_difficulty", -0.6), ("retake_difficulty", -0.4), ("rescues_students", 0.5)],
    "ease_to_get_good_grade": [("ease_to_get_good_grade", 1), ("grading_leniency", 0.8), ("semester_points_closure", 0.5)],
    "grading_leniency": [("grading_leniency", 1), ("resubmission_ease", 0.5)],
    "exam_difficulty": [("exam_difficulty", 1), ("exam_proctoring_strictness", 0.2)],
    "lab_difficulty": [("lab_difficulty", 1), ("oral_defense_strictness", 0.4), ("lab_authorship_check", 0.3)],
    "attendance_strictness": [("attendance_strictness", 1)],
    "deadline_strictness": [("deadline_strictness", 1)],
    "automatic_grade_likelihood": [("automatic_grade_signal", 1), ("automatic_credit_signal", 0.8), ("semester_points_closure", 0.5)],
    "cheating_ease": [("cheating_ease", 1), ("cheating_opportunity", 1), ("copying_tolerance", 0.7), ("solution_reuse_tolerance", 0.6), ("anti_cheating_strictness", -0.8), ("exam_proctoring_strictness", -0.5)],
    "anti_cheating_strictness": [("anti_cheating_strictness", 1), ("exam_proctoring_strictness", 0.7), ("oral_defense_strictness", 0.5), ("lab_authorship_check", 0.5), ("code_authorship_check", 0.5)],
    "plagiarism_enforcement": [("plagiarism_enforcement", 1), ("lab_authorship_check", 0.4), ("code_authorship_check", 0.4)],
    "strictness": [("attendance_strictness", 0.6), ("deadline_strictness", 0.6), ("exam_difficulty", 0.6), ("lab_difficulty", 0.5), ("anti_cheating_strictness", 0.4), ("grading_leniency", -0.5)],
}


def decay_weight(published_at: datetime | None, now: datetime | None = None, half_life_days: float = HALF_LIFE_DAYS) -> float:
    if not published_at:
        return 0.7
    now = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age = max(0.0, (now - published_at).total_seconds() / 86400)
    return 0.5 ** (age / half_life_days)


def bayesian(values: Iterable[tuple[float, float]]) -> tuple[float, float, int]:
    vals = list(values)
    weight = sum(w for _, w in vals)
    score = (PRIOR_MEAN * PRIOR_STRENGTH + sum(v * w for v, w in vals)) / (PRIOR_STRENGTH + weight)
    confidence = min(1.0, weight / (PRIOR_STRENGTH + weight))
    return score, confidence, len(vals)


def polarization_score(review_scores: list[float]) -> dict[str, Any]:
    dispersion = min(1.0, statistics.pstdev(review_scores) * 2.4) if len(review_scores) >= 2 else 0.0
    high_share = sum(value >= .65 for value in review_scores) / max(1, len(review_scores))
    low_share = sum(value <= .35 for value in review_scores) / max(1, len(review_scores))
    split_balance = min(1.0, 4 * high_share * low_share)
    score = dispersion * (.5 + .5 * split_balance)
    classification = "highly_polarizing" if score >= .55 else "controversial" if score >= .25 else "stable"
    return {"score": score, "classification": classification, "dispersion": dispersion,
            "high_share": high_share, "low_share": low_share, "split_balance": split_balance}


class AnalyticsEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg)

    def _metric_observations(self, analyses: list[dict[str, Any]], components: list[tuple[str, float]], *, recent_days: int | None = None) -> list[tuple[float, float]]:
        now = datetime.now(timezone.utc)
        out: list[tuple[float, float]] = []
        for row in analyses:
            published = row["published_at"]
            if recent_days and published and (now - published).days > recent_days:
                continue
            result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
            metrics = result.get("metrics", {})
            for name, direction in components:
                m = metrics.get(name)
                if not m or not m.get("evidence"):
                    continue
                value = float(m["value"])
                if direction < 0:
                    value = 1 - value
                weight = abs(direction) * float(m["confidence"]) * decay_weight(published, half_life_days=self.cfg.analytics_half_life_days)
                out.append((value, weight))
        return out

    def calculate_teacher(self, teacher_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            analyses = conn.execute(
                """SELECT DISTINCT ON (r.id) a.result,r.published_at,r.id review_id
                   FROM review_analyses a JOIN reviews r ON r.id=a.review_id
                   WHERE r.teacher_id=%s AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash AND r.active
                   ORDER BY r.id,a.analyzed_at DESC""",
                (teacher_id, ANALYZER_VERSION),
            ).fetchall()
            result: dict[str, Any] = {}
            metric_map = {name: [(name, 1.0)] for name in ANALYSIS_METRICS}
            metric_map.update(DERIVED)
            for metric, components in metric_map.items():
                all_obs = self._metric_observations(analyses, components)
                recent_obs = self._metric_observations(analyses, components, recent_days=365)
                two_year_obs = self._metric_observations(analyses, components, recent_days=730)
                three_year_obs = self._metric_observations(analyses, components, recent_days=1095)
                all_score, confidence, observation_count = bayesian(all_obs)
                recent_score, _, _ = bayesian(recent_obs)
                evidence_reviews = len({row["review_id"] for row in analyses if any(
                    (row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])).get("metrics", {}).get(name, {}).get("evidence")
                    for name, _ in components
                )})
                details = {"components": components, "component_observations": observation_count,
                           "effective_weight": sum(w for _, w in all_obs), "prior_mean": PRIOR_MEAN,
                           "prior_strength": PRIOR_STRENGTH, "half_life_days": self.cfg.analytics_half_life_days,
                           "window_scores": {"last_1_year": recent_score, "last_2_years": bayesian(two_year_obs)[0],
                                             "last_3_years": bayesian(three_year_obs)[0], "all_time": all_score}}
                conn.execute(
                    """INSERT INTO teacher_metrics(teacher_id,metric,metric_version,score,recent_score,all_time_score,sample_size,evidence_count,confidence,filters,details)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(teacher_id,metric,metric_version) DO UPDATE SET calculated_at=now(),score=excluded.score,
                       recent_score=excluded.recent_score,all_time_score=excluded.all_time_score,sample_size=excluded.sample_size,
                       evidence_count=excluded.evidence_count,confidence=excluded.confidence,filters=excluded.filters,details=excluded.details""",
                    (teacher_id, metric, METRIC_VERSION, recent_score if recent_obs else all_score,
                     recent_score if recent_obs else None, all_score, evidence_reviews, evidence_reviews,
                     confidence, json.dumps({"analyzer_version": ANALYZER_VERSION}), json.dumps(details)),
                )
                result[metric] = {"score": recent_score if recent_obs else all_score, "all_time_score": all_score,
                                  "recent_score": recent_score if recent_obs else None, "sample_size": evidence_reviews,
                                  "evidence_count": evidence_reviews, "confidence": confidence}

            # Polarization measures disagreement between reviews, not variance
            # between unrelated dimensions in the same review.
            review_scores: list[float] = []
            positive = (("teaching_quality", 1.0), ("explanation_quality", 1.0), ("helpfulness", .7),
                        ("respectfulness", .7), ("fairness", .8), ("ease_to_pass", .35),
                        ("exam_difficulty", -.25), ("lab_difficulty", -.2))
            for row in analyses:
                payload = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
                values = []
                for name, direction in positive:
                    item = payload.get("metrics", {}).get(name)
                    if not item or not item.get("evidence"):
                        continue
                    value = float(item["value"]) if direction > 0 else 1 - float(item["value"])
                    values.append((value, abs(direction) * float(item["confidence"])))
                if values and sum(weight for _, weight in values) > 0:
                    review_scores.append(sum(value * weight for value, weight in values) / sum(weight for _, weight in values))
            polarization = polarization_score(review_scores)
            pol = polarization["score"]
            classification = polarization["classification"]
            pol_confidence = min(1.0, len(review_scores) / 8)
            conn.execute(
                """INSERT INTO teacher_metrics(teacher_id,metric,metric_version,score,recent_score,all_time_score,sample_size,evidence_count,confidence,filters,details)
                   VALUES(%s,'polarization',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(teacher_id,metric,metric_version) DO UPDATE SET calculated_at=now(),score=excluded.score,recent_score=excluded.recent_score,
                   all_time_score=excluded.all_time_score,sample_size=excluded.sample_size,evidence_count=excluded.evidence_count,confidence=excluded.confidence,details=excluded.details""",
                (teacher_id, METRIC_VERSION, pol, pol, pol, len(review_scores), len(review_scores), pol_confidence,
                 json.dumps({"analyzer_version": ANALYZER_VERSION}),
                 json.dumps({"method": "between-review experience dispersion with opposite-extremes balance",
                             "classification": classification, "review_scores": review_scores,
                             "dispersion": polarization["dispersion"], "high_share": polarization["high_share"],
                             "low_share": polarization["low_share"], "split_balance": polarization["split_balance"]})),
            )
            result["polarization"] = {"score": pol, "sample_size": len(review_scores),
                                      "confidence": pol_confidence, "classification": classification}
            return result

    def calculate_all_teachers(self) -> dict[str, int]:
        with self.db.connect() as conn:
            ids = [int(r["id"]) for r in conn.execute(
                """SELECT DISTINCT t.id FROM teachers t JOIN reviews r ON r.teacher_id=t.id
                   JOIN review_analyses a ON a.review_id=r.id WHERE t.active AND a.analyzer_version=%s""", (ANALYZER_VERSION,)
            ).fetchall()]
        for teacher_id in ids:
            self.calculate_teacher(teacher_id)
        return {"teachers": len(ids)}

    def calculate_departments(self) -> dict[str, int]:
        with self.db.connect() as conn:
            deps = conn.execute("SELECT id FROM departments WHERE active").fetchall()
            for dep in deps:
                rows = conn.execute(
                    """SELECT tm.metric,tm.score,tm.confidence,tm.teacher_id FROM teacher_metrics tm
                       JOIN teacher_departments td ON td.teacher_id=tm.teacher_id AND td.active
                       WHERE td.department_id=%s AND tm.metric_version=%s AND tm.confidence>0""", (dep["id"], METRIC_VERSION)
                ).fetchall()
                grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for row in rows:
                    grouped[row["metric"]].append(row)
                for metric, values in grouped.items():
                    score, confidence, n = bayesian((float(v["score"]), float(v["confidence"])) for v in values)
                    self._upsert_department_metric(conn, int(dep["id"]), metric, score, n, confidence, {"teacher_scores": [v["score"] for v in values]})
                teaching = grouped.get("teaching_quality", [])
                strictness = grouped.get("strictness", [])
                ease = grouped.get("ease_to_pass", [])
                by_teacher: dict[int, dict[str, float]] = defaultdict(dict)
                for metric, values in grouped.items():
                    for value in values:
                        by_teacher[int(value["teacher_id"])][metric] = float(value["score"])
                experiences = [v.get("teaching_quality", 0.5) * 0.55 + v.get("ease_to_pass", 0.5) * 0.45 for v in by_teacher.values()]
                variance = min(1.0, statistics.pstdev(experiences) * 3) if len(experiences) > 1 else 0
                bad = sum(1 for v in by_teacher.values() if v.get("teaching_quality", 0.5) < .35 or v.get("strictness", .5) > .75) / max(1, len(by_teacher))
                good = sum(1 for v in by_teacher.values() if v.get("teaching_quality", 0) > .7 or v.get("ease_to_pass", 0) > .75) / max(1, len(by_teacher))
                total_teachers = conn.execute("SELECT count(*) n FROM teacher_departments WHERE department_id=%s AND active", (dep["id"],)).fetchone()["n"]
                covered = len(by_teacher)
                coverage = covered / max(1, total_teachers)
                for metric, score in (("teacher_variance", variance), ("bad_draw_risk", bad), ("good_draw_probability", good), ("teacher_coverage", coverage)):
                    self._upsert_department_metric(conn, int(dep["id"]), metric, score, covered, min(1, covered / 8) * coverage, {"teachers_total": total_teachers, "teachers_with_analysis": covered})
                if teaching and ease:
                    tq = sum(float(x["score"]) for x in teaching) / len(teaching)
                    ep = sum(float(x["score"]) for x in ease) / len(ease)
                    quadrant = ("good_teaching" if tq >= .5 else "weak_teaching") + "+" + ("easy" if ep >= .5 else "hard")
                    self._upsert_department_metric(conn, int(dep["id"]), "ease_vs_quality", (tq + ep) / 2, len(by_teacher), coverage, {"teaching_quality": tq, "ease_to_pass": ep, "quadrant": quadrant})
                total_reviews = conn.execute(
                    """SELECT count(DISTINCT r.id) n FROM reviews r JOIN teacher_departments td ON td.teacher_id=r.teacher_id
                       WHERE td.department_id=%s AND td.active AND r.active""", (dep["id"],)
                ).fetchone()["n"]
                analyzed_reviews = conn.execute(
                    """SELECT count(DISTINCT r.id) n FROM reviews r JOIN review_analyses a ON a.review_id=r.id
                       JOIN teacher_departments td ON td.teacher_id=r.teacher_id
                       WHERE td.department_id=%s AND td.active AND r.active AND a.analyzer_version=%s""", (dep["id"], ANALYZER_VERSION)
                ).fetchone()["n"]
                review_coverage = analyzed_reviews / max(1, total_reviews)
                self._upsert_department_metric(conn, int(dep["id"]), "review_coverage", review_coverage, analyzed_reviews,
                                               min(1, total_reviews / 20), {"reviews_total": total_reviews, "reviews_analyzed": analyzed_reviews})
                cheat_values = grouped.get("cheating_ease", [])
                if cheat_values:
                    avg_cheat = sum(float(x["score"]) for x in cheat_values) / len(cheat_values)
                    anti = {int(x["teacher_id"]): float(x["score"]) for x in grouped.get("anti_cheating_strictness", [])}
                    plagiarism = grouped.get("plagiarism_enforcement", [])
                    oral = grouped.get("oral_defense_strictness", [])
                    high_anti = sum(1 for value in anti.values() if value >= .7) / max(1, len(anti))
                    weak_control = sum(1 for x in cheat_values if float(x["score"]) >= .7 and anti.get(int(x["teacher_id"]), .5) <= .4) / len(cheat_values)
                    self._upsert_department_metric(conn, int(dep["id"]), "cheating_environment", avg_cheat, len(cheat_values),
                        min(1, len(cheat_values) / 8) * coverage,
                        {"average_cheating_ease": avg_cheat, "high_anti_cheating_teacher_share": high_anti,
                         "weak_control_evidence_teacher_share": weak_control,
                         "plagiarism_enforcement": sum(float(x["score"]) for x in plagiarism) / len(plagiarism) if plagiarism else None,
                         "oral_defense_strictness": sum(float(x["score"]) for x in oral) / len(oral) if oral else None})
            return {"departments": len(deps)}

    @staticmethod
    def _upsert_department_metric(conn, dep_id: int, metric: str, score: float, n: int, confidence: float, details: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO department_metrics(department_id,metric,metric_version,score,sample_size,confidence,filters,details)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(department_id,metric,metric_version) DO UPDATE SET
               calculated_at=now(),score=excluded.score,sample_size=excluded.sample_size,confidence=excluded.confidence,filters=excluded.filters,details=excluded.details""",
            (dep_id, metric, METRIC_VERSION, score, n, confidence, json.dumps({"teacher_metric_version": METRIC_VERSION}), json.dumps(details)),
        )
