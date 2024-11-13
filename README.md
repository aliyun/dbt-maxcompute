<p align="left">
  <img src="./icon_MaxCompute.svg" alt="MaxCompute logo" width="300" height="150" style="margin-right: 100px;"/>
  <img src="https://raw.githubusercontent.com/dbt-labs/dbt/ec7dee39f793aa4f7dd3dae37282cc87664813e4/etc/dbt-logo-full.svg" alt="dbt logo" width="300" height="150"/>
</p>

# dbt-maxcompute

Welcome to the **dbt-maxCompute** repository! This project aims to extend the capabilities of **dbt** (data build tool)
for users of Alibaba MaxCompute, a cutting-edge data processing platform.

## What is dbt?

**[dbt](https://www.getdbt.com/)** empowers data analysts and engineers to transform their data using software
engineering best practices. It serves as the **T** in the ELT (Extract, Load, Transform) process, allowing users to
organize, cleanse, denormalize, filter, rename, and pre-aggregate raw data, making it analysis-ready.

## About MaxCompute

MaxCompute is Alibaba Group's cloud data warehouse and big data processing platform, supporting massive data storage and
computation, widely used for data analysis and business intelligence. With MaxCompute, users can efficiently manage and
analyze large volumes of data and gain real-time business insights.

This repository contains the foundational code for the **dbt-maxcompute** adapter plugin. For guidance on developing the
adapter, please refer to the [official documentation](https://docs.getdbt.com/docs/contributing/building-a-new-adapter).

### Important Note

The `README` you are currently viewing will be updated with specific instructions and details on how to utilize the
adapter as development progresses.

### Adapter Versioning

This adapter plugin follows [semantic versioning](https://semver.org/). The initial version is **v1.8.0-a2**, designed
for compatibility with dbt Core v1.8.0. Since the plugin is in its early stages, the version number **a2** indicates
that it is an Alpha 2 release. A stable version will be released in the future, focusing on MaxCompute-specific
functionality and aiming for backwards compatibility.

## Getting Started

### Install the plugin

```bash
# we use python 3.8 for this example
conda create --name dbt-maxcompute-example python=3.8
conda activate dbt-maxcompute-example

pip install dbt-core
pip install dbt-maxcompute
```

### Configure dbt profile:

1. Create a file in the ~/.dbt/ directory named profiles.yml.
2. Copy the following and paste into the new profiles.yml file. Make sure you update the values where noted.

```yaml
jaffle_shop: # this needs to match the profile in your dbt_project.yml file
  target: dev
  outputs:
    dev:
      type: maxcompute
      project: dbt-example # Replace this with your project name
      schema: default # Replace this with schema name, e.g. dbt_bilbo
      endpoint: http://service.cn-shanghai.maxcompute.aliyun.com/api # Replace this with your maxcompute endpoint
      accessId: XXX # Replace this with your accessId(ak)
      accessKey: XXX # Replace this with your accessKey(sk)
```

Currently we support the following parameters：

| **Field**   | **Description**                                                                                    | **Default Value**       |
|-------------|----------------------------------------------------------------------------------------------------|-------------------------|
| `type`      | Specifies the type of database connection; must be set to "maxcompute" for MaxCompute connections. | `"maxcompute"`          |
| `project`   | The name of your MaxCompute project.                                                               | N/A (Must be specified) |
| `endpoint`  | The endpoint URL for connecting to MaxCompute.                                                     | N/A (Must be specified) |
| `accessId`  | The Access ID for authentication with MaxCompute.                                                  | N/A (Must be specified) |
| `accessKey` | The Access Key for authentication with MaxCompute.                                                 | N/A (Must be specified) |
| `schema`    | The namespace schema that the models will use in MaxCompute.                                       | N/A (Must be specified) |

**Notes**: The fields marked as "N/A (Must be specified)" indicate that these values are required and do not have
default values.

### Run you dbt models

If you are new to DBT, we have prepared a [Tutorial document](docs/Tutorial.md) for your reference. Of course, you can also access the
official documentation provided by DBT (but some additional adaptations may be required for MaxCompute)

## Developers Guide

If you want to contribute or develop the adapter, use the following command to set up your environment:

```bash
pip install -r dev-requirements.txt
```

## Reporting Bugs and Contributing

Your feedback helps improve the project:

- To report bugs or request features, please open a
  new [issue](https://github.com/aliyun/dbt-maxcompute/issues/new) on GitHub.

## Code of Conduct

We are committed to fostering a welcoming and inclusive environment. All community members are expected to adhere to
the [dbt Code of Conduct](https://community.getdbt.com/code-of-conduct).
