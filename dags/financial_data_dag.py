"""
Downloads stock market data from S&P 500 companies and inserts it into CrateDB.

Prerequisites
-------------
In CrateDB, the schema to store this data needs to be created once manually.
See the file setup/financial_data_schema.sql in this repository.

"""
import datetime
import math
import json
import logging
import requests
from bs4 import BeautifulSoup
import yfinance as yf
import pandas as pd

from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.operators.python import PythonOperator

def get_sp500_ticker_symbols():
    """Extracts SP500 companies' tickers from the SP500's wikipedia page"""

    # Getting the html code from S&P 500 wikipedia page
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r_html = requests.get(url,timeout=2.5).text
    soup = BeautifulSoup(r_html, 'html.parser')

    # The stock tickers are found in a table in the wikipedia page,
    # whose html "id" attribute is "constituents". Here, the html
    # soup is filtered to get the  table contents
    table_content = soup.find(id="constituents")

    # Each stock's information is stored in a 'tr' division,
    # so we use this as a filter to generate a list of stock data.
    # The first section (index=0) in the generated list contains
    # the headers (which are unimportant in this context), therefore,
    # only data from index=1 on is taken.
    # For mapping, we find the ticker in the first 'td' division of
    # each stock and replace, when given, a '.' (wikipedia notation)
    # with a '-' (yfinance notation).
    # Finally, the map is returned as a list.
    return list(map(lambda stock: stock.find('td').text.strip().replace('.', '-'),
                    table_content.find_all('tr')[1:]))


def download_yfinance_data_function(start_date):
    """downloads Adjusted Close data from SP500 companies"""

    tickers = get_sp500_ticker_symbols()
    data = yf.download(tickers, start=start_date)['Adj Close']
    return data.to_json()

def prepare_data_function(ti):
    """creates a list of dictionaries with clean data values"""

    # pulling data (as string)
    string_data = ti.xcom_pull(task_ids='download_data_task')

    # transforming to json
    json_data = json.loads(string_data)

    # transforming to dataframe for easier manipulation
    df = pd.DataFrame.from_dict(json_data, orient='index')

    values_dict = []

    for col, closing_date in enumerate(df.columns):

        for row, ticker in enumerate(df.index):
            adj_close = df.iloc[row, col]

            if not(adj_close is None or math.isnan(adj_close)):
                values_dict.append(
                    {'closing_date': closing_date, 'ticker': ticker, 'adj_close': adj_close}
                )
            else:
                logging.info("Skipping %s for %s, invalid adj_close (%s)",
                             ticker, closing_date, adj_close)

    return values_dict

def format_and_insert_data_function(ti):
    """formats values to SQL standards and inserts financial data values into CrateDB"""

    values_dict = ti.xcom_pull(task_ids='prepare_data_task')
    formatted_values = []
    for values in values_dict:
        formatted_values.append(
            f"({values['closing_date']}, '{values['ticker']}', {values['adj_close']})"
        )

    insert_stmt = f"""
        INSERT INTO sp500 (closing_date, ticker, adjusted_close)
        VALUES {", ".join(formatted_values)}
        ON CONFLICT (closing_date, ticker) DO UPDATE SET adjusted_close = excluded.adjusted_close
        """

    insert_data_task = PostgresOperator(
                task_id="insert_data_task",
                postgres_conn_id="cratedb_connection",
                sql=insert_stmt
                )

    insert_data_task.execute({})


with DAG(
    dag_id="financial_data_dag",
    start_date=datetime.datetime(2022, 1, 10),
    schedule_interval="@daily",
    catchup=False,
) as dag:

    download_data_task = PythonOperator(task_id='download_data_task',
                                    python_callable=download_yfinance_data_function,
                                    op_kwargs={
                                        "start_date": "{{ ds }}",
                                    },
                                    execution_timeout=datetime.timedelta(minutes=3))

    prepare_data_task = PythonOperator(task_id='prepare_data_task',
                                    python_callable=prepare_data_function,
                                    op_kwargs={},
                                    execution_timeout=datetime.timedelta(minutes=3))

    format_and_insert_data_task = PythonOperator(task_id='format_and_insert_data_task',
                                    python_callable=format_and_insert_data_function,
                                    op_kwargs={},
                                    execution_timeout=datetime.timedelta(minutes=3))

download_data_task >> prepare_data_task >> format_and_insert_data_task