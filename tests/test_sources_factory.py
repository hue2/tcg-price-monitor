"""Price source factory (`sources.build_source`): name -> source, unknown names fail loudly. No network."""

from dataclasses import replace

import pytest

from tcgwatch import sources
from tcgwatch.config import ConfigError
from tcgwatch.sources import PriceSource, TcgCsvSource, build_source


def test_tcgcsv_is_built_with_the_configured_user_agent_and_delay(example_cfg):
    cfg = replace(example_cfg, user_agent="tcgwatch-test/1.0", request_delay_seconds=1.5)
    src = build_source("tcgcsv", cfg)
    assert isinstance(src, TcgCsvSource)
    assert src.user_agent == "tcgwatch-test/1.0"
    assert src.delay == 1.5


def test_each_call_returns_a_fresh_source(example_cfg):
    assert build_source("tcgcsv", example_cfg) is not build_source("tcgcsv", example_cfg)


def test_unknown_source_is_a_config_error_naming_it(example_cfg):
    with pytest.raises(ConfigError, match="unknown price source 'mtgjson'"):
        build_source("mtgjson", example_cfg)


def test_source_name_is_case_sensitive(example_cfg):
    with pytest.raises(ConfigError, match="unknown price source 'TCGCSV'"):
        build_source("TCGCSV", example_cfg)


def test_empty_source_name_is_rejected(example_cfg):
    with pytest.raises(ConfigError):
        build_source("", example_cfg)


def test_package_exports_the_public_names():
    assert set(sources.__all__) == {"PriceSource", "TcgCsvSource", "build_source"}
    assert sources.PriceSource is PriceSource
