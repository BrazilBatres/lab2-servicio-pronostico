"""Cliente mínimo de la API REST de Airflow 3 (dispara y consulta el DAG de reentrenamiento)."""
from __future__ import annotations

import httpx

ESTADOS = {"queued": "en_curso", "running": "en_curso", "success": "exitoso", "failed": "fallido"}


class AirflowClient:
    def __init__(self, base_url: str, user: str | None, password: str | None, dag_id: str, timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.user, self.password = user, password
        self.dag_id = dag_id
        self.timeout = timeout
        self._token: str | None = None

    def _get_token(self) -> str | None:
        r = httpx.post(f"{self.base}/auth/token", json={"username": self.user, "password": self.password},
                       timeout=self.timeout)
        if r.status_code >= 400:  # p. ej. SIMPLE_AUTH_MANAGER_ALL_ADMINS: token sin credenciales
            r = httpx.get(f"{self.base}/auth/token", timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("access_token")

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        for intento in range(2):
            if self._token is None:
                self._token = self._get_token()
            h = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            r = httpx.request(method, f"{self.base}{path}", headers=h, timeout=self.timeout, **kw)
            if r.status_code in (401, 403) and intento == 0:
                self._token = None
                continue
            return r
        return r

    def health(self) -> None:
        r = httpx.get(f"{self.base}/api/v2/monitor/health", timeout=self.timeout)
        r.raise_for_status()
        st = r.json().get("scheduler", {}).get("status")
        if st not in (None, "healthy"):
            raise RuntimeError(f"scheduler {st}")

    def trigger(self, run_id: str) -> None:
        r = self._req("POST", f"/api/v2/dags/{self.dag_id}/dagRuns",
                      json={"dag_run_id": run_id, "logical_date": None, "conf": {"reentreno_id": run_id}})
        r.raise_for_status()

    def state(self, run_id: str) -> str | None:
        r = self._req("GET", f"/api/v2/dags/{self.dag_id}/dagRuns/{run_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return ESTADOS.get(r.json().get("state"), "en_curso")
