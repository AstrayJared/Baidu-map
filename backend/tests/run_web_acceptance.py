"""Run offline Web/E8.3 regressions without loading repository credential files.

From backend: python tests/run_web_acceptance.py
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "life-circle-algorithm/src")]
os.environ["BAIDU_MAP_AK"] = ""
os.environ["ANALYSIS_PROVIDER"] = "synthetic"
os.environ.pop("ANALYSIS_QPS", None)

from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource

read_env = DotEnvSettingsSource._static_read_env_file
protected = {(ROOT / "backend/.env").resolve(), (ROOT / "life-circle-demo/.env.local").resolve()}


def safe_env(file_path, **kwargs):
    return {} if Path(file_path).resolve() in protected else read_env(file_path, **kwargs)


DotEnvSettingsSource._static_read_env_file = staticmethod(safe_env)

import pytest

paths = [
    "backend/tests/test_analyses.py", "backend/tests/test_facilities.py",
    "backend/tests/test_web_e83_contract.py", "backend/tests/test_poi_evidence.py", "backend/tests/test_business_api.py", "backend/tests/test_endpoint_observations.py",
    "backend/tests/test_endpoint_e83_poi.py", "backend/tests/test_endpoint_e83_guidance.py",
    "backend/tests/test_endpoint_e83_sampling.py", "backend/tests/test_endpoint_e83_experiment_budget.py",
    "life-circle-algorithm/tests",
]
os.chdir(ROOT / "backend")
artifacts = ROOT / ".tmp/final-integration"
sys.exit(pytest.main([*[str(ROOT / path) for path in paths], "-q", "--tb=short",
    "-o", f"cache_dir={artifacts}/pytest-cache", f"--basetemp={artifacts}/pytest-temp-final",
    f"--junitxml={artifacts}/pytest.xml"]))
