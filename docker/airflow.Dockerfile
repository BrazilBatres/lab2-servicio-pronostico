FROM apache/airflow:3.1.0-python3.12
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/airflow/auth && chown 50000:0 /opt/airflow/auth && chmod 775 /opt/airflow/auth
USER airflow
COPY requirements-airflow.txt /tmp/requirements-airflow.txt
RUN pip install --no-cache-dir -r /tmp/requirements-airflow.txt
COPY src/ /opt/novamercado/src/
ENV PYTHONPATH=/opt/novamercado/src
