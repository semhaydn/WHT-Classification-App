import http.client
import os
import re
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ImproperlyConfigured


ROOT = Path(__file__).resolve().parent.parent


def demo_environment(tmp_path):
    env = os.environ.copy()
    env.update(
        TMPDIR=str(tmp_path),
        DJANGO_ENVIRONMENT="production",
        DJANGO_SECRET_KEY="test-only-secret-key-not-for-deployment",
        DJANGO_DEBUG="false",
        DJANGO_ALLOWED_HOSTS="localhost",
        WHT_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        HEROKU_DEMO_MODE="true",
        DJANGO_SUPERUSER_USERNAME="demo-test",
        DJANGO_SUPERUSER_PASSWORD="test-only-password",
        LLM_COST_TYPE_RECOMMENDATIONS_ENABLED="false",
    )
    env.pop("OPENAI_API_KEY", None)
    return env


def manage(env, *args):
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("demo_mode", [None, "false"])
def test_normal_development_settings_keep_existing_database(monkeypatch, demo_mode):
    if demo_mode is None:
        monkeypatch.delenv("HEROKU_DEMO_MODE", raising=False)
    else:
        monkeypatch.setenv("HEROKU_DEMO_MODE", demo_mode)
    monkeypatch.setenv("DJANGO_ENVIRONMENT", "development")
    monkeypatch.setenv("DJANGO_DEBUG", "true")
    config = runpy.run_path(str(ROOT / "config" / "settings.py"))

    assert config["DEBUG"] is True
    assert config["HEROKU_DEMO_MODE"] is False
    assert config["DATABASES"]["default"]["NAME"] == ROOT / "db.sqlite3"


def test_demo_mode_rejects_debug(monkeypatch):
    monkeypatch.setenv("HEROKU_DEMO_MODE", "true")
    monkeypatch.setenv("DJANGO_ENVIRONMENT", "production")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("DJANGO_DEBUG", "true")
    with pytest.raises(ImproperlyConfigured, match="DEBUG=False"):
        runpy.run_path(str(ROOT / "config" / "settings.py"))


def test_bootstrap_refuses_without_opt_in(tmp_path):
    env = demo_environment(tmp_path)
    env.update(HEROKU_DEMO_MODE="false", DJANGO_ENVIRONMENT="development")
    result = manage(env, "bootstrap_heroku_demo")
    assert result.returncode != 0
    assert "HEROKU_DEMO_MODE=true is required" in result.stderr
    assert not (tmp_path / "wht-heroku-demo.sqlite3").exists()


def test_bootstrap_loads_synthetic_data_once(tmp_path):
    env = demo_environment(tmp_path)
    result = manage(env, "bootstrap_heroku_demo")
    assert result.returncode == 0, result.stderr
    assert "Disposable demo database initialized" in result.stdout
    assert (tmp_path / "wht-heroku-demo.sqlite3").exists()

    verify = manage(
        env,
        "shell",
        "-c",
        (
            "from django.contrib.auth import get_user_model; "
            "from reference_data.models import CostType, SupplierCountry, WHTMapping; "
            "from transactions.models import OpenItem, MaterialData, HistoricalCostTypeSuggestion; "
            "u = get_user_model().objects.get(username='demo-test'); "
            "assert u.is_superuser and u.check_password('test-only-password'); "
            "assert CostType.objects.count() == 13; "
            "assert SupplierCountry.objects.count() == 2323; "
            "assert WHTMapping.objects.count() == 47; "
            "assert WHTMapping.objects.filter(status='APPROVED').count() == 0; "
            "assert MaterialData.objects.count() == 224; "
            "assert OpenItem.objects.count() == 42; "
            "assert HistoricalCostTypeSuggestion.objects.count() == 613; "
            "CostType.objects.filter(code='RENTAL').update(name='changed')"
        ),
    )
    assert verify.returncode == 0, verify.stderr

    again = manage(env, "bootstrap_heroku_demo")
    assert again.returncode == 0, again.stderr
    assert "already initialized" in again.stdout
    verify = manage(
        env,
        "shell",
        "-c",
        "from reference_data.models import CostType; "
        "assert CostType.objects.get(code='RENTAL').name == 'changed'",
    )
    assert verify.returncode == 0, verify.stderr


def test_bootstrap_requires_credentials_before_creating_database(tmp_path):
    env = demo_environment(tmp_path)
    env.pop("DJANGO_SUPERUSER_PASSWORD")
    result = manage(env, "bootstrap_heroku_demo")
    assert result.returncode != 0
    assert "DJANGO_SUPERUSER_PASSWORD" in result.stderr
    assert not (tmp_path / "wht-heroku-demo.sqlite3").exists()


@pytest.mark.parametrize("demo_mode", [None, "false"])
def test_web_startup_skips_bootstrap_without_opt_in(tmp_path, demo_mode):
    for name in ("python", "gunicorn"):
        executable = tmp_path / name
        executable.write_text(f"#!/bin/sh\necho invoked-{name}\n")
        executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    if demo_mode is None:
        env.pop("HEROKU_DEMO_MODE", None)
    else:
        env["HEROKU_DEMO_MODE"] = demo_mode
    result = subprocess.run(
        ["bash", "scripts/start_heroku.sh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "invoked-gunicorn"


def test_demo_web_startup_serves_static_and_accepts_login(tmp_path):
    env = demo_environment(tmp_path)
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    env["PYTHONPATH"] = f"{tmp_path}:{env.get('PYTHONPATH', '')}"
    env["DJANGO_SETTINGS_MODULE"] = "demo_test_settings"
    (tmp_path / "demo_test_settings.py").write_text(
        "from config.settings import *\n"
        "STATIC_ROOT = Path(os.environ['TMPDIR']) / 'collected-static'\n"
    )
    collect = manage(env, "collectstatic", "--noinput", "--verbosity", "0")
    assert collect.returncode == 0, collect.stderr

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env["PORT"] = str(port)
    log_path = tmp_path / "web.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            ["bash", "scripts/start_heroku.sh"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    log.flush()
                    pytest.fail(f"Web startup failed:\n{log_path.read_text()}")
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request(
                        "GET",
                        "/login/",
                        headers={"Host": f"localhost:{port}", "X-Forwarded-Proto": "https"},
                    )
                    response = connection.getresponse()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Web startup did not become ready.")
                    time.sleep(0.2)

            body = response.read().decode()
            assert response.status == 200
            token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
            assert token is not None
            csrf_cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            assert "Secure" in response.getheader("Set-Cookie")
            connection.request(
                "POST",
                "/login/",
                body=urlencode(
                    {
                        "username": "demo-test",
                        "password": "test-only-password",
                        "csrfmiddlewaretoken": token.group(1),
                    }
                ),
                headers={
                    "Host": f"localhost:{port}",
                    "X-Forwarded-Proto": "https",
                    "Origin": f"https://localhost:{port}",
                    "Cookie": csrf_cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response = connection.getresponse()
            response.read()
            assert response.status == 302
            assert response.getheader("Location") == "/dashboard/"

            connection.request(
                "GET",
                "/static/admin/css/base.css",
                headers={"Host": f"localhost:{port}", "X-Forwarded-Proto": "https"},
            )
            response = connection.getresponse()
            assert response.status == 200
            assert response.read()
            connection.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
