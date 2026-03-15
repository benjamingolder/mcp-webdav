import io
import os
import base64
import mimetypes
from datetime import datetime, timezone, date
from pathlib import PurePosixPath
from typing import Any

import httpx
import feedparser
import webdav3.client as wc
from icalendar import Calendar, Event
from dateutil.rrule import rruleset, rrulestr
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# ── WebDAV ──────────────────────────────────────────────────────────────────
WEBDAV_URL      = os.getenv("WEBDAV_URL", "https://lernen.juvecampus.ch/webdav")
WEBDAV_LOGIN    = os.environ["WEBDAV_LOGIN"]
WEBDAV_PASSWORD = os.environ["WEBDAV_PASSWORD"]
WEBDAV_ROOT     = os.getenv("WEBDAV_ROOT", "/")

# ── Kalender & RSS ───────────────────────────────────────────────────────────
ICAL_URL = os.getenv(
    "ICAL_URL",
    "https://lernen.juvecampus.ch/ical/paggregated/194118210/QZhclI.ics",
)
RSS_URL = os.getenv(
    "RSS_URL",
    "https://lernen.juvecampus.ch/rss/personal/u194805814/KHbf96jk/olat.rss",
)

MAX_TEXT_SIZE = int(os.getenv("MAX_TEXT_SIZE_BYTES", str(5 * 1024 * 1024)))  # 5 MB


# ── Helpers ──────────────────────────────────────────────────────────────────
def _webdav_client() -> wc.Client:
    return wc.Client({
        "webdav_hostname": WEBDAV_URL,
        "webdav_login":    WEBDAV_LOGIN,
        "webdav_password": WEBDAV_PASSWORD,
        "webdav_root":     WEBDAV_ROOT,
    })


def _is_text(mime: str | None) -> bool:
    if not mime:
        return False
    return mime.startswith("text/") or mime in {
        "application/json", "application/xml",
        "application/javascript", "application/x-yaml", "application/yaml",
    }


def _fetch(url: str) -> bytes:
    """Simple authenticated GET using WebDAV credentials."""
    with httpx.Client(timeout=30) as client:
        r = client.get(url, auth=(WEBDAV_LOGIN, WEBDAV_PASSWORD))
        r.raise_for_status()
        return r.content


def _dt_to_str(dt: Any) -> str | None:
    """Normalize icalendar dt values to ISO-8601 strings."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


# ── MCP Server ───────────────────────────────────────────────────────────────
mcp = FastMCP("JuventusSchulen")


# ═══════════════════════════════════════════════════════════════════════════════
# WEBDAV TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_directory(path: str = "/") -> list[dict[str, Any]]:
    """Verzeichnis auf dem WebDAV-Server der Juventus-Schule auflisten.

    Args:
        path: Verzeichnispfad (Standard: "/")

    Returns:
        Liste mit name, path, is_dir, size, modified, content_type.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        items = client.list(path, get_info=True)
    except WebDavException as e:
        raise ValueError(f"Kann '{path}' nicht auflisten: {e}") from e

    results = []
    for item in items:
        item_path = item.get("path", "")
        if item_path.rstrip("/") == path.rstrip("/"):
            continue
        results.append({
            "name":         PurePosixPath(item_path.rstrip("/")).name,
            "path":         item_path,
            "is_dir":       item.get("isdir", False),
            "size":         item.get("size"),
            "modified":     item.get("modified"),
            "content_type": item.get("content_type"),
        })
    return results


@mcp.tool()
def search_files(query: str, path: str = "/", recursive: bool = True) -> list[dict[str, Any]]:
    """Dateien und Ordner nach Name durchsuchen (Groß-/Kleinschreibung egal).

    Args:
        query: Suchbegriff.
        path: Startverzeichnis (Standard: "/").
        recursive: Unterverzeichnisse einschliessen (Standard: True).

    Returns:
        Liste passender Einträge mit name, path, is_dir, size, modified.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    query_lower = query.lower()
    results: list[dict[str, Any]] = []

    def _recurse(current: str) -> None:
        try:
            items = client.list(current, get_info=True)
        except WebDavException:
            return
        for item in items:
            item_path = item.get("path", "")
            if item_path.rstrip("/") == current.rstrip("/"):
                continue
            name   = PurePosixPath(item_path.rstrip("/")).name
            is_dir = item.get("isdir", False)
            if query_lower in name.lower():
                results.append({
                    "name":     name,
                    "path":     item_path,
                    "is_dir":   is_dir,
                    "size":     item.get("size"),
                    "modified": item.get("modified"),
                })
            if recursive and is_dir:
                _recurse(item_path)

    _recurse(path)
    return results


@mcp.tool()
def get_file_info(path: str) -> dict[str, Any]:
    """Metadaten einer Datei oder eines Ordners abrufen.

    Args:
        path: Vollständiger Pfad auf dem WebDAV-Server.

    Returns:
        Dict mit name, path, is_dir, size, modified, created, content_type.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        info = client.info(path)
    except WebDavException as e:
        raise ValueError(f"Kann Infos für '{path}' nicht abrufen: {e}") from e
    return {
        "name":         PurePosixPath(path.rstrip("/")).name,
        "path":         path,
        "is_dir":       client.is_dir(path),
        "size":         info.get("size"),
        "modified":     info.get("modified"),
        "created":      info.get("created"),
        "content_type": info.get("content_type"),
    }


@mcp.tool()
def read_file(path: str) -> dict[str, Any]:
    """Dateiinhalt vom WebDAV-Server lesen.

    Textdateien werden als UTF-8 zurückgegeben, Binärdateien als Base64.
    Dateien über MAX_TEXT_SIZE_BYTES (Standard 5 MB) werden abgelehnt.

    Args:
        path: Vollständiger Pfad zur Datei.

    Returns:
        Dict mit path, encoding, content, size, mime_type.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        info = client.info(path)
        size = int(info.get("size") or 0)
    except WebDavException as e:
        raise ValueError(f"Kein Zugriff auf '{path}': {e}") from e

    if size > MAX_TEXT_SIZE:
        raise ValueError(
            f"Datei ist {size:,} Bytes und überschreitet das Limit von {MAX_TEXT_SIZE:,} Bytes."
        )

    buf = io.BytesIO()
    try:
        client.download_from(buf, path)
    except WebDavException as e:
        raise ValueError(f"Download von '{path}' fehlgeschlagen: {e}") from e

    raw  = buf.getvalue()
    mime, _ = mimetypes.guess_type(path)

    if _is_text(mime):
        try:
            return {"path": path, "encoding": "utf-8",   "content": raw.decode("utf-8"), "size": len(raw), "mime_type": mime}
        except UnicodeDecodeError:
            pass
    return {"path": path, "encoding": "base64", "content": base64.b64encode(raw).decode("ascii"), "size": len(raw), "mime_type": mime}


@mcp.tool()
def create_folder(path: str) -> dict[str, str]:
    """Neuen Ordner auf dem WebDAV-Server erstellen.

    Args:
        path: Pfad des neuen Ordners.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        client.mkdir(path)
    except WebDavException as e:
        raise ValueError(f"Ordner '{path}' konnte nicht erstellt werden: {e}") from e
    return {"status": "erstellt", "path": path}


@mcp.tool()
def delete_item(path: str) -> dict[str, str]:
    """Datei oder Ordner vom WebDAV-Server löschen.

    Args:
        path: Pfad des zu löschenden Eintrags.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        client.clean(path)
    except WebDavException as e:
        raise ValueError(f"'{path}' konnte nicht gelöscht werden: {e}") from e
    return {"status": "gelöscht", "path": path}


@mcp.tool()
def move_item(source: str, destination: str) -> dict[str, str]:
    """Datei oder Ordner verschieben / umbenennen.

    Args:
        source: Aktueller Pfad.
        destination: Zielpfad (anderer Name = Umbenennen).
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        client.move(remote_path_from=source, remote_path_to=destination)
    except WebDavException as e:
        raise ValueError(f"Verschieben '{source}' → '{destination}' fehlgeschlagen: {e}") from e
    return {"status": "verschoben", "source": source, "destination": destination}


@mcp.tool()
def copy_item(source: str, destination: str) -> dict[str, str]:
    """Datei oder Ordner kopieren.

    Args:
        source: Quellpfad.
        destination: Zielpfad.
    """
    from webdav3.exceptions import WebDavException
    client = _webdav_client()
    try:
        client.copy(remote_path_from=source, remote_path_to=destination)
    except WebDavException as e:
        raise ValueError(f"Kopieren '{source}' → '{destination}' fehlgeschlagen: {e}") from e
    return {"status": "kopiert", "source": source, "destination": destination}


# ═══════════════════════════════════════════════════════════════════════════════
# KALENDER TOOLS (iCal)
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_ical() -> Calendar:
    raw = _fetch(ICAL_URL)
    return Calendar.from_ical(raw)


def _event_to_dict(component: Event) -> dict[str, Any]:
    return {
        "uid":         str(component.get("UID", "")),
        "summary":     str(component.get("SUMMARY", "")),
        "description": str(component.get("DESCRIPTION", "")),
        "location":    str(component.get("LOCATION", "")),
        "start":       _dt_to_str(component.get("DTSTART", {}).dt if component.get("DTSTART") else None),
        "end":         _dt_to_str(component.get("DTEND",   {}).dt if component.get("DTEND")   else None),
        "status":      str(component.get("STATUS", "")),
        "organizer":   str(component.get("ORGANIZER", "")),
        "url":         str(component.get("URL", "")),
    }


@mcp.tool()
def get_upcoming_events(days: int = 30) -> list[dict[str, Any]]:
    """Kommende Schulereignisse aus dem Juventus-Kalender abrufen.

    Args:
        days: Anzahl Tage ab heute (Standard: 30).

    Returns:
        Liste von Ereignissen mit uid, summary, description, location,
        start, end, status, organizer, url. Sortiert nach Startdatum.
    """
    cal   = _parse_ical()
    now   = datetime.now(tz=timezone.utc)
    cutoff = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from datetime import timedelta
    end_dt = cutoff + timedelta(days=days)

    results = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        start = dtstart.dt
        # Normalize to datetime with tz
        if isinstance(start, date) and not isinstance(start, datetime):
            start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        elif isinstance(start, datetime) and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        if cutoff <= start <= end_dt:
            results.append(_event_to_dict(component))

    results.sort(key=lambda e: e["start"] or "")
    return results


@mcp.tool()
def search_events(query: str) -> list[dict[str, Any]]:
    """Kalendereinträge nach Stichwort durchsuchen (Titel, Beschreibung, Ort).

    Args:
        query: Suchbegriff (Groß-/Kleinschreibung egal).

    Returns:
        Passende Ereignisse, sortiert nach Startdatum.
    """
    cal   = _parse_ical()
    q     = query.lower()
    results = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        haystack = " ".join([
            str(component.get("SUMMARY",     "")),
            str(component.get("DESCRIPTION", "")),
            str(component.get("LOCATION",    "")),
        ]).lower()
        if q in haystack:
            results.append(_event_to_dict(component))

    results.sort(key=lambda e: e["start"] or "")
    return results


@mcp.tool()
def get_events_in_range(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Ereignisse in einem bestimmten Datumsbereich abrufen.

    Args:
        start_date: Startdatum im Format YYYY-MM-DD.
        end_date:   Enddatum im Format YYYY-MM-DD (inklusiv).

    Returns:
        Ereignisse in diesem Zeitraum, sortiert nach Startdatum.
    """
    from datetime import timedelta
    cal    = _parse_ical()
    start  = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end    = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
    results = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        ev_start = dtstart.dt
        if isinstance(ev_start, date) and not isinstance(ev_start, datetime):
            ev_start = datetime(ev_start.year, ev_start.month, ev_start.day, tzinfo=timezone.utc)
        elif isinstance(ev_start, datetime) and ev_start.tzinfo is None:
            ev_start = ev_start.replace(tzinfo=timezone.utc)
        if start <= ev_start < end:
            results.append(_event_to_dict(component))

    results.sort(key=lambda e: e["start"] or "")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# RSS NEWS TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_rss() -> feedparser.FeedParserDict:
    raw = _fetch(RSS_URL)
    return feedparser.parse(raw)


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    published = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            published = str(getattr(entry, "published", ""))

    return {
        "title":       getattr(entry, "title",   ""),
        "link":        getattr(entry, "link",    ""),
        "summary":     getattr(entry, "summary", ""),
        "published":   published,
        "author":      getattr(entry, "author",  ""),
        "id":          getattr(entry, "id",       ""),
        "tags":        [t.term for t in getattr(entry, "tags", [])],
    }


@mcp.tool()
def get_news(limit: int = 20) -> list[dict[str, Any]]:
    """Neueste Einträge aus dem Juventus-Schul-Newsfeed abrufen.

    Args:
        limit: Maximale Anzahl Einträge (Standard: 20).

    Returns:
        Liste mit title, link, summary, published, author, id, tags.
        Neuste zuerst.
    """
    feed = _parse_rss()
    entries = feed.entries[:limit]
    return [_entry_to_dict(e) for e in entries]


@mcp.tool()
def search_news(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Newsfeed nach Stichwort durchsuchen (Titel und Zusammenfassung).

    Args:
        query: Suchbegriff (Groß-/Kleinschreibung egal).
        limit: Maximale Anzahl zu durchsuchender Einträge (Standard: 50).

    Returns:
        Passende News-Einträge, neuste zuerst.
    """
    feed    = _parse_rss()
    q       = query.lower()
    results = []
    for entry in feed.entries[:limit]:
        haystack = f"{getattr(entry, 'title', '')} {getattr(entry, 'summary', '')}".lower()
        if q in haystack:
            results.append(_entry_to_dict(entry))
    return results


@mcp.tool()
def get_feed_info() -> dict[str, Any]:
    """Metadaten des Juventus-News-RSS-Feeds abrufen (Titel, Beschreibung, etc.).

    Returns:
        Dict mit title, description, link, language, updated.
    """
    feed = _parse_rss()
    f    = feed.feed
    updated = None
    if hasattr(f, "updated_parsed") and f.updated_parsed:
        try:
            updated = datetime(*f.updated_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            updated = str(getattr(f, "updated", ""))
    return {
        "title":       getattr(f, "title",       ""),
        "description": getattr(f, "description", ""),
        "link":        getattr(f, "link",        ""),
        "language":    getattr(f, "language",    ""),
        "updated":     updated,
        "total_entries": len(feed.entries),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import uvicorn

    transport = os.getenv("MCP_TRANSPORT", "sse")
    host      = os.getenv("MCP_HOST", "0.0.0.0")
    port      = int(os.getenv("MCP_PORT", "8000"))

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"JuventusSchulen MCP Server läuft auf http://{host}:{port}/sse", file=sys.stderr)
        uvicorn.run(mcp.get_asgi_app(), host=host, port=port)
