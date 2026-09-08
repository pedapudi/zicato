"""Authored values must retain their meaning before configuration is constructed."""

from dataclasses import fields, is_dataclass

import pytest
from jsonschema import Draft202012Validator

from zicato.core.configuration import ConfigurationError, dataclass_schema, dataclass_to_jsonable
from zicato.core.scoring_config import ScoringWeights
from zicato.workspace_loader import scoring_weights_from_dict


@pytest.mark.parametrize(
    "raw, path",
    [
        ({"promote_mragin": 0.7}, "promote_mragin"),
        ({"overfitting": {"enabled": "false"}}, "overfitting.enabled"),
        ({"proposer_quality": {"best_of_n": 2.8}}, "proposer_quality.best_of_n"),
        ({"overfitting": "disabled"}, "overfitting"),
    ],
)
def test_authored_scoring_rejects_values_before_lossy_conversion(raw, path):
    with pytest.raises(ConfigurationError) as caught:
        scoring_weights_from_dict(raw)
    assert caught.value.path == f"scoring.{path}"
    assert caught.value.category in {"type", "unknown"}
    assert list(Draft202012Validator(dataclass_schema(ScoringWeights)).iter_errors(raw))


@pytest.mark.parametrize(
    "raw, path, category",
    [
        (None, "", "type"),
        ({"pass_rate_monotonicity": 1}, ".pass_rate_monotonicity", "type"),
        ({"promote_margin": True}, ".promote_margin", "type"),
        ({"promote_margin": float("nan")}, ".promote_margin", "range"),
        ({"namespace_monotonicity": {"drift:": "false"}}, ".namespace_monotonicity.drift:", "type"),
        ({"severity_weights": {"critical": "10"}}, ".severity_weights.critical", "type"),
        ({"regression_test_command": ["python", 4]}, ".regression_test_command[1]", "type"),
        ({"tournament": {"structure": "invalid"}}, ".tournament.structure", "range"),
        ({"overfitting": {"holdout_fraction": 0}}, ".overfitting.holdout_fraction", "range"),
        ({"overfitting": {"holdout_fraction": 1}}, ".overfitting.holdout_fraction", "range"),
        ({"holdout_margin": -0.1}, ".holdout_margin", "range"),
    ],
)
def test_authored_scoring_reports_the_persisted_field_and_failure_category(raw, path, category):
    with pytest.raises(ConfigurationError) as caught:
        scoring_weights_from_dict(raw)
    assert (caught.value.path, caught.value.category) == (f"scoring{path}", category)


def test_every_nested_configuration_object_is_closed_in_decoder_and_schema():
    defaults = ScoringWeights()
    schema = dataclass_schema(ScoringWeights)
    serialized = dataclass_to_jsonable(defaults)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(serialized)

    def check(instance, raw, declared, path):
        assert declared["additionalProperties"] is False
        assert set(declared["properties"]) == set(raw)
        with pytest.raises(ConfigurationError) as caught:
            from zicato.core.configuration import authored_dataclass_from_json

            authored_dataclass_from_json(type(instance), {**raw, "misspelled": 1}, path=path)
        assert caught.value.path == f"{path}.misspelled"
        for field in fields(instance):
            key = field.metadata.get("persisted_name") or field.name
            value = getattr(instance, field.name)
            assert declared["properties"][key]["default"] == raw[key]
            if is_dataclass(value):
                check(value, raw[key], declared["properties"][key], f"{path}.{key}")

    check(defaults, serialized, schema, "scoring")


def test_declared_mapping_extensions_keep_valid_values():
    raw = {"tournament": {"params": {"extension": {"labels": ["one", "two"], "limit": 3}}}}
    Draft202012Validator(dataclass_schema(ScoringWeights)).validate(raw)
    assert dataclass_to_jsonable(scoring_weights_from_dict(raw))["tournament"] == {
        "structure": "racing",
        "params": raw["tournament"]["params"],
    }
