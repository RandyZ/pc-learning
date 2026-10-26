"""JSON exponent overflow is not a literal NaN/Infinity and must also fail closed."""
import pytest
from math_agent.json_framing import trim_completed_root_suffix, close_completed_containers


@pytest.mark.parametrize('raw', ['{"n":1e999}}', '{"n":-1e999}}',
                                 '{"nested":[1e999]}}'])
def test_exponent_overflow_is_not_recovered(raw):
    assert trim_completed_root_suffix(raw) is None
    assert close_completed_containers(raw) is None


def test_finite_exponents_remain_verbatim():
    text='{"n":1.25e-8,"large":1e100}'
    assert trim_completed_root_suffix(text+'}')==text
