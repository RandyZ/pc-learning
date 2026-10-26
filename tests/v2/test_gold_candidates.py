import hashlib
import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "benchmarks" / "gold"
SCHEMA = json.loads((GOLD / "schema.json").read_text(encoding="utf-8"))
EXPECTED_SUBQUESTIONS = {
    "01": {"1", "2①", "2②", "2③", "3"},
    "02": {"1", "2①", "2②", "2③"},
    "03": {"1", "2", "3①", "3②"},
    "04": {"1", "2①", "2②"},
    "05": {"1①", "1②", "2"},
}


def load_case(case_id):
    return json.loads((GOLD / f"{case_id}.json").read_text(encoding="utf-8"))


def assert_cross_field_contract(case, case_id):
    assert case["case_id"] == case_id
    ids = [item["id"] for item in case["subquestions"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_SUBQUESTIONS[case_id]
    assert case["question_image"]["path"] == f"tests/test-items/{case_id}.png"
    assert case["answer_image"]["path"] == f"tests/test-items/{case_id}答案.png"


@pytest.mark.parametrize("case_id", sorted(EXPECTED_SUBQUESTIONS))
def test_candidate_gold_contract_and_original_subquestion_coverage(case_id):
    case = load_case(case_id)
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    jsonschema.validate(case, SCHEMA)
    assert_cross_field_contract(case, case_id)

    for item in case["subquestions"]:
        assert item["proof_checkpoints"]
        assert all(item["obligations"][name] for name in ("domain", "cases", "endpoints"))
        if item["final_answer"]["status"] == "needs_review":
            assert item["final_answer"]["review_notes"]
            assert "verified" not in item["final_answer"]
            assert "human_confirmed" not in item["final_answer"]


@pytest.mark.parametrize("case_id", sorted(EXPECTED_SUBQUESTIONS))
def test_candidate_gold_images_exist_and_match_hash(case_id):
    case = load_case(case_id)
    for image in (case["question_image"], case["answer_image"]):
        path = ROOT / image["path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == image["sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_answer",
        "blank_proof",
        "needs_review_without_notes",
        "missing_required",
        "extra_field",
        "invalid_hash",
        "geometry_without_diagram",
        "geometry_without_interaction",
        "function_with_interaction",
    ],
)
def test_candidate_gold_schema_rejects_invalid_cases(mutation):
    case = deepcopy(load_case("02" if mutation.startswith("geometry") else "01"))
    item = case["subquestions"][0]
    if mutation == "empty_answer":
        item["final_answer"]["value"] = ""
    elif mutation == "blank_proof":
        item["proof_checkpoints"][0] = " \t\n"
    elif mutation == "needs_review_without_notes":
        item["final_answer"].update(status="needs_review", review_notes=[])
    elif mutation == "missing_required":
        del case["independent_recheck"]
    elif mutation == "extra_field":
        case["unexpected"] = True
    elif mutation == "invalid_hash":
        case["question_image"]["sha256"] = "z" * 64
    elif mutation == "geometry_without_diagram":
        item["diagram_requirements"] = []
    elif mutation == "geometry_without_interaction":
        item["interaction_requirements"] = []
    elif mutation == "function_with_interaction":
        item["interaction_requirements"] = ["不应存在"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(case, SCHEMA)


@pytest.mark.parametrize(
    "mutation", ["duplicate_id", "case_id_mismatch", "image_path_mismatch", "swapped_images"]
)
def test_candidate_gold_rejects_cross_field_mismatches(mutation):
    case = deepcopy(load_case("01"))
    if mutation == "duplicate_id":
        case["subquestions"][1]["id"] = case["subquestions"][0]["id"]
    elif mutation == "case_id_mismatch":
        case["case_id"] = "02"
    elif mutation == "image_path_mismatch":
        case["question_image"]["path"] = "tests/test-items/02.png"
    elif mutation == "swapped_images":
        case["question_image"], case["answer_image"] = case["answer_image"], case["question_image"]
    with pytest.raises(AssertionError):
        assert_cross_field_contract(case, "01")


def test_case_01_tangent_line_right_branch_roots_regression():
    case = load_case("01")
    item = next(item for item in case["subquestions"] if item["id"] == "3")
    checkpoint = next(
        checkpoint for checkpoint in item["proof_checkpoints"] if "x=(7±2√6)/4" in checkpoint
    )
    assert "均大于0" in checkpoint
