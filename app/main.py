import hmac
import logging
import os
import secrets
import json
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

ROOT = Path(os.getenv("MEDIA_ROOT", "/media")).resolve()
USERNAME = os.getenv("LINKMANAGER_USER", "admin")
PASSWORD = os.getenv("LINKMANAGER_PASSWORD", "")
ALLOW_LINK_CREATION = os.getenv("ALLOW_LINK_CREATION", "true").lower() == "true"
MAX_LIST_ITEMS = int(os.getenv("MAX_LIST_ITEMS", "1000"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("linkmanager")
app = FastAPI(title="LinkManager", version="0.1.0")
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    user_ok = secrets.compare_digest(credentials.username, USERNAME)
    password_ok = bool(PASSWORD) and secrets.compare_digest(credentials.password, PASSWORD)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentification requise",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def safe_path(raw: str) -> Path:
    # Resolve existing symlinks and prevent escaping the configured media root.
    candidate = (ROOT / raw.lstrip("/")).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail="Chemin hors de /media interdit")
    return candidate


def file_info(path: Path) -> dict:
    st = path.stat(follow_symlinks=False)
    is_dir = path.is_dir()
    return {
        "name": path.name or "/media",
        "path": "/" + str(path.relative_to(ROOT)) if path != ROOT else "/",
        "type": "directory" if is_dir else ("file" if path.is_file() else "other"),
        "size": st.st_size if path.is_file() else None,
        "mtime": int(st.st_mtime),
        "inode": st.st_ino,
        "device": st.st_dev,
        "nlink": st.st_nlink,
        "hardlinked": st.st_nlink > 1,
    }


class LinkRequest(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    destination_directory: str = Field(min_length=1, max_length=4096)
    destination_name: Optional[str] = Field(default=None, max_length=255)
    confirm: bool = False


@app.get("/")
def index(user: str = Depends(authenticate)):
    return FileResponse("app/static/index.html")


@app.get("/api/health")
def health(user: str = Depends(authenticate)):
    return {"status": "ok", "media_root": str(ROOT), "link_creation_enabled": ALLOW_LINK_CREATION}


@app.get("/api/list")
def list_directory(path: str = Query(default="/"), user: str = Depends(authenticate)):
    target = safe_path(path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Dossier introuvable")
    try:
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        results = []
        for item in items[:MAX_LIST_ITEMS]:
            try:
                # Do not follow symlinks when presenting entries.
                if item.is_symlink():
                    results.append({
                        "name": item.name, "path": "/" + str(item.relative_to(ROOT)),
                        "type": "symlink", "size": None, "mtime": None,
                        "inode": None, "device": None, "nlink": None, "hardlinked": False,
                    })
                else:
                    results.append(file_info(item))
            except (OSError, ValueError):
                continue
        return {"path": "/" if target == ROOT else "/" + str(target.relative_to(ROOT)),
                "parent": None if target == ROOT else ("/" if target.parent == ROOT else "/" + str(target.parent.relative_to(ROOT))),
                "items": results, "truncated": len(items) > MAX_LIST_ITEMS}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission refusée")


@app.get("/api/folder-hardlinks")
def folder_hardlinks(path: str = Query(default="/"), user: str = Depends(authenticate)):
    """Count hardlinked files inside each immediate subfolder, with a bounded scan."""
    base = safe_path(path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail="Dossier introuvable")

    counts = {}
    visited = 0
    truncated = False
    scan_limit = 20000

    try:
        for current, dirs, filenames in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
            entries = [(name, True) for name in dirs] + [(name, False) for name in filenames]
            for name, is_dir in entries:
                entry = Path(current) / name
                visited += 1
                if visited > scan_limit:
                    truncated = True
                    break
                if entry.is_symlink():
                    continue
                try:
                    relative = entry.relative_to(base)
                    # Attribute descendant files to the immediate subfolder being scanned.
                    if len(relative.parts) < 2 or is_dir:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_nlink > 1 and entry.is_file():
                        folder_path = "/" + str(relative.parts[0])
                        counts[folder_path] = counts.get(folder_path, 0) + 1
                except (OSError, ValueError):
                    continue
            if truncated:
                break
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission refusée pendant l'analyse")

    return {"path": "/" if base == ROOT else "/" + str(base.relative_to(ROOT)),
            "folders": counts, "scanned": min(visited, scan_limit), "truncated": truncated}


@app.get("/api/info")
def info(path: str = Query(...), user: str = Depends(authenticate)):
    target = safe_path(path)
    if not target.exists() or target.is_symlink() or not target.is_file():
        raise HTTPException(status_code=404, detail="Fichier ordinaire introuvable")
    return file_info(target)


@app.post("/api/hardlink")
def create_hardlink(payload: LinkRequest, user: str = Depends(authenticate)):
    if not ALLOW_LINK_CREATION:
        raise HTTPException(status_code=403, detail="Création de hardlinks désactivée")
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation explicite requise")

    source = safe_path(payload.source)
    destination_dir = safe_path(payload.destination_directory)
    if source.is_symlink() or not source.exists() or not source.is_file():
        raise HTTPException(status_code=400, detail="La source doit être un fichier ordinaire, non symbolique")
    if destination_dir.is_symlink() or not destination_dir.exists() or not destination_dir.is_dir():
        raise HTTPException(status_code=400, detail="Le dossier de destination est invalide")

    try:
        source_stat = source.stat(follow_symlinks=False)
        destination_stat = destination_dir.stat(follow_symlinks=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Impossible de lire les métadonnées : {exc}")

    if source_stat.st_dev != destination_stat.st_dev:
        raise HTTPException(status_code=400, detail="Source et destination ne sont pas sur le même système de fichiers")
    name = payload.destination_name or source.name
    if name in ("", ".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="Nom de destination invalide")
    destination = destination_dir / name
    # Refuse overwrite, even if destination is a symlink.
    if os.path.lexists(destination):
        raise HTTPException(status_code=409, detail="Un élément portant ce nom existe déjà")

    try:
        os.link(source, destination, follow_symlinks=False)
        logger.info("HARDLINK user=%s source=%s destination=%s", user, source, destination)
        return {"status": "created", "source": "/" + str(source.relative_to(ROOT)),
                "destination": "/" + str(destination.relative_to(ROOT))}
    except OSError as exc:
        logger.warning("Hardlink failed user=%s source=%s destination=%s error=%s", user, source, destination, exc)
        raise HTTPException(status_code=400, detail=f"Création impossible : {exc}")


# Read-only integrations. Credentials are read only from server-side environment variables.
def _integration_config(service: str) -> dict:
    prefix = service.upper()
    return {
        "url": os.getenv(f"{prefix}_URL", "").strip().rstrip("/"),
        "api_key": os.getenv(f"{prefix}_API_KEY", "").strip(),
        "username": os.getenv(f"{prefix}_USERNAME", "").strip(),
        "password": os.getenv(f"{prefix}_PASSWORD", ""),
    }


def _safe_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL invalide : utiliser une URL HTTP(S) sans identifiants intégrés")
    return value.rstrip("/")


def _json_request(url: str, headers: dict | None = None, opener=None, data: bytes | None = None):
    request = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data is not None else "GET")
    client = opener or urllib.request.build_opener()
    with client.open(request, timeout=5) as response:
        body = response.read(2_000_000)
        if len(body) >= 2_000_000:
            raise ValueError("Réponse trop volumineuse")
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            return json.loads(body.decode("utf-8")), response.status
        return body.decode("utf-8", errors="replace").strip(), response.status


def _path_map(service: str) -> list[dict]:
    raw = os.getenv(f"{service.upper()}_PATH_MAP", "").strip()
    if not raw:
        return []
    try:
        mappings = json.loads(raw)
        if not isinstance(mappings, list):
            return []
        valid = []
        for item in mappings:
            if not isinstance(item, dict):
                continue
            remote = item.get("remote")
            local = item.get("local")
            if isinstance(remote, str) and isinstance(local, str) and remote.startswith("/") and local.startswith("/") and chr(0) not in remote + local:
                valid.append({"remote": remote.rstrip("/") or "/", "local": local.rstrip("/") or "/"})
        return sorted(valid, key=lambda item: len(item["remote"]), reverse=True)
    except (json.JSONDecodeError, TypeError):
        return []


def _map_service_path(service: str, remote_path: str) -> str | None:
    if not isinstance(remote_path, str) or not remote_path.startswith("/"):
        return None
    candidate = Path(remote_path)
    for mapping in _path_map(service):
        prefix = Path(mapping["remote"])
        try:
            relative = candidate.relative_to(prefix)
        except ValueError:
            continue
        suffix = str(relative)
        return mapping["local"] if suffix == "." else mapping["local"].rstrip("/") + "/" + suffix
    return None


def _api_status(service: str) -> dict:
    cfg = _integration_config(service)
    if not cfg["url"]:
        return {"configured": False, "connected": False, "message": "URL non configurée"}
    try:
        base = _safe_base_url(cfg["url"])
        headers = {"Accept": "application/json", "User-Agent": "LinkManager/1.0"}
        if service in ("RADARR", "SONARR"):
            if not cfg["api_key"]:
                return {"configured": True, "connected": False, "message": "Clé API manquante"}
            headers["X-Api-Key"] = cfg["api_key"]
            versions = ["v3", "v5"] if service == "SONARR" else ["v3"]
            last_error = None
            for version in versions:
                try:
                    data, _ = _json_request(f"{base}/api/{version}/system/status", headers=headers)
                    if not isinstance(data, dict):
                        raise ValueError("Réponse inattendue de l'API")
                    return {"configured": True, "connected": True, "version": data.get("version"), "app": data.get("appName", service.title()), "api": version}
                except urllib.error.HTTPError as exc:
                    last_error = exc
                    if exc.code == 401 or exc.code == 403:
                        return {"configured": True, "connected": False, "message": "Authentification refusée (clé API ou accès réseau)"}
                    if exc.code != 404:
                        break
            if last_error:
                raise last_error
        elif service == "QBITTORRENT":
            if not cfg["username"] or not cfg["password"]:
                return {"configured": True, "connected": False, "message": "Identifiants WebUI manquants"}
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login_data = urllib.parse.urlencode({"username": cfg["username"], "password": cfg["password"]}).encode()
            _, _ = _json_request(f"{base}/api/v2/auth/login", headers={"User-Agent": "LinkManager/1.0", "Referer": base + "/"}, opener=opener, data=login_data)
            version, _ = _json_request(f"{base}/api/v2/app/version", headers=headers, opener=opener)
            if not isinstance(version, str) or version.lower() == "fails.":
                raise ValueError("Authentification qBittorrent refusée")
            return {"configured": True, "connected": True, "version": version, "app": "qBittorrent"}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            message = "Authentification refusée (vérifier les identifiants ou la clé API)"
        else:
            message = f"Erreur HTTP {exc.code}"
        return {"configured": True, "connected": False, "message": message}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Integration check failed service=%s error=%s", service, type(exc).__name__)
        return {"configured": True, "connected": False, "message": "Connexion impossible ou réponse invalide"}
    return {"configured": True, "connected": False, "message": "API non disponible"}


def _get_arr_data(service: str, endpoint: str):
    cfg = _integration_config(service)
    base = _safe_base_url(cfg["url"])
    headers = {"Accept": "application/json", "X-Api-Key": cfg["api_key"], "User-Agent": "LinkManager/1.0"}
    candidates = [f"{base}/api/v3/{endpoint}"]
    if service == "SONARR":
        candidates.append(f"{base}/api/v5/{endpoint}")
    last_error = None
    for url in candidates:
        try:
            return _json_request(url, headers=headers)[0]
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 404:
                raise
    if last_error:
        raise last_error
    return []


@app.get("/api/integrations")
def integrations(user: str = Depends(authenticate)):
    """Return read-only connection diagnostics and a small inventory from configured services."""
    result = {
        "qBittorrent": _api_status("QBITTORRENT"),
        "Radarr": _api_status("RADARR"),
        "Sonarr": _api_status("SONARR"),
        "read_only": True,
        "inventory": {},
        "warnings": [
            "Les chemins retournés par les applications sont leurs chemins Docker, pas nécessairement les chemins /media.",
            "Aucune action n'est envoyée à qBittorrent, Radarr ou Sonarr."
        ],
    }
    if result["qBittorrent"].get("connected"):
        try:
            cfg = _integration_config("QBITTORRENT")
            base = _safe_base_url(cfg["url"])
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login_data = urllib.parse.urlencode({"username": cfg["username"], "password": cfg["password"]}).encode()
            _json_request(f"{base}/api/v2/auth/login", headers={"User-Agent": "LinkManager/1.0", "Referer": base + "/"}, opener=opener, data=login_data)
            torrents, _ = _json_request(f"{base}/api/v2/torrents/info", headers={"Accept": "application/json", "User-Agent": "LinkManager/1.0"}, opener=opener)
            if isinstance(torrents, list):
                result["inventory"]["qBittorrent"] = {
                    "total": len(torrents),
                    "items": [{
                        "name": str(t.get("name", ""))[:240],
                        "state": str(t.get("state", ""))[:80],
                        "save_path": str(t.get("save_path", ""))[:1024],
                        "save_path_linkmanager": _map_service_path("QBITTORRENT", str(t.get("save_path", ""))),
                        "content_path": str(t.get("content_path", ""))[:1024],
                        "content_path_linkmanager": _map_service_path("QBITTORRENT", str(t.get("content_path", ""))),
                        "size": t.get("size") if isinstance(t.get("size"), (int, float)) else None,
                        "progress": t.get("progress") if isinstance(t.get("progress"), (int, float)) else None,
                    } for t in torrents[:30]],
                    "truncated": len(torrents) > 30,
                }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            result["inventory"]["qBittorrent"] = {"error": "Impossible de lire la liste des torrents"}
    for service, key in (("RADARR", "Radarr"), ("SONARR", "Sonarr")):
        if result[key].get("connected"):
            try:
                roots = _get_arr_data(service, "rootfolder")
                if service == "RADARR":
                    entries = _get_arr_data(service, "movie")
                    label = "films"
                else:
                    entries = _get_arr_data(service, "series")
                    label = "séries"
                result["inventory"][key] = {
                    "total": len(entries) if isinstance(entries, list) else None,
                    "label": label,
                    "root_folders": [{"path": str(x.get("path", ""))[:1024], "linkmanager_path": _map_service_path(service, str(x.get("path", ""))), "accessible": x.get("accessible")} for x in roots[:100] if isinstance(x, dict)] if isinstance(roots, list) else [],
                }
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                result["inventory"][key] = {"error": "Connecté, mais inventaire indisponible"}
    return result




# --- Cross-application audit and explicitly confirmed manual actions ---
import hashlib
import re
import posixpath
import sqlite3
import threading
import time

MAX_AUDIT_TORRENTS = 50000
QBITTORRENT_PAGE_SIZE = 500
MAX_AUDIT_FILES = 50000
MAX_AUDIT_HASH_FILE_BYTES = 1024 * 1024 * 1024
MAX_AUDIT_HASH_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_AUDIT_HASH_PAIRS = 80

# Persistent inventory cache. The database lives on a dedicated Docker volume.
CACHE_DB_PATH = os.getenv("LINKMANAGER_DB_PATH", "/app/data/linkmanager.sqlite3")
INVENTORY_CACHE_TTL_SECONDS = max(30, int(os.getenv("INVENTORY_CACHE_TTL_SECONDS", "300")))
_cache_lock = threading.RLock()
_cache_connection = None


def _cache_db():
    """Return a process-wide SQLite connection, creating the schema on first use."""
    global _cache_connection
    with _cache_lock:
        if _cache_connection is None:
            db_path = Path(CACHE_DB_PATH)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries ("
                "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.commit()
            _cache_connection = connection
        return _cache_connection


def _cache_get(key: str, max_age: int | None = None):
    """Return (decoded JSON value, age seconds), or None for a miss/expired entry."""
    try:
        with _cache_lock:
            row = _cache_db().execute(
                "SELECT payload, updated_at FROM cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        age = max(0, time.time() - float(row[1]))
        if max_age is not None and age > max_age:
            return None
        return json.loads(row[0]), age
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Persistent cache read unavailable error=%s", type(exc).__name__)
        return None


def _cache_set_many(entries: dict[str, object]):
    """Write a batch in one transaction to avoid one fsync per torrent."""
    if not entries:
        return
    now = time.time()
    try:
        rows = [(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now)
                for key, value in entries.items()]
        with _cache_lock:
            connection = _cache_db()
            connection.executemany(
                "INSERT INTO cache_entries(cache_key, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                rows,
            )
            connection.commit()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Persistent cache write unavailable error=%s", type(exc).__name__)


def _cached_qbit_torrents(base: str, opener):
    """Reuse a recent complete torrent list across requests and container restarts."""
    cached = _cache_get("inventory:v1:qbit:torrents", INVENTORY_CACHE_TTL_SECONDS)
    if cached and isinstance(cached[0], list):
        return cached[0], False, {"torrent_list_source": "cache", "torrent_list_age_seconds": int(cached[1])}
    torrents, truncated = _qbit_all_torrents(base, opener)
    if not truncated:
        # Keep only fields used by the audit, rather than persisting the entire qBittorrent response.
        compact = [{key: torrent.get(key) for key in ("hash", "name", "state", "save_path", "size")}
                   for torrent in torrents if isinstance(torrent, dict)]
        _cache_set_many({"inventory:v1:qbit:torrents": compact})
    return torrents, truncated, {"torrent_list_source": "live", "torrent_list_age_seconds": 0}


def _cached_qbit_torrent_files(base: str, opener, torrent_hash: str, stats: dict,
                               pending: dict[str, object]):
    """Cache file metadata by torrent hash; a hash identifies immutable torrent contents."""
    key = "inventory:qbit:files:" + torrent_hash
    cached = _cache_get(key)
    if cached and isinstance(cached[0], list):
        stats["torrent_file_cache_hits"] += 1
        return cached[0]
    stats["torrent_file_cache_misses"] += 1
    files = _qbit_json(base, opener, "/api/v2/torrents/files", {"hash": torrent_hash})
    if not isinstance(files, list):
        files = []
    pending[key] = files
    if len(pending) >= 250:
        _cache_set_many(pending)
        pending.clear()
    return files


def _cached_arr_inventory(service: str, endpoint: str, stats: dict):
    key = f"inventory:{service.lower()}:{endpoint}"
    cached = _cache_get(key, INVENTORY_CACHE_TTL_SECONDS)
    if cached and isinstance(cached[0], list):
        stats["arr_cache_hits"] += 1
        return cached[0]
    stats["arr_cache_misses"] += 1
    entries = _get_arr_data(service, endpoint)
    if isinstance(entries, list):
        _cache_set_many({key: entries})
    return entries




class ManualAction(BaseModel):
    confirm_text: str = Field(min_length=1, max_length=32)


class TorrentRemoval(ManualAction):
    torrent_hash: str = Field(min_length=40, max_length=40)
    delete_files: bool = False


class ArrFileRemoval(ManualAction):
    service: str = Field(pattern="^(RADARR|SONARR)$")
    file_id: int = Field(gt=0)


class ArrItemAction(ManualAction):
    service: str = Field(pattern="^(RADARR|SONARR)$")
    item_id: int = Field(gt=0)


class EmptyFolderRemoval(ManualAction):
    path: str = Field(min_length=1, max_length=4096)


def _require_manual_confirmation(payload: ManualAction):
    if payload.confirm_text != "SUPPRIMER":
        raise HTTPException(status_code=400, detail="Confirmation refusée. Saisis exactement SUPPRIMER pour valider cette action.")
    

def _request_json_method(url: str, method: str = "GET", headers: dict | None = None,
                         opener=None, data: bytes | None = None):
    request = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    client = opener or urllib.request.build_opener()
    with client.open(request, timeout=8) as response:
        body = response.read(2_000_000)
        if len(body) >= 2_000_000:
            raise ValueError("Réponse trop volumineuse")
        if not body:
            return None, response.status
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            return json.loads(body.decode("utf-8")), response.status
        return body.decode("utf-8", errors="replace").strip(), response.status


def _qbit_session():
    cfg = _integration_config("QBITTORRENT")
    if not cfg["url"] or not cfg["username"] or not cfg["password"]:
        raise HTTPException(status_code=503, detail="qBittorrent n'est pas configuré.")
    try:
        base = _safe_base_url(cfg["url"])
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        login_data = urllib.parse.urlencode({"username": cfg["username"], "password": cfg["password"]}).encode()
        headers = {"User-Agent": "LinkManager/1.0", "Referer": base + "/", "Content-Type": "application/x-www-form-urlencoded"}
        # qBittorrent versions/reverse proxies may vary in the login response body.
        # Verify the resulting cookie session against an authenticated endpoint instead
        # of relying solely on the exact literal returned by /auth/login.
        _request_json_method(base + "/api/v2/auth/login", "POST", headers, opener, login_data)
        version, _ = _request_json_method(
            base + "/api/v2/app/version",
            headers={"Accept": "application/json", "User-Agent": "LinkManager/1.0"},
            opener=opener,
        )
        if not isinstance(version, str) or not version.strip() or version.lower().strip() == "fails.":
            raise HTTPException(status_code=502, detail="Authentification qBittorrent refusée.")
        return base, opener
    except HTTPException:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="Connexion qBittorrent impossible.")


def _qbit_json(base: str, opener, endpoint: str, params: dict | None = None):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    try:
        return _request_json_method(base + endpoint + query, headers={"Accept": "application/json", "User-Agent": "LinkManager/1.0"}, opener=opener)[0]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="Impossible de lire l'inventaire qBittorrent.")


def _qbit_post(base: str, opener, endpoint: str, params: dict):
    data = urllib.parse.urlencode(params).encode()
    headers = {"User-Agent": "LinkManager/1.0", "Referer": base + "/", "Content-Type": "application/x-www-form-urlencoded"}
    try:
        return _request_json_method(base + endpoint, "POST", headers, opener, data)[0]
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"qBittorrent a refusé l'action (HTTP {exc.code}).")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="Action qBittorrent impossible.")


def _arr_request(service: str, endpoint: str, method: str = "GET", payload: dict | None = None):
    cfg = _integration_config(service)
    if not cfg["url"] or not cfg["api_key"]:
        raise HTTPException(status_code=503, detail=f"{service.title()} n'est pas configuré.")
    try:
        base = _safe_base_url(cfg["url"])
        headers = {"Accept": "application/json", "X-Api-Key": cfg["api_key"], "User-Agent": "LinkManager/1.0"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        versions = ["v3", "v5"] if service == "SONARR" else ["v3"]
        last_error = None
        for version in versions:
            url = f"{base}/api/{version}/{endpoint.lstrip('/')}"
            try:
                return _request_json_method(url, method, headers, data=data)[0]
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 404 and version != versions[-1]:
                    continue
                if exc.code in (401, 403):
                    raise HTTPException(status_code=502, detail=f"Clé API ou autorisation {service.title()} refusée.")
                if exc.code == 404:
                    raise HTTPException(status_code=404, detail=f"Élément {service.title()} introuvable.")
                raise HTTPException(status_code=502, detail=f"{service.title()} a refusé l'action (HTTP {exc.code}).")
        if last_error:
            raise HTTPException(status_code=502, detail=f"API {service.title()} indisponible.")
    except HTTPException:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail=f"Connexion {service.title()} impossible.")


def _mapped_local_path(service: str, remote_path: str) -> Path | None:
    mapped = _map_service_path(service, remote_path)
    if not mapped:
        return None
    try:
        candidate = (ROOT / mapped.lstrip("/")).resolve()
        candidate.relative_to(ROOT)
        return candidate
    except (ValueError, OSError):
        return None


def _local_metadata(service: str, remote_path: str) -> dict:
    local = _mapped_local_path(service, remote_path)
    result = {"local_path": None, "mapped": local is not None, "exists": False,
              "size": None, "device": None, "inode": None, "nlink": None}
    if local is None:
        return result
    result["local_path"] = "/" + str(local.relative_to(ROOT)) if local != ROOT else "/"
    try:
        if local.is_symlink() or not local.is_file():
            return result
        st = local.stat(follow_symlinks=False)
        result.update({"exists": True, "size": st.st_size, "device": st.st_dev,
                       "inode": st.st_ino, "nlink": st.st_nlink})
    except (OSError, ValueError):
        pass
    return result


def _sha256_bounded(path: Path, cache: dict, budget: dict) -> str | None:
    key = str(path)
    if key in cache:
        return cache[key]
    try:
        st = path.stat(follow_symlinks=False)
        if path.is_symlink() or not path.is_file() or st.st_size > MAX_AUDIT_HASH_FILE_BYTES:
            cache[key] = None
            return None
        if budget["bytes"] + st.st_size > MAX_AUDIT_HASH_TOTAL_BYTES or budget["files"] >= MAX_AUDIT_HASH_PAIRS * 2:
            cache[key] = None
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        budget["bytes"] += st.st_size
        budget["files"] += 1
        cache[key] = digest.hexdigest()
        return cache[key]
    except OSError:
        cache[key] = None
        return None



def _qbit_all_torrents(base: str, opener, max_torrents: int = MAX_AUDIT_TORRENTS):
    """Fetch the complete qBittorrent inventory in pages, up to a safety ceiling."""
    all_torrents = []
    offset = 0
    while offset < max_torrents:
        page_size = min(QBITTORRENT_PAGE_SIZE, max_torrents - offset)
        page = _qbit_json(base, opener, "/api/v2/torrents/info",
                          {"limit": str(page_size), "offset": str(offset)})
        if not isinstance(page, list):
            raise HTTPException(status_code=502, detail="Réponse qBittorrent inattendue pendant la pagination.")
        all_torrents.extend(page)
        if len(page) < page_size:
            return all_torrents, False
        offset += len(page)
    # A full final page means there may be more records. Never pretend the inventory is complete.
    return all_torrents, True


def _build_audit_inventory():
    base, opener = _qbit_session()
    raw_torrents, truncated_torrents, cache_stats = _cached_qbit_torrents(base, opener)
    cache_stats.update({"torrent_file_cache_hits": 0, "torrent_file_cache_misses": 0,
                        "arr_cache_hits": 0, "arr_cache_misses": 0})
    pending_cache_writes = {}
    torrent_records = []
    scanned_torrent_files = 0
    for torrent in raw_torrents:
        torrent_hash = str(torrent.get("hash", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{40}", torrent_hash):
            continue
        torrent_record = {"hash": torrent_hash, "name": str(torrent.get("name", ""))[:240],
                          "state": str(torrent.get("state", ""))[:80],
                          "save_path": str(torrent.get("save_path", ""))[:1024],
                          "size": torrent.get("size") if isinstance(torrent.get("size"), (int, float)) else None,
                          "files": []}
        files = _cached_qbit_torrent_files(base, opener, torrent_hash, cache_stats, pending_cache_writes)
        if not isinstance(files, list):
            files = []
        for item in files:
            if scanned_torrent_files >= MAX_AUDIT_FILES:
                break
            name = str(item.get("name", ""))[:2048]
            remote = posixpath.normpath(posixpath.join(str(torrent.get("save_path", "")), name))
            metadata = _local_metadata("QBITTORRENT", remote)
            size = item.get("size") if isinstance(item.get("size"), (int, float)) else metadata["size"]
            record = {"torrent_hash": torrent_hash, "torrent_name": torrent_record["name"],
                      "name": posixpath.basename(name), "remote_path": remote,
                      "size": size, **metadata}
            torrent_record["files"].append(record)
            scanned_torrent_files += 1
        torrent_records.append(torrent_record)
        if scanned_torrent_files >= MAX_AUDIT_FILES:
            break

    library_records = []
    for service, endpoint, id_field, parent_field, label in (
        ("RADARR", "moviefile", "id", "movieId", "film"),
        ("SONARR", "episodefile", "id", "seriesId", "série"),
    ):
        if not _integration_config(service)["url"] or not _integration_config(service)["api_key"]:
            continue
        try:
            entries = _cached_arr_inventory(service, endpoint, cache_stats)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError, HTTPException):
            logger.warning("Audit library inventory unavailable service=%s", service)
            continue
        if not isinstance(entries, list):
            continue
        for item in entries[:MAX_AUDIT_FILES]:
            remote = str(item.get("path", ""))
            metadata = _local_metadata(service, remote)
            library_records.append({
                "service": service, "kind": label, "file_id": item.get(id_field),
                "item_id": item.get(parent_field), "name": posixpath.basename(remote),
                "remote_path": remote[:2048], "size": item.get("size") if isinstance(item.get("size"), (int, float)) else metadata["size"],
                **metadata,
            })
            if len(library_records) >= MAX_AUDIT_FILES:
                break
    _cache_set_many(pending_cache_writes)
    cache_stats["ttl_seconds"] = INVENTORY_CACHE_TTL_SECONDS
    return torrent_records, library_records, truncated_torrents, scanned_torrent_files, cache_stats




def _qbit_content_references(local_path: Path):
    """Find torrents whose mapped content path is the file or a parent directory of it."""
    cfg = _integration_config("QBITTORRENT")
    if not cfg["url"] or not cfg["username"] or not cfg["password"]:
        raise HTTPException(status_code=409, detail="qBittorrent doit être configuré pour vérifier les références avant de supprimer ce fichier.")
    base, opener = _qbit_session()
    torrents, truncated = _qbit_all_torrents(base, opener)
    if truncated:
        raise HTTPException(status_code=409, detail="L'inventaire qBittorrent dépasse la limite de sécurité de 50 000 torrents ; vérification incomplète, suppression refusée.")
    target = local_path.resolve()
    refs = []
    for torrent in torrents:
        content = str(torrent.get("content_path", ""))
        mapped = _mapped_local_path("QBITTORRENT", content)
        if mapped is None:
            continue
        try:
            target.relative_to(mapped.resolve())
            refs.append({"hash": str(torrent.get("hash", "")), "name": str(torrent.get("name", "")),
                         "content_path": content, "local_path": str(mapped)})
        except ValueError:
            continue
    return refs

def _arr_file_references():
    """Return mapped file paths currently referenced by Radarr/Sonarr."""
    references = []
    for service, endpoint, id_field, parent_field in (
        ("RADARR", "moviefile", "id", "movieId"),
        ("SONARR", "episodefile", "id", "seriesId"),
    ):
        cfg = _integration_config(service)
        if not cfg["url"] or not cfg["api_key"]:
            raise HTTPException(status_code=409, detail=f"Configure {service.title()} avant toute suppression de fichier pour vérifier les références croisées.")
        try:
            entries = _get_arr_data(service, endpoint)
        except Exception:
            # Fail closed for destructive actions if an enabled service cannot be checked.
            raise HTTPException(status_code=409, detail=f"Impossible de vérifier les références de fichiers dans {service.title()} ; action refusée.")
        if not isinstance(entries, list):
            raise HTTPException(status_code=409, detail=f"Inventaire des fichiers invalide dans {service.title()} ; action refusée.")
        for entry in entries:
            remote = str(entry.get("path", ""))
            local = _mapped_local_path(service, remote)
            if local is not None:
                references.append({"service": service, "file_id": entry.get(id_field),
                                   "item_id": entry.get(parent_field), "remote_path": remote,
                                   "local_path": str(local)})
    return references

@app.get("/api/audit")
def audit(user: str = Depends(authenticate)):
    """Read-only cross-check of torrent files, media-library files, and actual filesystem identities."""
    try:
        torrents, library, truncated_torrents, scanned_torrent_files, cache_stats = _build_audit_inventory()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Cross-application audit failed")
        raise HTTPException(status_code=502, detail="Le diagnostic croisé a échoué. Consulte les journaux du conteneur.")
    torrent_files = [f for t in torrents for f in t["files"]]
    # Index by actual filesystem identity first, then by filename and size for bounded content checks.
    identity_index = {}
    candidate_index = {}
    size_index = {}
    for f in library:
        if f["exists"] and f["device"] is not None and f["inode"] is not None:
            identity_index.setdefault((f["device"], f["inode"]), []).append(f)
        if f["size"] is not None:
            candidate_index.setdefault((f["name"].casefold(), f["size"]), []).append(f)
            size_index.setdefault(f["size"], []).append(f)
    matched_torrents = set()
    matched_library = set()
    matches = []
    potential_hardlinks = []
    hash_cache = {}
    hash_budget = {"bytes": 0, "files": 0}
    pair_checks = 0
    for index, torrent_file in enumerate(torrent_files):
        if torrent_file["exists"]:
            peers = identity_index.get((torrent_file["device"], torrent_file["inode"]), [])
            for peer in peers:
                matched_torrents.add(index)
                matched_library.add(id(peer))
                matches.append({"status": "hardlink_confirmed", "torrent": torrent_file, "library": peer})
        if torrent_file["size"] is None:
            continue
        # First look at same-name candidates, then compare other files of the same
        # size too: Radarr/Sonarr and torrent releases can use different filenames
        # for byte-identical media. SHA-256 is required before declaring a match.
        same_name = candidate_index.get((torrent_file["name"].casefold(), torrent_file["size"]), [])
        candidates = list(same_name)
        seen_peers = {id(peer) for peer in candidates}
        candidates.extend(peer for peer in size_index.get(torrent_file["size"], []) if id(peer) not in seen_peers)
        for peer in candidates:
            if not torrent_file["exists"] or not peer["exists"]:
                continue
            if torrent_file["device"] == peer["device"] and torrent_file["inode"] == peer["inode"]:
                continue
            pair_checks += 1
            if pair_checks > MAX_AUDIT_HASH_PAIRS:
                break
            tpath = ROOT / str(torrent_file["local_path"]).lstrip("/")
            lpath = ROOT / str(peer["local_path"]).lstrip("/")
            left_hash = _sha256_bounded(tpath, hash_cache, hash_budget)
            right_hash = _sha256_bounded(lpath, hash_cache, hash_budget)
            if left_hash and right_hash and left_hash == right_hash:
                matched_torrents.add(index)
                matched_library.add(id(peer))
                potential_hardlinks.append({"status": "identical_copy_can_be_hardlinked",
                                            "torrent": torrent_file, "library": peer,
                                            "note": "Contenu identique confirmé par SHA-256, même si les noms diffèrent ; les deux chemins doivent être sur le même système de fichiers pour créer un hardlink."})
            elif left_hash and right_hash:
                # Different content: same size alone is not enough to claim a match.
                continue
            elif id(peer) in seen_peers:
                potential_hardlinks.append({"status": "unverified_name_size_candidate",
                                            "torrent": torrent_file, "library": peer,
                                            "note": "Même nom et même taille, mais comparaison de contenu non réalisée (limite de diagnostic)."})
    for item in matches:
        item["key"] = (item["torrent"]["torrent_hash"], item["torrent"]["remote_path"], item["library"]["service"], item["library"]["file_id"])
    hardlink_count = sum(1 for m in matches if m["status"] == "hardlink_confirmed")
    identical_count = sum(1 for m in potential_hardlinks if m["status"] == "identical_copy_can_be_hardlinked")
    return {
        "read_only": True,
        "cache": cache_stats,
        "summary": {
            "torrents_scanned": len(torrents), "torrent_files_scanned": scanned_torrent_files,
            "library_files_scanned": len(library), "confirmed_hardlink_pairs": hardlink_count,
            "identical_copy_pairs": identical_count, "unverified_candidates": sum(1 for m in potential_hardlinks if m["status"] == "unverified_name_size_candidate"),
            "torrent_files_without_library_match": sum(1 for i, f in enumerate(torrent_files) if i not in matched_torrents),
            "library_files_without_torrent_match": sum(1 for f in library if id(f) not in matched_library),
            "hash_bytes_read": hash_budget["bytes"], "hash_files_read": hash_budget["files"],
        },
        "matches": matches[:1000],
        "potential_hardlinks": potential_hardlinks[:500],
        "torrent_only": [f for i, f in enumerate(torrent_files) if i not in matched_torrents][:500],
        "library_only": [f for f in library if id(f) not in matched_library][:500],
        "torrents": [{"hash": t["hash"], "name": t["name"], "state": t["state"], "size": t["size"],
                      "files_count": len(t["files"]), "unmapped_files": sum(1 for f in t["files"] if not f["mapped"])}
                     for t in torrents],
        "library_items": [{"service": f["service"], "kind": f["kind"], "file_id": f["file_id"],
                           "item_id": f["item_id"], "name": f["name"], "remote_path": f["remote_path"],
                           "local_path": f["local_path"], "exists": f["exists"], "mapped": f["mapped"],
                           "size": f["size"], "nlink": f["nlink"]} for f in library],
        "warnings": [
            "Un fichier sans correspondance n'est pas nécessairement orphelin : un torrent peut être en seed sans bibliothèque, et une bibliothèque peut rester après suppression du torrent.",
            "Les candidats fondés seulement sur le nom et la taille ne sont pas des correspondances confirmées.",
            "Le diagnostic parcourt les torrents par pages jusqu'à 50 000 entrées et analyse jusqu'à 50 000 fichiers ; la lecture SHA-256 reste limitée à 4 Gio par analyse.",
            "Aucune donnée n'a été modifiée. Toutes les actions destructives exigent une confirmation explicite."
        ],
        "truncated": {"torrents": truncated_torrents, "torrent_files": scanned_torrent_files >= MAX_AUDIT_FILES,
                      "hash_comparison": pair_checks > MAX_AUDIT_HASH_PAIRS or hash_budget["bytes"] >= MAX_AUDIT_HASH_TOTAL_BYTES},
    }


@app.post("/api/actions/remove-torrent")
def remove_torrent(payload: TorrentRemoval, user: str = Depends(authenticate)):
    _require_manual_confirmation(payload)
    torrent_hash = payload.torrent_hash.lower()
    if not re.fullmatch(r"[0-9a-f]{40}", torrent_hash):
        raise HTTPException(status_code=400, detail="Hash torrent invalide.")
    # When deleting data, require every torrent file to map into /media so its impact can be inspected.
    if payload.delete_files:
        base, opener = _qbit_session()
        files = _qbit_json(base, opener, "/api/v2/torrents/files", {"hash": torrent_hash})
        info = _qbit_json(base, opener, "/api/v2/torrents/info", {"hashes": torrent_hash})
        if not isinstance(info, list) or not info:
            raise HTTPException(status_code=404, detail="Torrent introuvable dans qBittorrent.")
        if not isinstance(files, list) or not files:
            raise HTTPException(status_code=409, detail="Impossible de vérifier les fichiers du torrent ; suppression des fichiers refusée.")
        save_path = str(info[0].get("save_path", ""))
        arr_references = _arr_file_references()
        for item in files:
            remote = posixpath.normpath(posixpath.join(save_path, str(item.get("name", ""))))
            local = _mapped_local_path("QBITTORRENT", remote)
            if local is None or not local.exists() or local.is_symlink() or not local.is_file():
                raise HTTPException(status_code=409, detail="Au moins un fichier du torrent n'est pas mappé ou vérifiable sous /media. Retrait du torrent sans effacer ses fichiers possible.")
            if any(ref["local_path"] == str(local) for ref in arr_references):
                raise HTTPException(status_code=409, detail="Un fichier du torrent est aussi référencé directement par Radarr/Sonarr au même chemin. Retrait du torrent sans supprimer ses fichiers possible.")
    base, opener = _qbit_session()
    _qbit_post(base, opener, "/api/v2/torrents/delete", {"hashes": torrent_hash, "deleteFiles": "true" if payload.delete_files else "false"})
    logger.warning("MANUAL_ACTION user=%s action=remove_torrent hash=%s delete_files=%s", user, torrent_hash, payload.delete_files)
    return {"status": "removed", "torrent_hash": torrent_hash, "files_deleted": payload.delete_files}


@app.post("/api/actions/remove-library-file")
def remove_library_file(payload: ArrFileRemoval, user: str = Depends(authenticate)):
    _require_manual_confirmation(payload)
    endpoint = "moviefile" if payload.service == "RADARR" else "episodefile"
    # Resolve the file through the service API first and refuse paths that cannot be mapped and checked.
    item = _arr_request(payload.service, f"{endpoint}/{payload.file_id}", "GET")
    if not isinstance(item, dict):
        raise HTTPException(status_code=502, detail="Réponse inattendue de l'application.")
    remote = str(item.get("path", ""))
    local = _mapped_local_path(payload.service, remote)
    if local is None or not local.exists() or local.is_symlink() or not local.is_file():
        raise HTTPException(status_code=409, detail="Chemin non associé ou fichier non vérifiable sous /media ; suppression refusée.")
    references = _arr_file_references()
    same_path_references = [ref for ref in references
                            if ref["local_path"] == str(local)
                            and not (ref["service"] == payload.service and ref["file_id"] == payload.file_id)]
    if same_path_references:
        raise HTTPException(status_code=409, detail="Ce chemin est référencé par un autre fichier de bibliothèque. Résous cette référence avant de supprimer.")
    torrent_references = _qbit_content_references(local)
    if torrent_references:
        raise HTTPException(status_code=409, detail="Ce fichier est situé dans le contenu d'un torrent qBittorrent. Retire d'abord le torrent ou utilise un hardlink distinct après vérification.")
    # This removes the library file path only. If it is a hardlink, other names remain intact.
    _arr_request(payload.service, f"{endpoint}/{payload.file_id}", "DELETE")
    logger.warning("MANUAL_ACTION user=%s action=remove_library_file service=%s file_id=%s path=%s nlink=%s",
                   user, payload.service, payload.file_id, remote, local.stat().st_nlink if local.exists() else "removed")
    return {"status": "removed", "service": payload.service, "file_id": payload.file_id,
            "path": "/" + str(local.relative_to(ROOT)), "note": "Suppression du chemin bibliothèque demandée ; les autres hardlinks éventuels restent en place."}


@app.post("/api/actions/unmonitor")
def unmonitor_item(payload: ArrItemAction, user: str = Depends(authenticate)):
    _require_manual_confirmation(payload)
    endpoint = "movie" if payload.service == "RADARR" else "series"
    item = _arr_request(payload.service, f"{endpoint}/{payload.item_id}", "GET")
    if not isinstance(item, dict):
        raise HTTPException(status_code=404, detail="Élément introuvable.")
    item["monitored"] = False
    _arr_request(payload.service, f"{endpoint}/{payload.item_id}", "PUT", item)
    logger.warning("MANUAL_ACTION user=%s action=unmonitor service=%s item_id=%s", user, payload.service, payload.item_id)
    return {"status": "unmonitored", "service": payload.service, "item_id": payload.item_id}


@app.post("/api/actions/remove-library-item")
def remove_library_item(payload: ArrItemAction, user: str = Depends(authenticate)):
    _require_manual_confirmation(payload)
    endpoint = "movie" if payload.service == "RADARR" else "series"
    # Deliberately never delete files here; remove the library entry and add an exclusion to avoid re-import.
    exclusion_param = "addImportExclusion" if payload.service == "RADARR" else "addImportListExclusion"
    query = urllib.parse.urlencode({"deleteFiles": "false", exclusion_param: "true"})
    _arr_request(payload.service, f"{endpoint}/{payload.item_id}?{query}", "DELETE")
    logger.warning("MANUAL_ACTION user=%s action=remove_library_item service=%s item_id=%s files_deleted=false",
                   user, payload.service, payload.item_id)
    return {"status": "removed_from_library", "service": payload.service, "item_id": payload.item_id,
            "files_deleted": False, "monitoring": "stopped", "exclusion_added": True}


@app.post("/api/actions/remove-empty-folder")
def remove_empty_folder(payload: EmptyFolderRemoval, user: str = Depends(authenticate)):
    _require_manual_confirmation(payload)
    target = safe_path(payload.path)
    if target == ROOT or target.is_symlink() or not target.exists() or not target.is_dir():
        raise HTTPException(status_code=400, detail="Seul un sous-dossier ordinaire existant peut être ciblé.")
    try:
        target.rmdir()  # Fails safely if the directory is not empty.
    except OSError as exc:
        if exc.errno in (39, 66):  # ENOTEMPTY on Linux/macOS
            raise HTTPException(status_code=409, detail="Le dossier n'est pas vide ; aucun contenu n'a été supprimé.")
        raise HTTPException(status_code=400, detail="Impossible de supprimer ce dossier.")
    logger.warning("MANUAL_ACTION user=%s action=remove_empty_folder path=%s", user, payload.path)
    return {"status": "removed", "path": payload.path, "only_if_empty": True}



@app.get("/api/search")
def search(q: str = Query(min_length=1, max_length=200), path: str = Query(default="/"),
           user: str = Depends(authenticate)):
    base = safe_path(path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail="Dossier introuvable")
    query = q.casefold()
    matches = []
    visited = 0
    # Bounded traversal to keep a large library from monopolizing the app.
    for current, dirs, filenames in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
        for name in dirs + filenames:
            visited += 1
            p = Path(current) / name
            if query in name.casefold():
                try:
                    if p.is_symlink():
                        continue
                    matches.append(file_info(p))
                except (OSError, ValueError):
                    pass
            if visited >= 20000:
                return {"results": matches[:500], "truncated": True, "scanned": visited}
    return {"results": matches[:500], "truncated": len(matches) > 500, "scanned": visited}

