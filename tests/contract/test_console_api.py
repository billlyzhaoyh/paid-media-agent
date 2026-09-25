"""The localhost console: token and host checks, static page, and action endpoints."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paid_media_agent.admin.server import create_console_app

TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def workspace(tmp_path: Path, project_root: Path) -> Path:
    for name in ("instructions.md", ".env.example"):
        shutil.copy(project_root / name, tmp_path / name)
    for directory in ("skills", "config", "docs"):
        shutil.copytree(project_root / directory, tmp_path / directory)
    (tmp_path / "workspace").mkdir()
    return tmp_path


@pytest.fixture
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for name in ("ANTHROPIC_API_KEY", "PIPEBOARD_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    app = create_console_app(workspace, token=TOKEN)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_page_and_static_assets_are_served_with_csp(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Paid Media Agent" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    for name in (
        "logo-docker.svg",
        "logo-google.svg",
        "logo-meta.svg",
        "logo-linkedin.svg",
        "logo-reddit.svg",
        "logo-x.svg",
        "logo-bigquery.svg",
    ):
        mark = client.get(f"/static/{name}")
        assert mark.status_code == 200, name
        assert mark.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/static/../pyproject.toml").status_code in (404, 400)


def test_api_requires_token_and_local_host(client: TestClient) -> None:
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Admin-Token": "wrong"}).status_code == 401
    foreign = client.get("/api/status", headers={"X-Admin-Token": TOKEN, "Host": "evil.example"})
    assert foreign.status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/models", json={"provider": "anthropic"}).status_code == 401


def test_model_catalog_endpoint_uses_a_draft_key_without_saving(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paid_media_agent.admin import actions

    def catalog(root: Path, provider: str, *, api_key: str = "") -> actions.ActionResult:
        assert root == workspace and provider == "openrouter" and api_key == "draft-key"
        return actions.ActionResult(
            action="models_list",
            ok=True,
            status="ok",
            summary="Loaded",
            detail={
                "models": [
                    {
                        "id": "openai:provider/new-model",
                        "name": "New model",
                        "provider": "provider",
                    }
                ]
            },
        )

    before = (workspace / ".env").read_bytes() if (workspace / ".env").exists() else None
    monkeypatch.setattr(actions, "models_list", catalog)
    response = client.post(
        "/api/models",
        headers={"X-Admin-Token": TOKEN},
        json={"provider": "openrouter", "api_key": "draft-key"},
    )
    assert response.status_code == 200 and response.json()["ok"]
    assert "draft-key" not in response.text
    after = (workspace / ".env").read_bytes() if (workspace / ".env").exists() else None
    assert after == before


def test_status_routes_and_config_round_trip(client: TestClient, workspace: Path) -> None:
    headers = {"X-Admin-Token": TOKEN}
    data = client.get("/api/status", headers=headers).json()
    assert [r["id"] for r in data["routes"]] == [
        "local",
        "pipeboard",
        "org",
        "direct",
        "self_hosted",
        "slack",
    ]
    assert [p["name"] for p in data["processes"]] == ["serve"]
    posted = client.post(
        "/api/config",
        headers=headers,
        json={
            "updates": {"PAID_MEDIA_MODEL": "openai:gpt-5.5", "OPENAI_API_KEY": "sk-" + "z" * 24}
        },
    ).json()
    assert posted["ok"] is True
    config = client.get("/api/config", headers=headers).json()
    keys = {k["name"]: k for k in config["detail"]["keys"]}
    assert (
        keys["OPENAI_API_KEY"]["value"] == "••••••••"
        and keys["PAID_MEDIA_MODEL"]["value"] == "openai:gpt-5.5"
    )
    assert "z" * 24 not in config.__repr__()
    bad = client.post("/api/config", headers=headers, json={"updates": {"EVIL": "x"}}).json()
    assert bad["status"] == "fail"
    routes = {r["id"]: r for r in client.get("/api/status", headers=headers).json()["routes"]}
    assert {s["id"]: s["status"] for s in routes["local"]["steps"]}["model"] == "done"


def test_actions_accounts_policy_and_kill_switch(client: TestClient, workspace: Path) -> None:
    headers = {"X-Admin-Token": TOKEN}
    assert client.post("/api/actions/nope", headers=headers, json={}).status_code == 404
    discovered = client.post("/api/actions/accounts_discover", headers=headers, json={}).json()
    first = discovered["detail"]["accounts"][0]
    assert first["mapped_alias"], "fixture accounts arrive already mapped to the example aliases"
    added = client.post(
        "/api/accounts",
        headers=headers,
        json={
            "alias": "primary",
            "platform": first["platform"],
            "provider_account_id": first["provider_account_id"] + "-new",
            "currency": "USD",
            "timezone": "UTC",
        },
    ).json()
    assert added["ok"], added
    assert (workspace / "config" / "accounts.toml").exists()
    assert client.delete("/api/accounts/primary", headers=headers).json()["ok"]
    policy = client.post(
        "/api/actions/policy_validate", headers=headers, json={"live": False}
    ).json()
    assert policy["ok"] and len(policy["detail"]["admitted"]) == 6
    engaged = client.post("/api/kill-switch", headers=headers, json={"engaged": True}).json()
    assert engaged["ok"]
    banner = client.get("/api/status", headers=headers).json()["result"]["detail"]["writes"][
        "kill_switch_engaged"
    ]
    assert banner is True
    assert (
        client.post("/api/kill-switch", headers=headers, json={"engaged": False}).json()["status"]
        == "fail"
    )
    assert client.post(
        "/api/kill-switch", headers=headers, json={"engaged": False, "confirm": True}
    ).json()["ok"]
    generated = client.post("/api/actions/generate_secrets", headers=headers, json={}).json()
    assert generated["ok"] and generated["detail"]["api_token_show_once"].endswith(":operator")


def test_demo_runs_through_the_console(client: TestClient) -> None:
    headers = {"X-Admin-Token": TOKEN}
    result = client.post(
        "/api/actions/demo_run", headers=headers, json={"with_proposal": True}
    ).json()
    assert result["ok"], result["summary"]
    assert "Comparison window" in result["detail"]["answer"]
    assert result["detail"]["receipt"]["status"] == "verified"


def test_process_endpoints_run_only_fixed_templates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from paid_media_agent.admin.processes import PROCESS_TEMPLATES

    headers = {"X-Admin-Token": TOKEN}
    assert client.post("/api/processes/unknown/start", headers=headers).status_code == 409
    for removed in ("mda-deploy", "slack"):
        assert client.post(f"/api/processes/{removed}/start", headers=headers).status_code == 409
    log = client.get("/api/processes/serve/log", headers=headers).json()
    assert log["name"] == "serve" and log["running"] is False
    monkeypatch.setitem(PROCESS_TEMPLATES, "serve", (sys.executable, "-c", "print('served')"))
    manager = client.app.state.console.processes
    try:
        started = client.post("/api/processes/serve/start", headers=headers)
        assert started.status_code == 200 and started.json()["command"] == (
            "uv run paid-media-agent serve"
        )
        manager._procs["serve"].wait(timeout=5)
        assert manager.view("serve").state == "completed"
        assert "served" in manager.tail("serve")
    finally:
        manager.stop_all()


def test_state_check_runs_through_the_console(client: TestClient, workspace: Path) -> None:
    headers = {"X-Admin-Token": TOKEN}
    result = client.post("/api/actions/state_test", headers=headers, json={}).json()
    assert result["ok"], result["summary"]
    assert (workspace / "workspace" / "state" / "pma.duckdb").is_file()
    for removed in ("database_test", "mda_check", "sandbox_test", "snapshot_check"):
        assert client.post(f"/api/actions/{removed}", headers=headers, json={}).status_code == 404
    assert client.post("/api/slack/icon", headers=headers, content=b"x").status_code in (404, 405)


def test_token_less_mode_accepts_same_origin_only(workspace: Path) -> None:
    """A coding agent's browser pane can only open a plain URL, so the token is replaced by a
    same-origin rule: other sites cannot drive the console even though it runs on localhost."""
    app = create_console_app(workspace, token=TOKEN, require_token=False)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.get("/api/status").status_code == 200, "no token needed from the page itself"
    same = {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}
    assert client.get("/api/config", headers=same).status_code == 200
    cross = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.get("/api/config", headers=cross).status_code == 403
    assert client.get("/api/config", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403


def test_connection_checks_expire_when_the_connection_changes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved is not verified; a different key invalidates verification, data mode does not."""
    from paid_media_agent.admin import actions

    headers = {"X-Admin-Token": TOKEN}
    monkeypatch.delenv("PAID_MEDIA_FIXTURE_ANCHOR", raising=False)
    monkeypatch.setattr(
        actions,
        "model_test",
        lambda _root: actions.ActionResult(
            action="model_test", ok=True, status="ok", summary="tested"
        ),
    )
    client.post("/api/actions/model_test", headers=headers, json={})
    client.post(
        "/api/config", headers=headers, json={"updates": {"PAID_MEDIA_DATA_MODE": "sample"}}
    )
    assert (
        client.get("/api/status", headers=headers).json()["connection_checks"]["model_test"][
            "status"
        ]
        == "ok"
    )
    client.post(
        "/api/config", headers=headers, json={"updates": {"ANTHROPIC_API_KEY": "unused-new-key"}}
    )
    assert (
        "model_test" not in client.get("/api/status", headers=headers).json()["connection_checks"]
    )


def test_model_check_expires_when_shell_key_changes_behind_blank_env_entry(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check stamps must use the effective key, including the shell fallback."""
    from paid_media_agent.admin import actions, envfile

    monkeypatch.setattr(envfile, "_EXPORTED_BY_CONSOLE", set())
    envfile.write_env(
        workspace, {"PAID_MEDIA_MODEL": "anthropic:claude-sonnet-4-6", "ANTHROPIC_API_KEY": ""}
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "first-shell-test-key")
    monkeypatch.setattr(
        actions,
        "model_test",
        lambda _root: actions.ActionResult(
            action="model_test", ok=True, status="ok", summary="tested"
        ),
    )
    headers = {"X-Admin-Token": TOKEN}
    client.post("/api/actions/model_test", headers=headers, json={})
    assert "model_test" in client.get("/api/status", headers=headers).json()["connection_checks"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "second-shell-test-key")
    assert (
        "model_test" not in client.get("/api/status", headers=headers).json()["connection_checks"]
    )
