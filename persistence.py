"""Sauvegarde du fichier SQLite sur stockage S3-compatible (Cloudflare R2, etc.)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import streamlit as st

DEFAULT_DB_KEY = "agpm.db"


def local_db_path() -> Path:
    return Path(os.environ.get("AGPM_DB_PATH", "agpm.db")).resolve()


def get_persistence_config() -> Optional[dict[str, str]]:
    """Lit la config cloud (secrets Streamlit ou variables d'environnement)."""
    cfg: dict[str, str] = {}
    try:
        block = st.secrets.get("persistence")
        if block:
            cfg.update({str(k): str(v) for k, v in dict(block).items() if v is not None and str(v).strip()})
    except (FileNotFoundError, AttributeError, TypeError):
        pass

    env_map = {
        "bucket": "AGPM_S3_BUCKET",
        "key": "AGPM_S3_KEY",
        "aws_access_key_id": "AGPM_S3_ACCESS_KEY_ID",
        "aws_secret_access_key": "AGPM_S3_SECRET_ACCESS_KEY",
        "endpoint_url": "AGPM_S3_ENDPOINT_URL",
        "region": "AGPM_S3_REGION",
    }
    for field, env_name in env_map.items():
        val = os.environ.get(env_name)
        if val:
            cfg[field] = val

    if cfg.get("enabled", "true").lower() in ("0", "false", "no"):
        return None
    cfg.pop("enabled", None)

    if not cfg.get("bucket") or not cfg.get("aws_access_key_id") or not cfg.get("aws_secret_access_key"):
        return None
    cfg.setdefault("key", DEFAULT_DB_KEY)
    cfg.setdefault("region", "auto")
    return cfg


def persistence_is_configured() -> bool:
    return get_persistence_config() is not None


def is_ephemeral_streamlit_host() -> bool:
    host = (os.environ.get("HOSTNAME") or "").lower()
    if "streamlit.app" in host or "streamlit" in host:
        return True
    for var in ("STREAMLIT_RUNTIME_ENVIRONMENT", "STREAMLIT_SHARING_MODE"):
        val = (os.environ.get(var) or "").lower()
        if val and "cloud" in val:
            return True
    return False


def _s3_client(cfg: dict[str, str]):
    import boto3

    kwargs: dict[str, Any] = {
        "aws_access_key_id": cfg["aws_access_key_id"],
        "aws_secret_access_key": cfg["aws_secret_access_key"],
        "region_name": cfg.get("region", "auto"),
    }
    endpoint = cfg.get("endpoint_url", "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def pull_db_from_cloud(cfg: dict[str, str], local_path: Path) -> bool:
    """Télécharge la base distante. Retourne True si un fichier a été récupéré."""
    from botocore.exceptions import ClientError

    client = _s3_client(cfg)
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(cfg["bucket"], cfg["key"], str(local_path))
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def push_db_to_cloud(cfg: dict[str, str], local_path: Path) -> None:
    if not local_path.is_file():
        return
    client = _s3_client(cfg)
    client.upload_file(str(local_path), cfg["bucket"], cfg["key"])


@st.cache_resource
def bootstrap_local_database() -> str:
    """Au démarrage du serveur : récupère agpm.db depuis le cloud si configuré."""
    path = local_db_path()
    cfg = get_persistence_config()
    if cfg:
        try:
            pull_db_from_cloud(cfg, path)
        except Exception as exc:
            st.session_state["_persistence_pull_error"] = str(exc)
    return str(path)


class PersistingConnection:
    """Connexion SQLite qui envoie agpm.db vers le cloud après chaque commit."""

    def __init__(self, conn: sqlite3.Connection, db_path: str):
        self._conn = conn
        self._db_path = db_path

    def commit(self) -> None:
        self._conn.commit()
        cfg = get_persistence_config()
        if not cfg:
            return
        try:
            push_db_to_cloud(cfg, Path(self._db_path))
            st.session_state.pop("_persistence_push_error", None)
        except Exception as exc:
            st.session_state["_persistence_push_error"] = str(exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def clear_bootstrap_cache() -> None:
    bootstrap_local_database.clear()


def restore_database_file(data: bytes) -> None:
    path = local_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    cfg = get_persistence_config()
    if cfg:
        push_db_to_cloud(cfg, path)
    clear_bootstrap_cache()
