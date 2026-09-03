"""Agent-side HTTP client. Standard library only.

The agent runs on every machine that has documents on it, so it keeps the
Phase 1 property of installing without dependencies. `urllib.request` plus `ssl`
is enough for what the protocol needs, and it avoids making every client machine
carry a copy of httpx to move some bytes.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths

USER_AGENT = "cairn-agent/0.1"
TIMEOUT = 60


class RemoteError(RuntimeError):
    """A request failed. Carries the status so callers can branch on 404 vs 409."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass
class RemoteConfig:
    url: str
    token: str
    device_name: str

    @property
    def base(self) -> str:
        return self.url.rstrip("/") + "/v1"


class CairnClient:
    def __init__(self, config: RemoteConfig, *, ca_file: str | None = None, insecure: bool = False):
        self.config = config
        self._ctx: ssl.SSLContext | None = None
        if config.url.lower().startswith("https"):
            self._ctx = ssl.create_default_context(cafile=ca_file)
            if insecure:
                # Only for a self-signed cert on a trusted LAN during bring-up.
                self._ctx.check_hostname = False
                self._ctx.verify_mode = ssl.CERT_NONE

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, *, body: object = None,
                 json_body: object = None, content_type: str | None = None,
                 content_length: int | None = None) -> tuple[int, bytes]:
        url = f"{self.config.base}{path}"
        data = body
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.config.token}",
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self._ctx) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RemoteError(exc.code, detail) from None
        except urllib.error.URLError as exc:
            raise RemoteError(0, f"cannot reach {self.config.url}: {exc.reason}") from None

    def _json(self, method: str, path: str, **kw) -> dict:
        _, raw = self._request(method, path, **kw)
        return json.loads(raw) if raw else {}

    # -- api --------------------------------------------------------------

    def health(self) -> dict:
        return self._json("GET", "/health")

    def devices(self) -> dict:
        return self._json("GET", "/devices")

    def have(self, digests: list[str]) -> set[str]:
        """Ask which digests the server already holds. One round trip per batch."""
        if not digests:
            return set()
        out: set[str] = set()
        for i in range(0, len(digests), 1000):
            batch = digests[i : i + 1000]
            out.update(self._json("POST", "/objects/have", json_body={"digests": batch})["have"])
        return out

    def claim(self, digests: list[str]) -> int:
        """Take ownership of objects the server already holds. See the server docstring."""
        if not digests:
            return 0
        total = 0
        for i in range(0, len(digests), 1000):
            total += self._json("POST", "/objects/claim",
                                json_body={"digests": digests[i : i + 1000]})["claimed"]
        return total

    def verify_objects(self, digests: list[str] | None = None) -> dict:
        """Ask the server to re-hash its own bytes. See the server docstring."""
        payload = {"digests": digests or []}
        return self._json("POST", "/objects/verify", json_body=payload)

    def put_object(self, digest: str, path: str | Path) -> dict:
        """Upload a file's bytes, streamed rather than buffered.

        `urllib` will stream a file object as long as Content-Length is set, so a
        multi-gigabyte object never has to fit in the agent's memory.
        """
        real = paths.long_path(path)
        size = os.path.getsize(real)
        with open(real, "rb") as fh:
            return self._json("PUT", f"/objects/{digest}", body=fh,
                              content_type="application/octet-stream",
                              content_length=size)

    def get_object(self, digest: str, dest: str | Path) -> Path:
        _, raw = self._request("GET", f"/objects/{digest}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.long_path(dest), "wb") as fh:
            fh.write(raw)
        return dest

    def push_catalogue(self, rows: list[dict]) -> dict:
        total = 0
        for i in range(0, len(rows), 500):
            total += self._json("POST", "/catalogue", json_body={"files": rows[i : i + 500]})["upserted"]
        return {"upserted": total}

    def search(self, query: str, limit: int = 20) -> list[dict]:
        q = urllib.parse.quote(query)
        return self._json("GET", f"/search?q={q}&limit={int(limit)}")["hits"]


def enroll(url: str, name: str, secret: str, platform: str | None = None,
           *, ca_file: str | None = None, insecure: bool = False) -> RemoteConfig:
    """Exchange the enrollment secret for a device token."""
    tmp = CairnClient(RemoteConfig(url=url, token="", device_name=name),
                      ca_file=ca_file, insecure=insecure)
    payload = {"name": name, "secret": secret, "platform": platform}
    body = tmp._json("POST", "/devices/enroll", json_body=payload)
    return RemoteConfig(url=url, token=body["token"], device_name=body["name"])

