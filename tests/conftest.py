import os

import pytest
import yaml

# Import the fuctional fixtures as a plugin
# Note: fixtures with session scope need to be local

pytest_plugins = ["dbt.tests.fixtures.project"]


# The profile dictionary, used to write out profiles.yml
@pytest.fixture(scope="class")
def dbt_profile_target():
    filepath = os.environ.get(
        "DBT_PROFILE_PATH",
        os.path.join(os.path.dirname(__file__), "..", "dbt_profile.yml"),
    )
    with open(filepath, "r") as file:
        config = yaml.safe_load(file)
    return config
