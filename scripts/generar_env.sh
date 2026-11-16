#!/usr/bin/env bash
# Crea .env a partir de .env.example con claves aleatorias (no se imprimen). No sobrescribe un .env existente.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then echo ".env ya existe; no se modifica"; exit 0; fi
gen() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }
sed -e "s|^PRODUCT_DB_PASSWORD=.*|PRODUCT_DB_PASSWORD=$(gen)|" \
    -e "s|^AIRFLOW_DB_PASSWORD=.*|AIRFLOW_DB_PASSWORD=$(gen)|" \
    -e "s|^AIRFLOW_PASSWORD=.*|AIRFLOW_PASSWORD=$(gen)|" \
    -e "s|^AIRFLOW_JWT_SECRET=.*|AIRFLOW_JWT_SECRET=$(gen)$(gen)|" \
    -e "s|^AIRFLOW_UID=.*|AIRFLOW_UID=$(id -u)|" .env.example > .env
chmod 600 .env
echo ".env creado con claves aleatorias (ver con: cat .env)"
