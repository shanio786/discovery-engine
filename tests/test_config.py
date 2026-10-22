from pathlib import Path

import pytest

from ode.config import ConfigError, load_config
from tests.conftest import make_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def test_shipped_config_is_valid():
    c = load_config(REPO_CONFIG)
    assert c.seeds and c.market_codes
    assert all(s.queries for s in c.seeds)


def test_queries_for_skips_seeds_without_that_language(config):
    de = config.queries_for(config.market("de-DE"))
    assert [(s.key, q) for s, q in de] == [("raspberry_pi", "raspberry pi projekt")]
    en = config.queries_for(config.market("en-US"))
    assert len(en) == 2


@pytest.mark.parametrize("override,msg", [
    ({"markets": []}, "at least one market"),
    ({"markets": ["english"]}, "<lang>-<REGION>"),
    ({"seeds": [{"key": "a"}, {"key": "a"}]}, "duplicate seed"),
    ({"seeds": [{"key": "Bad Key"}]}, "lowercase"),
    ({"seeds": [{"name": "no key"}]}, "needs a `key`"),
    ({"rate_limits": {"x": {"rate": 0}}}, "must be > 0"),
    ({"discovery": {"exclude_title_patterns": ["("]}}, "bad exclude_title_patterns"),
])
def test_invalid_config_fails_fast(override, msg):
    with pytest.raises(ConfigError, match=msg):
        make_config(**override)


def test_missing_config_file_is_clear(tmp_path):
    with pytest.raises(ConfigError, match="config not found"):
        load_config(tmp_path / "nope.yaml")
