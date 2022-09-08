"""
Imports NYC Taxi data from S3 into CrateDB

A detailed tutorial is available at https://community.crate.io/t/cratedb-and-apache-airflow-building-a-data-ingestion-pipeline/926

Prerequisites
-------------
In the CrateDB schema "nyc_taxi", the tables "load_files_processed",
"load_trips_staging" and "trips" need to be present before running the DAG.
You can retrieve the CREATE TABLE statements from the file setup/taxi-schema.sql
in this repository.
"""
import logging
from pathlib import Path
import pendulum
from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.http.operators.http import SimpleHttpOperator
from airflow.operators.python import PythonOperator


def get_processed_files(_ti):
    pg_hook = PostgresHook(postgres_conn_id="cratedb_demo_connection")
    records = pg_hook.get_records(
        sql="SELECT file_name FROM nyc_taxi.load_files_processed"
    )

    # flatten nested list as there is only one column
    return list(map(lambda record: record[0], records))


def clean_data_urls(ti):
    data_urls_raw = ti.xcom_pull(task_ids="download_data_urls", key="return_value")

    data_urls = data_urls_raw.split("\n")
    # we only import Yellow tripdata for now due to different CSV schemas
    data_urls_filtered = filter(lambda element: "yellow" in element, data_urls)

    return list(data_urls_filtered)


def identitfy_missing_urls(ti):
    data_urls_processed = ti.xcom_pull(task_ids="get_processed_files")
    data_urls_available = ti.xcom_pull(task_ids="clean_data_urls")

    return list(set(data_urls_available) - set(data_urls_processed))


def process_new_files(ti):
    missing_urls = ti.xcom_pull(task_ids="identitfy_missing_urls")

    for missing_url in missing_urls:
        logging.info(missing_url)

        file_name = missing_url.split("/").pop()

        PostgresOperator(
            task_id=f"copy_{file_name}",
            postgres_conn_id="cratedb_demo_connection",
            sql=f"""
                    COPY nyc_taxi.load_trips_staging
                    FROM '{missing_url}'
                    WITH (format = 'csv', empty_string_as_null = true)
                    RETURN SUMMARY;
                """,
        ).execute({})

        PostgresOperator(
            task_id=f"log_{file_name}",
            postgres_conn_id="cratedb_demo_connection",
            sql=Path("include/taxi-insert.sql").read_text(encoding="utf-8"),
        ).execute({})

        PostgresOperator(
            task_id=f"mark_processed_{file_name}",
            postgres_conn_id="cratedb_demo_connection",
            sql=f"INSERT INTO nyc_taxi.load_files_processed VALUES ('{missing_url}');",
        ).execute({})

        PostgresOperator(
            task_id=f"purge_staging_{file_name}",
            postgres_conn_id="cratedb_demo_connection",
            sql="DELETE FROM nyc_taxi.load_trips_staging;",
        ).execute({})


with DAG(
    dag_id="nyc-taxi",
    start_date=pendulum.datetime(2021, 11, 11, tz="UTC"),
    schedule_interval="@daily",
    catchup=False,
) as dag:
    download_data_urls = SimpleHttpOperator(
        task_id="download_data_urls",
        method="GET",
        http_conn_id="http_raw_github",
        endpoint="toddwschneider/nyc-taxi-data/master/setup_files/raw_data_urls.txt",
        headers={},
    )

    clean_data_urls = PythonOperator(
        task_id="clean_data_urls",
        python_callable=clean_data_urls,
        op_kwargs={},
    )

    clean_data_urls << download_data_urls

    get_processed_files = PythonOperator(
        task_id="get_processed_files",
        python_callable=get_processed_files,
        op_kwargs={},
    )

    identitfy_missing_urls = PythonOperator(
        task_id="identitfy_missing_urls",
        python_callable=identitfy_missing_urls,
        op_kwargs={},
    )

    identitfy_missing_urls << [clean_data_urls, get_processed_files]

    # The staging table should be empty already. Purging it again in case of
    # an abort or other error case.
    purge_staging_init = PostgresOperator(
        task_id="purge_staging_init",
        postgres_conn_id="cratedb_demo_connection",
        sql="DELETE FROM nyc_taxi.load_trips_staging;",
    )

    process_new_files = PythonOperator(
        task_id="process_new_files",
        python_callable=process_new_files,
        op_kwargs={},
    )

    process_new_files << [identitfy_missing_urls, purge_staging_init]
