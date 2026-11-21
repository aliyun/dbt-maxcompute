{% macro mc_validate_microbatch_config(partition_by, batch_size, event_time, language=None) %}
  {% if partition_by is none %}
    {% set missing_partition_msg -%}
    The 'microbatch' strategy requires a `partition_by` config.
    {%- endset %}
    {% do exceptions.raise_compiler_error(missing_partition_msg) %}
  {% endif %}

  {% if not event_time %}
    {% do exceptions.raise_compiler_error(
      "The 'microbatch' strategy requires an `event_time` config."
    ) %}
  {% endif %}

  {#-- A batch is selected by `<event_time> >= '<start>' and <event_time> < '<end>'`, and dbt     --#}
  {#-- renders those boundaries timestamp-shaped: '2025-05-01 00:00:00+00:00'. MaxCompute does not --#}
  {#-- convert that text against a DATE column and it does not raise either - the comparison       --#}
  {#-- evaluates to NULL, so *no* row of the batch is selected, an empty overwrite changes nothing --#}
  {#-- and the run reports success with the target left empty. Measured in a UTC session and in    --#}
  {#-- +08:00/-08:00 ones alike, so this is not a clock disagreement (the session-timezone defect  --#}
  {#-- is a separate one): a DATE event column can never be windowed by this strategy.             --#}
  {#--                                                                                             --#}
  {#-- This is an EARLY DIAGNOSTIC, not the authority. The authority is the physical type of the   --#}
  {#-- column being filtered, asked once per decision in the adapter's filter path, which can       --#}
  {#-- describe the relation (#38's renderer plus this branch): a verified DATE there is refused on --#}
  {#-- every session, before any session-clock fallback. What is *not* done any more is letting a   --#}
  {#-- YAML declaration stop a model: measured on the real ref() path, a column physically          --#}
  {#-- `timestamp` was declared `timestamp_ntz`, so trusting the declaration would both over- and   --#}
  {#-- under-refuse. No type-blind boundary text exists either: the date-only form that a DATE      --#}
  {#-- column does accept raises ODPS-0130071 against a DATETIME column and evaluates to NULL for   --#}
  {#-- TIMESTAMP and TIMESTAMP_NTZ, and OR-ing the two texts inherits the DATETIME failure - those  --#}
  {#-- readings are in docs/microbatch-support.md.                                                 --#}
  {#-- This refusal is a statement about the SQL boundary text, so it is not applied to      --#}
  {#-- MaxFrame Python models: those bind the window in Python - pandas Timestamp bounds      --#}
  {#-- against the event series after astype("datetime64[ns]") - see                          --#}
  {#-- relations/table/create.sql:102-116. Whether that path returns the rows it should on a  --#}
  {#-- DATE event column is not measured here; what is measured is that it never performs the  --#}
  {#-- DATE-versus-timestamp-text comparison this message describes. Anything other than      --#}
  {#-- an explicit 'python' keeps the check, so an unknown language fails closed.             --#}
  {% if event_time is string and partition_by.auto_partition() and language != 'python' %}
    {% set event_field = event_time | replace("`", "") | trim | lower %}
    {% for i in range(partition_by.fields | length) %}
      {% set field = partition_by.fields[i] | replace("`", "") | trim | lower %}
      {% set data_type = partition_by.data_types[i] | trim | lower %}
      {% if field == event_field and data_type == "date" %}
        {% set date_event_time_msg -%}
The 'microbatch' strategy warns: this model declares a `date` event partition column.
  `event_time`: {{ event_time }}
  `partition_by`: field {{ partition_by.fields[i] }} is declared data_type {{ partition_by.data_types[i] }}
A batch is compared against a timestamp-shaped boundary ({{ event_time }} >= '2025-05-01
00:00:00+00:00'), and MaxCompute evaluates a DATE against that text as NULL rather than raising:
every batch selects zero rows, the overwrite writes nothing, and the run still reports success - which
is why the adapter refuses that case as soon as it can verify the physical type. A declaration alone is
not enough to stop the model: the column being filtered may well be an instant.
The window is attached to the upstream relation's `event_time` column, so that column is what has to
become an instant: cast it where the upstream model is built and declare `event_time` on the result,
then partition this model on it with `data_type: timestamp`. A `cast()` inside this model's own
SELECT is measured to be too late - the batch is still empty and still reported as a success.
DATE->DATETIME is not a way out either (this warehouse refuses that cast), and neither is making the
DATE column itself the partition key (MaxCompute partition keys must be BIGINT or STRING).
        {%- endset %}
        {% do exceptions.warn(date_event_time_msg) %}
      {% endif %}
    {% endfor %}
  {% endif %}

  {% if partition_by.granularity != batch_size %}
    {% set invalid_partition_by_granularity_msg -%}
    The 'microbatch' strategy requires a `partition_by` config with the same granularity as its configured `batch_size`.
    Got:
      `batch_size`: {{ batch_size }}
      `partition_by.granularity`: {{ partition_by.granularity }}
    {%- endset %}
    {% do exceptions.raise_compiler_error(invalid_partition_by_granularity_msg) %}
  {% endif %}
{% endmacro %}

{% macro mc_generate_microbatch_build_sql(
      tmp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, tmp_relation_exists, tblproperties
) %}
    {% set build_sql = mc_insert_overwrite_sql(
        tmp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, tmp_relation_exists, tblproperties
    ) %}

    {{ return(build_sql) }}
{% endmacro %}
