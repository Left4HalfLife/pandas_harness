import io
import json
import zipfile

import pandas as pd
import pytest

from harness import normalize_endpoint


def upload(client, headers, content=b"region,sales\nNorth,20\nSouth,30\n"):
    return client.post("/api/upload", headers=headers, data={
        "file": (io.BytesIO(content), "sales.csv"),
    })


def connect(client, headers, provider, key="secret-api-key"):
    return client.post("/api/connection/discover", headers=headers, json={
        "endpoint": provider["url"], "provider": "local", "api_key": key,
    })


def verify(client, headers, model="test-model"):
    return client.post("/api/connection/verify", headers=headers, json={"model": model})


def test_upload_quality_preview_and_session_isolation(app, client, headers):
    response = upload(client, headers, b"region,sales\nNorth,20\nNorth,20\nSouth,\n")
    assert response.status_code == 201
    dataset = response.json["datasets"][0]
    assert (dataset["rows"], dataset["columns"], dataset["missing"], dataset["duplicates"]) == (3, 2, 1, 1)
    assert dataset["preview"]["data"][-1] == ["South", None]
    other = app.test_client()
    assert other.get("/api/state").json["datasets"] == []
    assert other.delete(f"/api/datasets/{dataset['id']}", headers={
        "X-CSRF-Token": other.get("/api/state").json["csrf"],
    }).status_code == 404
    assert client.delete(f"/api/datasets/{dataset['id']}", headers=headers).status_code == 204
    assert client.get("/api/state").json["datasets"] == []


def test_workbook_multiple_sheets(client, headers):
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame({"sales": [1, 2]}).to_excel(writer, sheet_name="January", index=False)
        pd.DataFrame({"sales": [3]}).to_excel(writer, sheet_name="February", index=False)
    stream.seek(0)
    response = client.post("/api/upload", headers=headers, data={"file": (stream, "sales.xlsx")})
    assert response.status_code == 201
    assert [item["name"] for item in response.json["datasets"]] == [
        "sales.xlsx / January", "sales.xlsx / February",
    ]


@pytest.mark.parametrize("filename,content", [
    ("script.py", b"print('no')"), ("bad.xlsx", b"not a workbook"),
    ("empty.csv", b"a,b\n"), ("bad.csv", b"\xff\xfe"),
])
def test_reject_invalid_uploads(client, headers, filename, content):
    response = client.post("/api/upload", headers=headers, data={
        "file": (io.BytesIO(content), filename),
    })
    assert response.status_code == 400
    assert response.json["error"]
    assert client.get("/api/state").json["datasets"] == []


def test_limits_are_enforced(client, headers):
    too_many_rows = b"a\n" + b"1\n" * 100001
    assert upload(client, headers, too_many_rows).status_code == 400
    columns = ",".join(f"c{i}" for i in range(101))
    assert upload(client, headers, f"{columns}\n{','.join(['1'] * 101)}\n".encode()).status_code == 400
    for _ in range(8):
        assert upload(client, headers).status_code == 201
    assert upload(client, headers).status_code == 400
    assert upload(client, headers, b"a" * (10 * 1024 * 1024 + 1)).status_code == 413


def test_expanded_workbook_limit(client, headers):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large", b"a" * (64 * 1024 * 1024 + 1))
    stream.seek(0)
    response = client.post("/api/upload", headers=headers, data={"file": (stream, "bomb.xlsx")})
    assert response.status_code == 400
    assert "Expanded" in response.json["error"]


def test_csrf_required(client, headers):
    assert client.post("/api/upload").status_code == 403
    assert client.delete("/api/connection").status_code == 403
    assert client.delete("/api/connection", headers=headers).status_code == 200


def test_model_discovery_verification_and_write_only_key(client, headers, provider):
    discovered = connect(client, headers, provider)
    assert discovered.status_code == 200
    assert discovered.json["settings"]["models"] == ["another-model", "test-model"]
    assert discovered.json["settings"]["has_key"] is True
    assert discovered.json["settings"]["verified"] is False
    assert verify(client, headers).json["settings"]["verified"] is True
    assert provider["requests"][0] == ("/v1/models", "Bearer secret-api-key")
    assert provider["requests"][1][0] == "/v1/chat/completions"
    for response in [discovered, client.get("/api/state"), client.get("/")]:
        assert b"secret-api-key" not in response.data
        assert "secret-api-key" not in str(response.headers)
    with client.session_transaction() as cookie:
        assert list(cookie) == ["sid"]
    assert connect(client, headers, provider, key="").status_code == 200
    assert provider["requests"][-1][1] == "Bearer secret-api-key"
    cleared = client.delete("/api/connection", headers=headers)
    assert cleared.json["settings"]["has_key"] is False
    assert cleared.json["settings"]["models"] == []


def test_key_not_forwarded_when_endpoint_changes(client, headers, provider):
    assert connect(client, headers, provider).status_code == 200
    changed = client.post("/api/connection/discover", headers=headers, json={
        "endpoint": provider["url"] + "/other", "provider": "local", "api_key": "",
    })
    assert changed.status_code == 200
    assert provider["requests"][-1][1] is None
    assert changed.json["settings"]["has_key"] is False


def test_provider_failures_are_explicit_and_never_echo_secrets(client, headers, provider):
    provider["status"] = 401
    response = connect(client, headers, provider)
    assert response.status_code == 502
    assert "401" in response.json["error"]
    assert "secret-api-key" not in response.text
    provider["status"] = 200
    provider["models"] = []
    assert connect(client, headers, provider).status_code == 502
    assert verify(client, headers).status_code == 400


def test_chat_validation_failure_and_history(app, client, headers, provider):
    dataset = upload(client, headers).json["datasets"][0]
    body = {"dataset_id": dataset["id"], "question": "What is total sales?"}
    assert client.post("/api/chat", headers=headers, json=body).status_code == 400
    connect(client, headers, provider)
    verify(client, headers)
    app.config["ANALYZER"] = lambda *_args: {"type": "text", "value": "50"}
    response = client.post("/api/chat", headers=headers, json=body)
    assert response.json["answer"]["value"] == "50"
    history = client.get("/api/state").json["history"][dataset["id"]]
    assert [item["role"] for item in history] == ["user", "assistant"]

    def failed(*_args):
        raise RuntimeError("secret-api-key upstream details")

    app.config["ANALYZER"] = failed
    response = client.post("/api/chat", headers=headers, json=body)
    assert response.status_code == 502
    assert "secret-api-key" not in response.text
    assert len(client.get("/api/state").json["history"][dataset["id"]]) == 2
    for invalid in ["", " " * 3, "a" * 4001, None]:
        assert client.post("/api/chat", headers=headers, json={**body, "question": invalid}).status_code == 400


def test_environment_defaults_remain_write_only(tmp_path):
    from harness import create_app
    app = create_app({
        "TESTING": True, "API_KEY": "environment-secret",
        "ENDPOINT": "https://example.test/v1", "JOB_DIR": str(tmp_path),
    })
    state = app.test_client().get("/api/state")
    assert state.json["settings"]["has_key"]
    assert "environment-secret" not in state.text


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://user:pass@host/v1",
    "https://example.com/v1?token=secret", "http://example.com/#fragment",
    "http://example.com:bad", "http://example.com:0", "http://example.com/\nfoo", "",
])
def test_invalid_endpoint(url):
    with pytest.raises(Exception, match="URL"):
        normalize_endpoint(url)


def test_bare_endpoint_gets_v1():
    assert normalize_endpoint("http://localhost:1234") == "http://localhost:1234/v1"
    assert normalize_endpoint("https://example.com/api/v1/") == "https://example.com/api/v1"


def test_invalid_json_and_security_headers(client, headers):
    for path in ["/api/chat", "/api/connection/discover", "/api/connection/verify"]:
        response = client.post(path, headers=headers, json=[])
        assert response.status_code == 400
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "secret-api-key" not in json.dumps(response.json)
