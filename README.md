# Pandas Harness

A Docker Compose-ready Flask workspace for exploring CSV and XLSX files with pandas and a PandasAI chat sidebar. Connect your own OpenAI-compatible endpoint, or run locally with LM Studio.

## Run

Requires Docker Engine and Docker Compose v2.

```sh
cp .env.example .env
chmod 600 .env
# Optionally edit .env to preconfigure your endpoint and key.
docker compose up --build -d
```

Open **http://localhost:5000**. Upload a CSV or XLSX workbook, select a dataset, and explore its preview, column types, missing values and duplicate rows. Every workbook sheet becomes a separate dataset.

In **AI settings**:

1. Choose OpenAI-compatible or LM Studio and enter the API base URL.
2. Enter an API key, or leave it blank for a local server that does not require one.
3. Click **Connect & list models** to query `GET /models`.
4. Choose a discovered model and click **Verify model**. This sends a small chat completion request and may incur a provider charge.
5. Ask questions about the selected dataset. Answers can be text, tables or matplotlib charts. Follow-up questions include recent conversation context.

The app uses PandasAI **2.3.2** because it supplies the requested `pandasai.llm.local_llm.LocalLLM` integration, including for OpenAI-compatible hosted endpoints. Python 3.11, pandas 1.5.3 and NumPy 1.26.4 are pinned for compatibility. The web service uses PandasAI only to generate analysis code; it never executes that code.

## LM Studio

Load a code-capable model and start LM Studio's OpenAI-compatible server. Use `http://host.docker.internal:1234/v1` from Docker (not `localhost`, which would refer to the web container). Docker Compose configures the host gateway on Linux. LM Studio may need to listen on an interface reachable from Docker; restrict access with your firewall. No public model service or PandasAI API key is required.

Set `AI_PROVIDER=local` and `AI_API_BASE=http://host.docker.internal:1234/v1` in `.env`, or enter them in settings. The model must support ordinary `/chat/completions` requests and Python/pandas code generation. Listing models verifies discovery; **Verify model** additionally checks that the selected model can respond.

## Credentials and deployment boundaries

- Keys entered in the UI are **write-only**, held only in server memory for that browser workspace, and never included in settings responses, cookies, HTML, browser storage, job files or application logs. The password field is cleared after submission. A blank key retains the existing key **only for the same endpoint**; changing the endpoint never forwards the previous key. Forget connection clears that workspace's copy.
- Keys cannot be hashed because the app must send them to the model endpoint. A server/container administrator can still access server memory or environment variables. “Write-only” protects against retrieval through the application, not a compromised server or browser. Use HTTPS for hosted endpoints and for any non-local browser connection.
- Optional `.env` credentials remain on disk by your choice; protect that file, never commit it, and do not share it. The file is excluded from Docker builds. Environment defaults seed new workspaces; forgetting a key does not edit `.env`, so a new workspace can still inherit it.
- The signed, HttpOnly, SameSite=Strict cookie contains only a random workspace identifier. Mutations require a per-workspace CSRF token. Sessions expire after eight idle hours and uploads/chat/UI-entered keys disappear on restart. The server holds up to 32 workspaces. Keep **one Gunicorn worker**: state is in memory, not shared between processes.
- PandasAI sends schema/sample rows and questions to your chosen endpoint. Use trusted providers and data; AI answers are not guaranteed correct. Disable third-party PandasAI telemetry with `SCARF_NO_ANALYTICS=true` (already set in Docker).
- Model-generated Python runs in the **separate worker container**: no network, API keys, `.env`, Docker socket or host filesystem mounts; read-only root filesystem, dropped capabilities, limited memory/processes/CPU, and a 30-second execution deadline. Only JSON and verified PNGs cross the job/result boundary; no pickle is used. Do not run `worker.py` directly on a sensitive host: its isolation comes from Compose.
- This is a **trusted-user local harness**, not a hardened public multi-tenant service. Analysis jobs share a temporary Docker volume, so generated code could access another queued job's data or disrupt the worker. Configurable endpoint URLs also intentionally allow local/private-network access. Keep the default localhost-only port binding. Add authentication, TLS, endpoint allowlisting and per-job isolation before any shared or public deployment. Do not mount credentials into the worker.
- Temporary job files are removed after each request. A crash may leave files in the job volume; `docker compose down -v` removes the volume and any remaining data.

## Limits

Uploads: 10 MiB per file, UTF-8 CSV or `.xlsx` (not legacy `.xls`). At most eight datasets per workspace, 100,000 rows and 100 columns per dataset, and 64 MiB uncompressed per workbook. Empty sheets/files are rejected. Previews and AI table results show up to 100 rows; full datasets are used for analysis. Chat questions are limited to 4,000 characters, with the most recent 20 messages retained per dataset. One operation per workspace runs at a time.

## Development

```sh
uv python install 3.11
uv venv --python 3.11
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
```

For a UI-only development server (uploads/settings work, but chat requires the Compose worker and its shared job directory):

```sh
SCARF_NO_ANALYTICS=true .venv/bin/flask --app wsgi run
```

Use Compose for end-to-end execution. `docker compose logs web worker` shows operational failures without logging keys or generated-code output.
