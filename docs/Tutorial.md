# DBT-MaxCompute Tutorial: Jaffle Shop

This example is a modified version of Steps 7-14 from the [Official Guide](https://docs.getdbt.com/guides/manual-install). Since the SQL examples provided in the official tutorial are not compatible with MaxCompute, we will utilize the TPCH dataset from a public dataset to construct a similar Jaffle Shop project. This project aims to teach users how to:

- Build models using MaxCompute SQL
- Modify the materialization of models (View or Table)
- Create relationships among multiple models (Build models on top of other models)
- Add tests to models
- Document models

## Building Models with MaxCompute SQL

Under the models directory, create a new file named `customer.sql` to construct a customer model.

```sql
-- models/customer.sql
with customers as (
    select
        c_custkey as customer_id,
        c_name as customer_name,
        c_phone as customer_phone
    from BIGDATA_PUBLIC_DATASET.tpch_10g.customer
),
orders as (
    select
        o_orderkey as order_id,
        o_custkey as customer_id,
        o_orderdate as order_date,
        o_orderstatus as status
    from BIGDATA_PUBLIC_DATASET.tpch_10g.orders
),
customer_orders as (
    select
        customer_id,
        min(order_date) as first_order_date,
        max(order_date) as most_recent_order_date,
        count(order_id) as number_of_orders
    from orders
    group by customer_id
),
final as (
    select
        customers.customer_id,
        customers.customer_name,
        customers.customer_phone,
        customer_orders.first_order_date,
        customer_orders.most_recent_order_date,
        coalesce(customer_orders.number_of_orders, 0) as number_of_orders
    from customers left join customer_orders
        on customers.customer_id=customer_orders.customer_id
)
select * from final
```

When `dbt run` is executed, a view named `customer` will be created in the pre-configured Project and Schema.

```bash
1 of 1 START sql view model dbt_project.default.customer ....................... [RUN]
1 of 1 OK created sql view model dbt_project.default.customer .................. [OK in 2.78s]
Finished running 1 view model in 0 hours 0 minutes and 14.53 seconds (14.53s).
Completed successfully
Done. PASS=1 WARN=0 ERROR=0 SKIP=0 TOTAL=1
```

## Modify the Materialization of Models (View or Table)

To modify the materialization of models, refer to [Change the way your model is materialized](https://docs.getdbt.com/guides/manual-install#change-the-way-your-model-is-materialized).

## Create Relationships Among Multiple Models

Refer to [Build models on top of other models](https://docs.getdbt.com/guides/manual-install#build-models-on-top-of-other-models). We will split the original `customer.sql` file into three models:

`models/stg_customers.sql`

```sql
select c_custkey as customer_id,
       c_name    as customer_name,
       c_phone   as customer_phone
from BIGDATA_PUBLIC_DATASET.tpch_10g.customer
```

`models/stg_orders.sql`

```sql
select o_orderkey    as order_id,
       o_custkey     as customer_id,
       o_orderdate   as order_date,
       o_orderstatus as status
from BIGDATA_PUBLIC_DATASET.tpch_10g.orders
```

`models/customers.sql`

```sql
with customers as (
    select * from {{ ref('stg_customers') }}
),
orders as (
    select * from {{ ref('stg_orders') }}
),
customer_orders as (
    select
        customer_id,
        min(order_date) as first_order_date,
        max(order_date) as most_recent_order_date,
        count(order_id) as number_of_orders
    from orders
    group by customer_id
),
final as (
    select
        customers.customer_id,
        customers.customer_name,
        customers.customer_phone,
        customer_orders.first_order_date,
        customer_orders.most_recent_order_date,
        coalesce(customer_orders.number_of_orders, 0) as number_of_orders
    from customers left join customer_orders
        on customers.customer_id=customer_orders.customer_id
)
select * from final
```

When you execute `dbt run`, separate views/tables will be created for `stg_customers`, `stg_orders`, and `customers`. dbt infers the order to run these models automatically, so since `customers` depends on `stg_customers` and `stg_orders`, dbt builds `customers` last. No explicit dependency definitions are necessary.

```bash
1 of 3 START sql table model dbt_project.default.stg_customers ................. [RUN]
1 of 3 OK created sql table model dbt_project.default.stg_customers ............ [OK in 9.39s]
2 of 3 START sql table model dbt_project.default.stg_orders .................... [RUN]
2 of 3 OK created sql table model dbt_project.default.stg_orders ............... [OK in 13.49s]
3 of 3 START sql table model dbt_project.default.customer ...................... [RUN]
3 of 3 OK created sql table model dbt_project.default.customer ................. [OK in 12.50s]

Finished running 3 table models in 0 hours 0 minutes and 47.17 seconds (47.17s).
Completed successfully
Done. PASS=3 WARN=0 ERROR=0 SKIP=0 TOTAL=3
```

## Add Tests to Models

1. Create a new YAML file in the models directory, named `models/schema.yml`.
2. Add the following contents to the file:

`models/schema.yml`

```yaml
version: 2
models:
  - name: customers
    columns:
      - name: customer_id
        tests:
          - unique
          - not_null
  - name: stg_customers
    columns:
      - name: customer_id
        tests:
          - unique
          - not_null
  - name: stg_orders
    columns:
      - name: order_id
        tests:
          - unique
          - not_null
      - name: status
        tests:
          - accepted_values:
              values: ['F', 'O', 'P']
      - name: customer_id
        tests:
          - not_null
          - relationships:
              to: ref('stg_customers')
              field: customer_id
```

Run `dbt test`, and confirm that all your tests passed. When you run `dbt test`, dbt iterates through your YAML files and constructs a query for each test. Each query will return the number of records that fail the test. If this number is 0, then the test is successful.

```bash
1 of 7 START test accepted_values_stg_orders_status__F__O__P ................... [RUN]
1 of 7 PASS accepted_values_stg_orders_status__F__O__P ......................... [PASS in 4.83s]
2 of 7 START test not_null_stg_customers_customer_id ........................... [RUN]
2 of 7 PASS not_null_stg_customers_customer_id ................................. [PASS in 3.05s]
3 of 7 START test not_null_stg_orders_customer_id .............................. [RUN]
3 of 7 PASS not_null_stg_orders_customer_id .................................... [PASS in 2.23s]
4 of 7 START test not_null_stg_orders_order_id ................................. [RUN]
4 of 7 PASS not_null_stg_orders_order_id ....................................... [PASS in 1.56s]
5 of 7 START test relationships_stg_orders_customer_id__customer_id__ref_stg_customers_  [RUN]
5 of 7 PASS relationships_stg_orders_customer_id__customer_id__ref_stg_customers_  [PASS in 6.71s]
6 of 7 START test unique_stg_customers_customer_id ............................. [RUN]
6 of 7 PASS unique_stg_customers_customer_id ................................... [PASS in 2.89s]
7 of 7 START test unique_stg_orders_order_id ................................... [RUN]
7 of 7 PASS unique_stg_orders_order_id ......................................... [PASS in 8.48s]
Finished running 7 data tests in 0 hours 0 minutes and 40.23 seconds (40.23s).
Completed successfully
Done. PASS=7 WARN=0 ERROR=0 SKIP=0 TOTAL=7
```

## Document Models

1. Update your `models/schema.yml` file to include some descriptions, such as those below:

```yaml
version: 2
models:
  - name: customers
    description: One record per customer
    columns:
      - name: customer_id
        description: Primary key
        tests:
          - unique
          - not_null
      - name: first_order_date
        description: NULL when a customer has not yet placed an order.
  - name: stg_customers
    description: This model cleans up customer data
    columns:
      - name: customer_id
        description: Primary key
        tests:
          - unique
          - not_null
  - name: stg_orders
    description: This model cleans up order data
    columns:
      - name: order_id
        description: Primary key
        tests:
          - unique
          - not_null
      - name: status
        tests:
          - accepted_values:
              values: ['F', 'O', 'P']
      - name: customer_id
        tests:
          - not_null
          - relationships:
              to: ref('stg_customers')
              field: customer_id
```

2. Run `dbt docs generate` to generate the documentation for your project. dbt introspects your project and your warehouse to generate a JSON file with rich documentation about your project.
3. Run the `dbt docs serve` command to launch the documentation in a local website.
