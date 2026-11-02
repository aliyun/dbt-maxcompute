# Python UDFs

> **Status: Generally Available (`1.11.3`).** Supported for production workloads
> within the scope and limitations documented below.

`dbt-maxcompute` can build persistent MaxCompute Python functions from dbt
`functions:` resources. Python scalar UDFs and Python aggregate UDFs (UDAFs)
are supported.

This capability is separate from MaxFrame Python models. It uses PyODPS to
upload a Python resource and register a MaxCompute catalog function, so the
function can be called from dbt SQL models, BI tools, notebooks, and other
MaxCompute clients.

## Supported scope

- dbt Core 1.11 and 1.12 within the [tested combinations](dbt-core-compatibility.md);
- Python scalar UDFs defined as standard dbt Python functions;
- Python aggregate UDFs implemented with the MaxCompute `BaseUDAF` lifecycle;
- dbt DAG dependencies through `{{ function('name') }}`;
- content-addressed, dbt-managed PY resources;
- in-place function updates that preserve the previous function until the new
  resource has been uploaded;
- existing MaxCompute FILE, TABLE, PY, JAR, or ARCHIVE resources as UDF
  dependencies;
- existing archive resources added to `sys.path` before user imports.

The adapter enables MaxCompute CPython 3.11 by default with
`odps.sql.python.version=cp311` for SQL submitted through dbt. CPython 3.7.3
remains available as an explicit legacy compatibility mode.

This global CP311 default is unchanged from the `1.11.3` beta releases,
including calls by raw SQL function name. When upgrading from `1.11.2`, set
`sql_hints={'odps.sql.python.version': 'cp27'}` for existing Python 2.7 UDFs,
or use `cp37` for Python 3.7 UDFs. A legacy UDF called by a hook also needs
the corresponding `SET odps.sql.python.version=...;` in that hook's SQL.

dbt Core requires every Python function to have an effective
`runtime_version`. Set CPython 3.11 once as the project-wide default in
`dbt_project.yml`; individual function YAML files can then omit it:

```yaml
functions:
  +runtime_version: "3.11"
```

Declare it on an individual function only to override the project default, for
example when a legacy function must use `cp37`.

MaxCompute selects the Python runtime at SQL session level rather than storing
it in function metadata. The dbt `runtime_version` config controls source
validation and compatibility reporting; callers of a legacy `cp37` function
must therefore also use a `cp37` SQL session hint.

## Function overloads

Function overloads introduced in dbt Core 1.12 are not supported. MaxCompute
function deployment in this adapter updates one catalog function per name;
multiple signatures would replace that same function. A non-empty `overloads`
list raises a compilation error before uploading resources or changing the
root function, including when that function already exists.

Define each signature under a different function name instead. Functions without
`overloads`, or with an empty list, retain their existing deployment behavior.

## Scalar UDF quick start

Create `functions/double_value.py`:

```python
def main(value):
    if value is None:
        return None
    return value * 2
```

Create `functions/double_value.yml`:

```yaml
functions:
  - name: double_value
    description: Double a BIGINT value.
    config:
      entry_point: main
      runtime_version: "3.11"
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
```

Build the function:

```bash
dbt build --select double_value
```

Call it from a model:

```sql
select
    order_id,
    {{ function('double_value') }}(quantity) as doubled_quantity
from {{ ref('orders') }}
```

The `function()` macro renders the fully qualified MaxCompute function name and
adds the function node as an upstream dbt dependency.

### What the adapter generates

The `.py` source remains a normal dbt Python UDF. During deployment, the
adapter adds a small MaxCompute wrapper similar to:

```python
from odps.udf import annotate


@annotate("bigint->bigint")
class GeneratedHandler:
    def evaluate(self, value):
        return main(value)
```

The annotation is generated from `arguments[].data_type` and
`returns.data_type`, so users do not duplicate the signature in Python.

A native MaxCompute scalar class with an `evaluate()` method is also accepted:

```python
class Multiplier:
    def __init__(self):
        self.factor = 2

    def evaluate(self, value):
        return value * self.factor
```

Set `entry_point: Multiplier` for this form.

## Aggregate UDFs

dbt exposes `type: aggregate`, but aggregation lifecycle contracts differ by
warehouse. MaxCompute requires a bounded, marshallable buffer. For that reason,
aggregate functions use the native MaxCompute lifecycle instead of attempting
to translate another warehouse's state object.

Create `functions/sum_values.py`:

```python
class SumValues:
    def new_buffer(self):
        return [0]

    def iterate(self, buffer, value):
        if value is not None:
            buffer[0] += value

    def merge(self, buffer, partial):
        buffer[0] += partial[0]

    def terminate(self, buffer):
        return buffer[0]
```

Create `functions/sum_values.yml`:

```yaml
functions:
  - name: sum_values
    config:
      type: aggregate
      entry_point: SumValues
      runtime_version: cp311
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
```

The adapter validates the four required methods and adds `BaseUDAF` plus the
generated `@annotate` signature. The value returned by `new_buffer()` must be a
bounded Python object supported by `marshal`, normally a list or dictionary.
MaxCompute limits the marshalled buffer to 2 MB.

## Data types

Use MaxCompute UDF data types in YAML. Common examples include:

- `bigint`, `string`, `double`, `boolean`, `datetime`, `date`, `decimal`;
- `array<bigint>`, `map<string,bigint>`, and
  `struct<name:string,value:double>`.

For easier migration, the adapter maps these common aliases:

| YAML type | MaxCompute UDF type |
|---|---|
| `integer`, `int64` | `bigint` |
| `float64` | `double` |
| `bool` | `boolean` |
| `numeric` | `decimal` |
| `bytes` | `binary` |

Aliases are also normalized inside nested types, for example
`array<int64>` becomes `array<bigint>`.

MaxCompute Python UDFs do not support the JSON type. Type compatibility is
checked again by MaxCompute when a SQL statement calls the function.

## Resource dependencies

MaxCompute does not install public PyPI packages while creating a UDF. Upload
compatible dependencies as MaxCompute resources before building the function.

Reference FILE or TABLE resources used through `odps.distcache`:

```yaml
functions:
  - name: lookup_name
    config:
      entry_point: main
      runtime_version: "3.11"
      maxcompute:
        resources:
          - lookup.txt
          - lookup_table_resource
    arguments:
      - name: id
        data_type: bigint
    returns:
      data_type: string
```

Reference CPython 3.11-compatible archive resources that must be importable:

```yaml
config:
  entry_point: main
  runtime_version: "3.11"
  maxcompute:
    python_libraries:
      - your-package-cp311-manylinux_x86_64.zip
```

Every `python_libraries` entry is also attached to the function's resource
list. The adapter inserts `work/<resource-name>` at the front of `sys.path`
before normal user imports. Dependency resources are user-managed and are
never deleted by dbt.

The standard dbt `packages` config is rejected rather than silently treated as
MaxCompute resources. Package names such as `numpy` do not identify an uploaded
archive, and compiled dependencies must match the selected MaxCompute Python
runtime and operating-system image. A `cp37` wheel cannot be reused on
`cp311`.

## Deployment and failure behavior

For each source version, the adapter:

1. validates the source against the declared Python 3.11 or 3.7 syntax and
   checks the entry point;
2. generates the MaxCompute handler and signature;
3. uploads a content-addressed PY resource such as
   `dbt_udf_double_value_<hash>.py`;
4. creates the function, or updates the existing function's class and resource
   list with the PyODPS `Function.update()` API;
5. deletes the previous dbt-managed code resource only after the function
   update succeeds.

If upload or update fails, the previous function registration and its resource
remain available. If the update result is ambiguous, the new resource is also
kept so cleanup cannot break a function whose server-side update succeeded.
The next successful build removes obsolete dbt-managed resources.

dbt refuses to overwrite a content-addressed resource with the same name if it
does not carry the expected dbt ownership marker.

## BigQuery migration differences

| Capability | dbt-bigquery | dbt-maxcompute |
|---|---|---|
| Scalar source | Plain Python function | Same; adapter generates MaxCompute class |
| Runtime | Python 3.11 | CPython 3.11 by default (`3.11`, `cp311`); CPython 3.7 compatibility mode |
| Signature | BigQuery DDL from YAML | MaxCompute `@annotate` from YAML |
| Packages | Warehouse installs `packages` | Pre-upload compatible resources |
| Replacement | `CREATE OR REPLACE FUNCTION` | Upload new resource, then `Function.update()` |
| Aggregate | Warehouse-specific handler | MaxCompute `BaseUDAF` lifecycle |
| Table UDF | Not in dbt's supported contract | MaxCompute supports UDTF, but adapter does not expose it yet |
| Default arguments | Adapter-dependent | Not supported |
| Volatility | Ignored with a warning | Ignored with a warning |

## Current limitations

- SQL function resources are not part of this Python-first implementation.
- dbt table function resources / MaxCompute UDTFs are not exposed because dbt
  Core currently supports scalar and aggregate function contracts only.
- Function overloads are not supported by this adapter.
- `default_value`, function `grants`, and persisted function descriptions are
  not supported.
- MaxCompute UDFs cannot access the public internet by default.
- A single MaxCompute SQL statement cannot mix incompatible Python runtimes.
  The adapter defaults to `cp311`; a legacy `cp37` call must explicitly
  override `odps.sql.python.version` in `sql_hints` for that execution path.
- `dbt show --select` uses dbt Core's preview SQL path, which does not apply
  model `sql_hints`. For a legacy runtime, also provide the matching `SET`
  in the model's `sql_header` when previewing it.

## Troubleshooting

### The runtime version is rejected

Set the project-wide `functions: +runtime_version: "3.11"` default shown above,
or declare `runtime_version: "3.11"` / `cp311` on each function. Legacy
functions may declare `3.7`, `3.7.3`, or `cp37`, but SQL that calls them must
override the adapter's default `odps.sql.python.version` hint to `cp37`.

### A dependency cannot be imported

Confirm that the resource exists in the same project and schema as the
function, is listed under `maxcompute.python_libraries`, and was built for the
MaxCompute runtime. Include every transitive dependency.

### The function builds but a call fails type checking

Check that the SQL argument types exactly match the generated annotation. Add
an explicit SQL `cast` when the source column has a different MaxCompute type.

### An aggregate function is rejected

Use `new_buffer`, `iterate`, `merge`, and `terminate`. A Snowflake-style class
using `accumulate`, `aggregate_state`, and `finish` cannot be translated safely
because MaxCompute requires a marshallable intermediate buffer.
