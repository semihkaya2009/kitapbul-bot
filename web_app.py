import argparse
import asyncio
from http.cookies import SimpleCookie
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from telethon import TelegramClient

import guncelle


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "web_static"
DOWNLOAD_DIR = ROOT_DIR / "web_downloads"
DB_PATH = ROOT_DIR / guncelle.DB_FILE
SESSION_NAME = str(ROOT_DIR / "kitap_tarayici_oturum")
MAX_RESULTS = 80
MAX_FETCH_SELECTION = 20
RESULT_CACHE_TTL = 60 * 60 * 2
APP_PASSWORD = os.environ.get("KITAP_WEB_PASSWORD", "31914886923")
SESSION_COOKIE_NAME = "kitap_web_session"
SESSION_TTL = 60 * 60 * 12

RESULT_CACHE = {}
RESULT_CACHE_LOCK = threading.Lock()
WEB_SESSIONS = {}
WEB_SESSIONS_LOCK = threading.Lock()
TELEGRAM_LOCK = threading.Lock()


def json_response(handler, payload, status=HTTPStatus.OK):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def error_response(handler, message, status=HTTPStatus.BAD_REQUEST):
    json_response(handler, {"ok": False, "error": message}, status)


def redirect_response(handler, location):
    handler.send_response(HTTPStatus.FOUND)
    handler.send_header("Location", location)
    handler.end_headers()


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw_body = handler.rfile.read(length)
    return json.loads(raw_body.decode("utf-8"))


def cookie_value(handler, name):
    raw_cookie = handler.headers.get("Cookie")
    if not raw_cookie:
        return None

    cookie = SimpleCookie()
    cookie.load(raw_cookie)
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def cleanup_web_sessions():
    now = time.time()
    with WEB_SESSIONS_LOCK:
        expired_tokens = [
            token for token, session in WEB_SESSIONS.items()
            if session["expires_at"] <= now
        ]
        for token in expired_tokens:
            WEB_SESSIONS.pop(token, None)


def create_web_session():
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with WEB_SESSIONS_LOCK:
        WEB_SESSIONS[token] = {
            "created_at": time.time(),
            "expires_at": time.time() + SESSION_TTL,
        }
    return token


def destroy_web_session(token):
    if not token:
        return
    with WEB_SESSIONS_LOCK:
        WEB_SESSIONS.pop(token, None)


def is_authenticated(handler):
    cleanup_web_sessions()
    token = cookie_value(handler, SESSION_COOKIE_NAME)
    if not token:
        return False

    with WEB_SESSIONS_LOCK:
        session = WEB_SESSIONS.get(token)
        if not session:
            return False

        if session["expires_at"] <= time.time():
            WEB_SESSIONS.pop(token, None)
            return False

        session["expires_at"] = time.time() + SESSION_TTL
        return True


def require_json_auth(handler):
    if is_authenticated(handler):
        return True

    error_response(handler, "Oturum gerekli. Lütfen tekrar giriş yap.", HTTPStatus.UNAUTHORIZED)
    return False


def cache_result(result):
    result_id = uuid.uuid4().hex
    with RESULT_CACHE_LOCK:
        RESULT_CACHE[result_id] = {
            "created_at": time.time(),
            "result": result,
        }
    return result_id


def cleanup_result_cache():
    now = time.time()
    with RESULT_CACHE_LOCK:
        expired_ids = [
            result_id for result_id, cached in RESULT_CACHE.items()
            if now - cached["created_at"] > RESULT_CACHE_TTL
        ]
        for result_id in expired_ids:
            RESULT_CACHE.pop(result_id, None)


def get_cached_result(result_id):
    with RESULT_CACHE_LOCK:
        cached = RESULT_CACHE.get(result_id)
        if not cached:
            return None
        cached["created_at"] = time.time()
        return cached["result"]


def search_books(query):
    search_tokens = guncelle.arama_tokenlarini_getir(query)
    if not search_tokens:
        return []

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT grup_adi, dosya_adi, grup_id, mesaj_id FROM kitaplar")
        rows = cursor.fetchall()

    matched_results = []
    for result in rows:
        _, file_name, _, _ = result
        score = guncelle.sonuc_arama_skoru(query, search_tokens, file_name)
        if score > 0:
            matched_results.append((score, result))

    matched_results.sort(key=lambda item: (-item[0], guncelle.arama_metnini_normalize_et(item[1][1])))
    return guncelle.tekil_sonuclari_getir(matched_results)[:MAX_RESULTS]


def result_to_payload(result):
    group_name, file_name, _, _, *extra = result
    copy_count = extra[0] if extra else 1
    sources = extra[1] if len(extra) > 1 else [result[:4]]
    result_id = cache_result(result)
    return {
        "id": result_id,
        "group": group_name,
        "file": file_name,
        "copy_count": copy_count,
        "source_count": len(sources),
    }


def safe_filename(file_name):
    clean_name = os.path.basename(file_name).strip()
    clean_name = re.sub(r"[^\w .@()\\[\\]-]+", "_", clean_name, flags=re.UNICODE)
    clean_name = re.sub(r"\s+", " ", clean_name).strip(" .")
    return clean_name or "kitap.pdf"


def unique_download_path(file_name):
    safe_name = safe_filename(file_name)
    return DOWNLOAD_DIR / f"{uuid.uuid4().hex[:10]}-{safe_name}"


def download_url(path):
    return f"/downloads/{path.name}"


async def download_result(result):
    _, fallback_file_name, _, _, *extra = result
    copy_count = extra[0] if extra else 1
    sources = extra[1] if len(extra) > 1 else [result[:4]]
    errors = []

    async with TelegramClient(SESSION_NAME, guncelle.API_ID, guncelle.API_HASH) as client:
        for source_index, source in enumerate(sources, start=1):
            group_name, file_name, group_id, message_id = source
            try:
                message = await client.get_messages(group_id, ids=message_id)
                if not message or not message.media:
                    raise RuntimeError("Mesajda indirilebilir medya yok")

                target_path = unique_download_path(file_name or fallback_file_name)
                downloaded_path = await client.download_media(message, file=str(target_path))
                if not downloaded_path:
                    raise RuntimeError("Telegram dosya yolunu döndürmedi")

                path = Path(downloaded_path)
                return {
                    "ok": True,
                    "file": file_name,
                    "group": group_name,
                    "copy_count": copy_count,
                    "used_source": source_index,
                    "source_count": len(sources),
                    "url": download_url(path),
                    "size": path.stat().st_size if path.exists() else None,
                }
            except Exception as exc:
                errors.append(f"{source_index}/{len(sources)}: {exc}")

    return {
        "ok": False,
        "file": fallback_file_name,
        "error": errors[-1] if errors else "Dosya indirilemedi",
    }


async def download_results(results):
    downloaded = []
    failed = []

    for result in results:
        download_info = await download_result(result)
        if download_info["ok"]:
            downloaded.append(download_info)
        else:
            failed.append(download_info)

    zip_info = None
    if len(downloaded) > 1:
        zip_path = DOWNLOAD_DIR / f"kitaplar-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
        used_names = set()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in downloaded:
                file_path = DOWNLOAD_DIR / Path(item["url"]).name
                arcname = safe_filename(item["file"])
                if arcname in used_names:
                    stem, suffix = os.path.splitext(arcname)
                    arcname = f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
                used_names.add(arcname)
                archive.write(file_path, arcname=arcname)
        zip_info = {
            "file": zip_path.name,
            "url": download_url(zip_path),
            "size": zip_path.stat().st_size,
        }

    return downloaded, failed, zip_info


def run_downloads(results):
    with TELEGRAM_LOCK:
        return asyncio.run(download_results(results))


def serve_file(handler, path, content_type=None, attachment=False, send_body=True):
    if not path.exists() or not path.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND)
        return

    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    handler.send_header("Content-Length", str(path.stat().st_size))
    if attachment:
        handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
    handler.end_headers()
    if send_body:
        with path.open("rb") as file:
            shutil.copyfileobj(file, handler.wfile)


class BookWebHandler(BaseHTTPRequestHandler):
    server_version = "KitapWeb/1.0"

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        self.handle_static_get(send_body=True)

    def do_HEAD(self):
        self.handle_static_get(send_body=False)

    def handle_static_get(self, send_body=True):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            target = "index.html" if is_authenticated(self) else "login.html"
            serve_file(self, STATIC_DIR / target, "text/html; charset=utf-8", send_body=send_body)
            return

        if path == "/login":
            if is_authenticated(self):
                redirect_response(self, "/")
                return
            serve_file(self, STATIC_DIR / "login.html", "text/html; charset=utf-8", send_body=send_body)
            return

        if path.startswith("/static/"):
            relative_path = unquote(path.removeprefix("/static/"))
            target_path = (STATIC_DIR / relative_path).resolve()
            if STATIC_DIR.resolve() not in target_path.parents and target_path != STATIC_DIR.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            serve_file(self, target_path, send_body=send_body)
            return

        if path.startswith("/downloads/"):
            if not is_authenticated(self):
                redirect_response(self, "/login")
                return
            relative_path = unquote(path.removeprefix("/downloads/"))
            target_path = (DOWNLOAD_DIR / relative_path).resolve()
            if DOWNLOAD_DIR.resolve() not in target_path.parents and target_path != DOWNLOAD_DIR.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            serve_file(self, target_path, attachment=True, send_body=send_body)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/login":
                self.handle_login()
                return
            if parsed.path == "/api/logout":
                self.handle_logout()
                return
            if parsed.path == "/api/search":
                if not require_json_auth(self):
                    return
                self.handle_search()
                return
            if parsed.path == "/api/fetch":
                if not require_json_auth(self):
                    return
                self.handle_fetch()
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except json.JSONDecodeError:
            error_response(self, "Geçersiz JSON gövdesi")
        except Exception as exc:
            error_response(self, str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_login(self):
        payload = read_json_body(self)
        password = str(payload.get("password", ""))
        if password != APP_PASSWORD:
            error_response(self, "Şifre hatalı", HTTPStatus.UNAUTHORIZED)
            return

        token = create_web_session()
        json_data = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(json_data)))
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"
        )
        self.end_headers()
        self.wfile.write(json_data)

    def handle_logout(self):
        token = cookie_value(self, SESSION_COOKIE_NAME)
        destroy_web_session(token)
        json_data = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(json_data)))
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        )
        self.end_headers()
        self.wfile.write(json_data)

    def handle_search(self):
        cleanup_result_cache()
        payload = read_json_body(self)
        query = str(payload.get("query", "")).strip()

        if len(query) < 2:
            error_response(self, "Arama için en az 2 karakter yaz")
            return

        started_at = time.time()
        results = search_books(query)
        response_results = [result_to_payload(result) for result in results]
        total_copy_count = sum(item["copy_count"] for item in response_results)

        json_response(self, {
            "ok": True,
            "query": query,
            "duration_ms": int((time.time() - started_at) * 1000),
            "result_count": len(response_results),
            "merged_count": max(0, total_copy_count - len(response_results)),
            "results": response_results,
        })

    def handle_fetch(self):
        payload = read_json_body(self)
        ids = payload.get("ids", [])
        if not isinstance(ids, list):
            error_response(self, "ids listesi bekleniyor")
            return

        ids = [str(result_id) for result_id in ids if str(result_id).strip()]
        ids = list(dict.fromkeys(ids))
        if not ids:
            error_response(self, "En az bir kitap seç")
            return
        if len(ids) > MAX_FETCH_SELECTION:
            error_response(self, f"Tek seferde en fazla {MAX_FETCH_SELECTION} kitap getirilebilir")
            return

        results = []
        missing_ids = []
        for result_id in ids:
            result = get_cached_result(result_id)
            if result is None:
                missing_ids.append(result_id)
            else:
                results.append(result)

        if missing_ids:
            error_response(self, "Bazı seçimlerin süresi dolmuş. Aynı aramayı tekrar yapıp yeniden seç.")
            return

        downloaded, failed, zip_info = run_downloads(results)
        json_response(self, {
            "ok": True,
            "downloaded": downloaded,
            "failed": failed,
            "zip": zip_info,
        })


def main():
    parser = argparse.ArgumentParser(description="Kitap arama web arayüzü")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", default=int(os.environ.get("PORT", 8787)), type=int)
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    STATIC_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), BookWebHandler)
    print(f"Kitap web arayüzü hazır: http://{args.host}:{args.port}")
    print("Not: Aynı Telegram session dosyasını kullanan bot ayrı bir terminalde açıksa dosya indirme çakışabilir.")
    server.serve_forever()


if __name__ == "__main__":
    main()
