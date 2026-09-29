import os
import sys

import pytest

# tests/ is not guaranteed to be importable as a package for every way pytest
# can be started, so make the helper import independent of the invocation.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from maxcompute_gating import blocked_reason, load_profile

# Import the functional fixtures as a plugin
# Note: fixtures with session scope need to be local
pytest_plugins = ["dbt.tests.fixtures.project"]


# The profile dictionary, used to write out profiles.yml
@pytest.fixture(scope="class")
def dbt_profile_target():
    """Profile target for tests that talk to a real MaxCompute project.

    Missing credentials are a *skipped* run, never a passing one: the reason
    computed by :mod:`maxcompute_gating` is reported by pytest for every case.
    """
    reason = blocked_reason()
    if reason:
        pytest.skip(f"MaxCompute integration not run: {reason}")
    return load_profile()
