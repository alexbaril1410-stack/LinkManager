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
                        "content_path": str(t.get("content_path", ""))[:1024],
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
                    "root_folders": [{"path": str(x.get("path", ""))[:1024], "accessible": x.get("accessible")} for x in roots[:100] if isinstance(x, dict)] if isinstance(roots, list) else [],
                }
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                result["inventory"][key] = {"error": "Connecté, mais inventaire indisponible"}
    return result


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

