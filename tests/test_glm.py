import json
from types import SimpleNamespace

from studizba.config import GLMAccount
from studizba.glm import GLMProvider
from studizba.models import ReviewAnalysis


def test_glm_json_validation_accepts_evidence_metrics():
    result = ReviewAnalysis.model_validate({
        "language": "ru",
        "metrics": {"exam_difficulty": {"value": .8, "confidence": .9, "evidence": ["экзамен тяжёлый"], "context": ["exam"]}},
        "overall_confidence": .9,
    })
    assert result.metrics["exam_difficulty"].value == .8


def test_glm_json_extraction_handles_fences_and_thinking():
    raw = '<think>ignored</think>```json\n{"language":"ru","metrics":{},"overall_confidence":0}\n```'
    assert GLMProvider._extract_json(raw)["language"] == "ru"


def test_batch_item_evidence_is_validated_against_its_own_review():
    parsed = {
        "language": "ru",
        "metrics": {
            "exam_difficulty": {
                "value": .9,
                "confidence": .8,
                "evidence": ["экзамен очень тяжёлый", "чужая цитата"],
                "context": ["exam"],
            }
        },
        "overall_confidence": .8,
    }
    result = GLMProvider._validate_evidence("экзамен очень тяжёлый", parsed)
    assert result.metrics["exam_difficulty"].evidence == ["экзамен очень тяжёлый"]


def test_unknown_model_metric_is_dropped_without_losing_valid_metrics():
    parsed = {
        "language": "ru",
        "metrics": {
            "lecture_difficulty": {"value": .9, "confidence": .8, "evidence": ["лекции сложные"], "context": ["lecture"]},
            "teaching_quality": {"value": .2, "confidence": .7, "evidence": ["плохо объясняет"], "context": ["lecture"]},
        },
        "overall_confidence": .7,
    }
    result = GLMProvider._validate_evidence("плохо объясняет, лекции сложные", parsed)
    assert set(result.metrics) == {"teaching_quality"}


def test_zai_47_falls_back_to_45_in_order():
    config = SimpleNamespace(glm_api_key="", glm_model="glm-4.7-flash")
    account = GLMAccount("zai", "secret", "zai", "glm-4.7-flash")

    assert GLMProvider(config, account).models == ["glm-4.7-flash", "glm-4.5-flash"]


def test_non_zai_provider_does_not_get_zai_fallback():
    config = SimpleNamespace(glm_api_key="", glm_model="glm-4.7-flash")
    account = GLMAccount("other", "secret", "openrouter", "glm-4.7-flash")

    assert GLMProvider(config, account).models == ["glm-4.7-flash"]
