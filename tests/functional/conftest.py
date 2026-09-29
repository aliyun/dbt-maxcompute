"""Plumbing that applies to every functional (live-project) suite.

Kept deliberately narrow: this observes which schema a test class is about to
use so the runner script can attribute its cleanup check to *this run*.  It does
not depend on the ``project`` fixture, so it cannot drag credentials or server
state into a test that never asked for them.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maxcompute_gating


@pytest.fixture(scope="class", autouse=True)
def record_test_schema_for_cleanup_audit(unique_schema):
    """Record the schema this class will use, when one is configured."""
    if maxcompute_gating.blocked_reason() is None:
        maxcompute_gating.record_schema(str(unique_schema))
    yield
