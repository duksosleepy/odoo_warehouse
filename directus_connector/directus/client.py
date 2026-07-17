from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote


class DirectusClientError(Exception):
    """Raised when Directus cannot be reached or returns an invalid response."""


def _load_httpx():
    try:
        import httpx
    except ImportError as exc:
        raise DirectusClientError("The Python package 'httpx' is required.") from exc
    return httpx


class DirectusClient:
    """Small synchronous Directus client.

    The LUG Directus server accepts the API key through the `access_token` query
    parameter, so that mode is the default. Bearer auth is still supported for
    reuse against standard Directus deployments.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        auth_mode: str = "access_token",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.auth_mode = auth_mode or "access_token"
        self.timeout = timeout
        self._httpx = _load_httpx()
        self._client = None

    def __enter__(self) -> "DirectusClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def open(self) -> None:
        if self._client is not None:
            return
        headers = {}
        if self.auth_mode == "bearer" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = self._httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self):
        self.open()
        return self._client

    def get_items(
        self,
        collection: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = f"/items/{quote(collection, safe='')}"
        request_params = dict(params or {})
        if self.auth_mode == "access_token" and self.token:
            request_params.setdefault("access_token", self.token)
        try:
            response = self.client.get(path, params=request_params)
            response.raise_for_status()
        except self._httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            body = exc.response.text[:500]
            raise DirectusClientError(
                f"Directus returned HTTP {status_code}: {body}"
            ) from exc
        except self._httpx.RequestError as exc:
            raise DirectusClientError(
                f"Directus request failed: {exc.__class__.__name__}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise DirectusClientError("Directus returned a non-JSON response.") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise DirectusClientError("Directus response does not contain a data key.")
        return payload

    def iter_items(
        self,
        collection: str,
        *,
        fields: Sequence[str] | None = None,
        page_size: int = 500,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        offset = 0
        total_count = None
        page_size = max(int(page_size or 500), 1)

        while True:
            request_params = dict(params or {})
            request_params.update({"limit": page_size, "offset": offset})
            if fields:
                request_params["fields"] = ",".join(fields)
            if total_count is None:
                request_params.setdefault("meta", "total_count")

            payload = self.get_items(collection, params=request_params)
            rows = payload.get("data") or []
            if not isinstance(rows, list):
                raise DirectusClientError("Directus data payload is not a list.")

            if total_count is None:
                meta = payload.get("meta") or {}
                total_count = meta.get("total_count")

            for row in rows:
                if isinstance(row, dict):
                    yield row

            row_count = len(rows)
            offset += row_count
            if not row_count or row_count < page_size:
                break
            if total_count is not None and offset >= int(total_count):
                break
