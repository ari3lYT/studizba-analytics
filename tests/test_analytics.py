from datetime import datetime, timedelta, timezone

from studizba.analytics import bayesian, decay_weight, polarization_score


def test_bayesian_shrinkage_prevents_tiny_sample_overconfidence():
    score, confidence, n = bayesian([(1.0, 1.0)])
    assert n == 1
    assert .5 < score < .7
    assert confidence < .25


def test_recent_review_has_more_weight():
    now = datetime.now(timezone.utc)
    assert decay_weight(now, now) > decay_weight(now - timedelta(days=730), now)


def test_opposite_extremes_are_not_equivalent_to_moderate_reviews():
    split = [0.05, 0.1, 0.9, 0.95]
    moderate = [0.45, 0.48, 0.52, 0.55]
    assert polarization_score(split)["score"] > polarization_score(moderate)["score"] * 5
    assert polarization_score(split)["classification"] == "highly_polarizing"
    assert polarization_score(moderate)["classification"] == "stable"
