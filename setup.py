#!/usr/bin/env python
import sys

# require a supported version of Python
if sys.version_info < (3, 8):
    print("Error: dbt does not support this version of Python.")
    print("Please upgrade to Python 3.8 or higher.")
    sys.exit(1)

try:
    from setuptools import find_namespace_packages
except ImportError:
    # the user has a downlevel version of setuptools.
    print("Error: dbt requires setuptools v40.1.0 or higher.")
    print('Please upgrade setuptools with "pip install --upgrade setuptools" and try again')
    sys.exit(1)

from pathlib import Path
from setuptools import setup


# pull the long description from the README
README = Path(__file__).parent / "README.md"

# used for this adapter's version and in determining the compatible dbt-core version
VERSION = Path(__file__).parent / "dbt/adapters/maxcompute/__version__.py"


def _dbt_maxcompute_version() -> str:
    """
    Pull the package version from the main package version file
    """
    attributes = {}
    exec(VERSION.read_text(), attributes)
    return attributes["version"]


package_name = "dbt-maxcompute"
description = """The MaxCompute adapter plugin for dbt"""

setup(
    name="dbt-maxcompute",
    version=_dbt_maxcompute_version(),
    description="The MaxCompute adapter plugin for dbt",
    long_description=README.read_text(),
    long_description_content_type="text/markdown",
    author="Alibaba Cloud MaxCompute Team",
    author_email="zhangdingxin.zdx@alibaba-inc.com",
    url="https://github.com/dingxin-tech/dbt-maxcompute",
    packages=find_namespace_packages(include=["dbt", "dbt.*"]),
    include_package_data=True,
    install_requires=[
        "dbt-common>=1.0.4,<2.0",
        "dbt-adapters>=1.1.1,<2.0",
        "pyodps",  # latest
        "pandas>=0.17.0",
        # add dbt-core to ensure backwards compatibility of installation, this is not a functional dependency
        "dbt-core>=1.8.0",
    ],
    zip_safe=False,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.9",
)
