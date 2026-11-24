import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

MAX_ROWS = 100_000
MAX_COLUMNS = 100
MAX_DATASETS = 8
SESSION_TTL = 8 * 60 * 60


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


@dataclass
class Workspace:
    endpoint: str
    key: str = field(repr=False)
    provider: str = "openai"
    model: str = ""
    models: list = field(default_factory=list)
    verified: bool = False
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    datasets: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def normalize_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise APIError("Enter a valid API base URL.")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise APIError("Enter a valid API base URL.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and port == 0)
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise APIError("Use an http(s) API base URL without credentials or query parameters.")
    path = parsed.path.rstrip("/") or "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def public_settings(workspace):
    return {
        "endpoint": workspace.endpoint,
        "provider": workspace.provider,
        "model": workspace.model,
        "models": workspace.models,
        "has_key": bool(workspace.key),
        "verified": workspace.verified,
    }


def frame_payload(frame, limit=100):
    return json.loads(frame.head(limit).to_json(orient="split", date_format="iso"))


def dataset_info(dataset_id, dataset):
    frame = dataset["frame"]
    return {
        "id": dataset_id,
        "name": dataset["name"],
        "rows": len(frame),
        "columns": len(frame.columns),
        "missing": int(frame.isna().sum().sum()),
        "duplicates": int(frame.duplicated().sum()),
        "fields": [
            {"name": str(name), "dtype": str(dtype), "missing": int(frame[name].isna().sum())}
            for name, dtype in frame.dtypes.items()
        ],
        "preview": frame_payload(frame),
    }


def read_upload(upload):
    filename = secure_filename(upload.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise APIError("Upload a .csv or .xlsx file.")
    try:
        if suffix == ".xlsx":
            content = io.BytesIO(upload.read())
            with zipfile.ZipFile(content) as archive:
                if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024:
                    raise APIError("Expanded workbook exceeds the 64 MiB limit.")
            content.seek(0)
            with pd.ExcelFile(content, engine="openpyxl") as workbook:
                if len(workbook.sheet_names) > MAX_DATASETS:
                    raise APIError(f"Workbooks may contain at most {MAX_DATASETS} sheets.")
                frames = {
                    f"{filename} / {sheet}": workbook.parse(sheet, nrows=MAX_ROWS + 1)
                    for sheet in workbook.sheet_names
                }
        else:
            frames = {filename: pd.read_csv(upload, nrows=MAX_ROWS + 1)}
    except APIError:
        raise
    except (ValueError, OSError, UnicodeError, zipfile.BadZipFile, KeyError) as error:
        logging.getLogger(__name__).warning("Upload parsing failed: %s", type(error).__name__)
        raise APIError("Unable to read this file. Check its format and use UTF-8 for CSV files.") from None
    for frame in frames.values():
        if frame.empty:
            raise APIError("Files and workbook sheets must contain at least one data row.")
        if len(frame) > MAX_ROWS or len(frame.columns) > MAX_COLUMNS:
            raise APIError(f"Each dataset is limited to {MAX_ROWS:,} rows and {MAX_COLUMNS} columns.")
        frame.columns = frame.columns.map(str)
        if not frame.columns.is_unique:
            raise APIError("Column names must be unique.")
    return frames


def create_app(test_config=None):
    load_dotenv()
    os.environ.setdefault("SCARF_NO_ANALYTICS", "true")
    from .analysis import analyze

    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        ENDPOINT=os.getenv("AI_API_BASE", "https://api.openai.com/v1"),
        API_KEY=os.getenv("AI_API_KEY", ""),
        MODEL=os.getenv("AI_MODEL", ""),
        PROVIDER=os.getenv("AI_PROVIDER", "openai"),
        JOB_DIR=os.getenv("JOB_DIR", "/jobs"),
        ANALYZER=analyze,
    )
    if test_config:
        app.config.update(test_config)
    workspaces = {}
    registry_lock = threading.Lock()
    app.extensions["workspaces"] = workspaces
    for name in ("pandasai", "httpx", "openai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    @app.before_request
    def workspace_and_csrf():
        if request.path == "/health":
            return None
        now = time.monotonic()
        with registry_lock:
            for sid, workspace in list(workspaces.items()):
                if now - workspace.touched > SESSION_TTL and not workspace.lock.locked():
                    del workspaces[sid]
            sid = session.get("sid")
            if sid not in workspaces:
                if len(workspaces) >= 32:
                    raise APIError("Workspace capacity reached. Try again later.", 503)
                session.clear()
                sid = secrets.token_urlsafe(32)
                session["sid"] = sid
                workspaces[sid] = Workspace(
                    endpoint=normalize_endpoint(app.config["ENDPOINT"]),
                    key=app.config["API_KEY"],
                    model=app.config["MODEL"],
                    provider=app.config["PROVIDER"],
                )
            g.workspace = workspaces[sid]
            g.workspace.touched = now
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, g.workspace.csrf):
                raise APIError("Session expired. Reload the page and try again.", 403)
        if request.path.startswith("/api/"):
            if not g.workspace.lock.acquire(blocking=False):
                raise APIError("Another operation is in progress. Please wait.", 409)
            g.workspace_locked = True

    @app.teardown_request
    def release_workspace(_error):
        if getattr(g, "workspace_locked", False):
            g.workspace.lock.release()

    @app.after_request
    def secure_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = "Upload exceeds the 10 MiB limit." if error.code == 413 else error.description
        return jsonify(error=message), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.error("Request failed: %s", type(error).__name__)
        return jsonify(error="The operation failed. Please try again or check the server."), 500

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        return jsonify(
            csrf=g.workspace.csrf,
            settings=public_settings(g.workspace),
            datasets=[dataset_info(key, value) for key, value in g.workspace.datasets.items()],
            history=g.workspace.history,
        )

    @app.post("/api/connection/discover")
    def discover():
        body = request.get_json()
        if not isinstance(body, dict):
            raise APIError("Expected a JSON object.")
        endpoint = normalize_endpoint(body.get("endpoint", ""))
        provider = body.get("provider", "openai")
        if provider not in {"openai", "local"}:
            raise APIError("Choose OpenAI-compatible or LM Studio.")
        key = body.get("api_key", "")
        if (
            not isinstance(key, str) or len(key) > 4096
            or any(not 33 <= ord(c) <= 126 for c in key)
        ):
            raise APIError("Invalid API key.")
        if not key and endpoint == g.workspace.endpoint:
            key = g.workspace.key
        if not key and provider != "local":
            raise APIError("An API key is required for this provider.")
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            with requests.Session() as client:
                client.trust_env = False
                with client.get(
                    f"{endpoint}/models", headers=headers, timeout=(5, 15),
                    allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code != 200:
                        raise APIError(
                            f"Model discovery returned HTTP {response.status_code}. Check the URL and key.", 502
                        )
                    chunks = bytearray()
                    for chunk in response.iter_content(8192):
                        chunks.extend(chunk)
                        if len(chunks) > 1024 * 1024:
                            raise APIError("The model list response is too large.", 502)
                    payload = json.loads(chunks)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Invalid model list")
            models = sorted({
                item["id"] for item in payload["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and 0 < len(item["id"]) <= 256
            })
        except (requests.RequestException, ValueError, UnicodeError):
            raise APIError("Could not read the model list. Check the endpoint, credentials and network.", 502) from None
        if not models:
            raise APIError("The endpoint returned no models. Load a model in your server first.", 502)
        old_model = g.workspace.model
        g.workspace.endpoint = endpoint
        g.workspace.provider = provider
        g.workspace.key = key
        g.workspace.models = models
        g.workspace.model = old_model if old_model in models else models[0]
        g.workspace.verified = False
        return jsonify(settings=public_settings(g.workspace))

    @app.post("/api/connection/verify")
    def verify():
        body = request.get_json()
        if not isinstance(body, dict):
            raise APIError("Expected a JSON object.")
        model = body.get("model")
        if model not in g.workspace.models:
            raise APIError("Discover models and choose one from the returned list.")
        from openai import OpenAI, OpenAIError
        g.workspace.verified = False
        try:
            with OpenAI(
                base_url=g.workspace.endpoint,
                api_key=g.workspace.key or "lm-studio",
                timeout=20, max_retries=0,
            ) as client:
                result = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=16,
                    stream=False,
                )
                if not result.choices or not result.choices[0].message.content:
                    raise APIError("The model returned an empty response.", 502)
        except OpenAIError:
            g.workspace.verified = False
            raise APIError("Chat verification failed. Check the selected model and endpoint.", 502) from None
        g.workspace.model = model
        g.workspace.verified = True
        return jsonify(settings=public_settings(g.workspace))

    @app.delete("/api/connection")
    def clear_connection():
        g.workspace.key = ""
        g.workspace.model = ""
        g.workspace.models = []
        g.workspace.verified = False
        return jsonify(settings=public_settings(g.workspace))

    @app.post("/api/upload")
    def upload():
        if "file" not in request.files:
            raise APIError("Choose a CSV or XLSX file.")
        frames = read_upload(request.files["file"])
        if len(g.workspace.datasets) + len(frames) > MAX_DATASETS:
            raise APIError(f"A workspace supports {MAX_DATASETS} datasets. Remove one first.")
        added = []
        for name, frame in frames.items():
            dataset_id = secrets.token_hex(12)
            dataset = {"name": name, "frame": frame}
            g.workspace.datasets[dataset_id] = dataset
            added.append(dataset_info(dataset_id, dataset))
        return jsonify(datasets=added), 201

    @app.delete("/api/datasets/<dataset_id>")
    def delete_dataset(dataset_id):
        if dataset_id not in g.workspace.datasets:
            raise APIError("Dataset not found.", 404)
        del g.workspace.datasets[dataset_id]
        g.workspace.history.pop(dataset_id, None)
        return "", 204

    @app.post("/api/chat")
    def chat():
        body = request.get_json()
        if not isinstance(body, dict):
            raise APIError("Expected a JSON object.")
        dataset_id = body.get("dataset_id")
        if not isinstance(dataset_id, str) or dataset_id not in g.workspace.datasets:
            raise APIError("Select an uploaded dataset first.")
        question = body.get("question", "")
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise APIError("Enter a question of 1 to 4,000 characters.")
        if not g.workspace.verified:
            raise APIError("Connect and verify a model in AI settings first.")
        history = g.workspace.history.setdefault(dataset_id, [])
        try:
            answer = app.config["ANALYZER"](
                g.workspace.datasets[dataset_id]["frame"], question.strip(),
                history[-6:], g.workspace, Path(app.config["JOB_DIR"]),
            )
        except (RuntimeError, TimeoutError, OSError, ValueError) as error:
            app.logger.warning("Analysis failed: %s", type(error).__name__)
            raise APIError(
                "Analysis failed. Try rephrasing the question, check the model, "
                "and ensure the analysis worker is running.", 502
            ) from None
        history.extend([
            {"role": "user", "type": "text", "value": question.strip()},
            {"role": "assistant", **answer},
        ])
        del history[:-20]
        return jsonify(answer=answer)

    return app
