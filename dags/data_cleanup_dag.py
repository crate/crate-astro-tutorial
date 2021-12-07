"""
Implements a retention policy by dropping expired partitions

A detailed tutorial is available at https://community.crate.io/t/cratedb-and-apache-airflow-implementation-of-data-retention-policy/913

Prerequisites
-------------
In CrateDB, tables for storing retention policies need to be created once manually.
See the file setup/data_cleanup_schema.sql in this repository.
"""
import datetime
import json
import logging
from pathlib import Path
from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.hooks.postgres_hook import PostgresHook
from airflow.operators.python_operator import PythonOperator


def get_policies(logical_date):
    pg_hook = PostgresHook(postgres_conn_id="cratedb_connection")
    sql = Path('include/data_cleanup_retrieve_policies.sql').read_text().format(date=logical_date)
    records = pg_hook.get_records(sql=sql)

    return json.dumps(records)


def delete_partition(partition):
    logging.info("Deleting partition %s = %s for table %s",
                 partition["column_name"],
                 partition["partition_value"],
                 partition["table_fqn"],
    )

    PostgresOperator(
        task_id="delete_from_{table}_{partition}_{value}".format(
            table=partition["table_fqn"],
            partition=partition["column_name"],
            value=partition["partition_value"],
        ),
        postgres_conn_id="cratedb_connection",
        sql=Path('include/data_cleanup_delete.sql').read_text().format(
            table=partition["table_fqn"],
            column=partition["column_name"],
            value=partition["partition_value"],
        ),
    ).execute(dict())


def reallocate_partition(partition):
    logging.info("Reallocating partition %s = %s for table %s to %s = %s",
                 partition["column_name"],
                 partition["partition_value"],
                 partition["table_fqn"],
                 partition["reallocation_attribute_name"],
                 partition["reallocation_attribute_value"],
    )

    # Reallocate the partition
    PostgresOperator(
        task_id="reallocate_{table}_{partition}_{value}".format(
            table=partition["table_fqn"],
            partition=partition["column_name"],
            value=partition["partition_value"],
        ),
        postgres_conn_id="cratedb_connection",
        sql=Path('include/data_cleanup_reallocate.sql').read_text().format(
            table=partition["table_fqn"],
            column=partition["column_name"],
            value=partition["partition_value"],
            attribute_name=partition["reallocation_attribute_name"],
            attribute_value=partition["reallocation_attribute_value"],
        ),
    ).execute(dict())

    # Update tracking information. As we process partitions in ascending order
    # by the partition value, it is safe to always save the current value.
    PostgresOperator(
        task_id="reallocate_track_{table}_{partition}_{value}".format(
            table=partition["table_fqn"],
            partition=partition["column_name"],
            value=partition["partition_value"],
        ),
        postgres_conn_id="cratedb_connection",
        sql=Path('include/data_cleanup_reallocate_tracking.sql').read_text().format(
            table=partition["table_name"],
            schema=partition["schema_name"],
            partition_value=partition["partition_value"],
        ),
    ).execute(dict())

def process_partition(partition):
    if partition["strategy"] == 'delete':
        delete_partition(partition)
    elif partition["strategy"] == 'reallocate':
        reallocate_partition(partition)
    else:
        logging.warning("Ignoring unknown strategy \"%s\" for table %s",
                        partition["strategy"],
                        partition["table_fqn"],
        )


def map_policy(policy):
    return {
        "schema_name": policy[0],
        "table_name": policy[1],
        "table_fqn": policy[2],
        "column_name": policy[3],
        "partition_value": policy[4],
        "strategy": policy[5],
        "reallocation_attribute_name": policy[6],
        "reallocation_attribute_value": policy[7],
    }


def process_partitions(ti):
    retention_policies = ti.xcom_pull(task_ids="retrieve_retention_policies")
    policies_obj = json.loads(retention_policies)

    for policy in policies_obj:
        process_partition(map_policy(policy))


with DAG(
    dag_id="data-cleanup-dag",
    start_date=datetime.datetime(2021, 11, 19),
    schedule_interval="@daily",
    catchup=False,
) as dag:
    get_policies = PythonOperator(
        task_id="retrieve_retention_policies",
        python_callable=get_policies,
        op_kwargs={
            "logical_date": "{{ ds }}",
        },
    )

    apply_policies = PythonOperator(
        task_id="apply_data_retention_policies",
        python_callable=process_partitions,
        provide_context=True,
        op_kwargs={},
    )

    get_policies >> apply_policies
