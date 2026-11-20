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
            "from transactions.models import OpenItem, MaterialData, HistoricalCostTypeSuggestion, ImportBatch; "
            "from transactions.services.cost_type_ai.recommendation import recommendations_enabled; "
            "u = get_user_model().objects.get(username='demo-test'); "
            "assert u.is_superuser and u.check_password('test-only-password'); "
            "assert CostType.objects.count() == 13; "
            "assert SupplierCountry.objects.count() == 2323; "
            "assert WHTMapping.objects.count() == 47; "
            "assert WHTMapping.objects.filter(status='APPROVED', "
            "effective_from='2024-03-01', effective_to='2027-12-01', "
            "approved_by=u, approved_at__isnull=False).count() == 47; "
            "assert WHTMapping.objects.filter(status='DRAFT').count() == 0; "
            "assert MaterialData.objects.count() == 0; "
            "assert OpenItem.objects.count() == 0; "
            "assert HistoricalCostTypeSuggestion.objects.count() == 0; "
            "assert ImportBatch.objects.count() == 0; "
            "assert not recommendations_enabled(); "
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


@pytest.mark.parametrize("existing", ["unmarked", "legacy", "missing-database"])
def test_bootstrap_refuses_incomplete_or_legacy_state(tmp_path, existing):
    env = demo_environment(tmp_path)
    database = tmp_path / "wht-heroku-demo.sqlite3"
    marker = tmp_path / "wht-heroku-demo.initialized"
    if existing != "missing-database":
        database.write_bytes(b"existing data must be preserved")
    if existing != "unmarked":
        marker.write_text("")

    result = manage(env, "bootstrap_heroku_demo")

    assert result.returncode != 0
    expected_message = "missing" if existing == "missing-database" else "existing"
    assert expected_message in result.stderr.lower()
    if existing != "missing-database":
        assert database.read_bytes() == b"existing data must be preserved"
    assert marker.exists() == (existing != "unmarked")


def test_uat_files_import_through_import_center(tmp_path):
    env = demo_environment(tmp_path)
    result = manage(env, "bootstrap_heroku_demo")
    assert result.returncode == 0, result.stderr

    script = """
import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from reference_data.models import CostType
from transactions.models import ImportBatch, ImportUploadSession, MaterialData, MaterialMatch, OpenItem
from transactions.services.classification import complete_classification, save_final_classification
from transactions.services.gl_eligibility import get_gl_eligibility_map, is_open_item_classifiable
from transactions.services.import_center import _parse_csv
from transactions.services.review import finalize_invoice
from transactions.services.wht_determination import determine_wht

client = Client(HTTP_HOST="localhost")
assert client.login(username="demo-test", password="test-only-password")
folder = Path("data") / "uat_scenarios" / "realistic_v1"
for field, filename, model, expected in (
    ("material_data_file", "material_data_uat.csv", MaterialData, 152),
    ("open_items_file", "open_line_items_uat.csv", OpenItem, 353),
):
    upload = SimpleUploadedFile(filename, (folder / filename).read_bytes(), content_type="text/csv")
    response = client.post("/imports/", {field: upload}, secure=True)
    assert response.status_code == 302, response.content
    session = ImportUploadSession.objects.latest("created_at")
    assert session.preview["can_import"]
    assert model.objects.count() == 0
    response = client.post(response.url, secure=True)
    assert response.status_code == 302
    session.refresh_from_db()
    assert session.status == ImportUploadSession.Status.COMPLETED
    assert model.objects.count() == expected

filename = "uat_scenarios.csv"
for kind in ("material", "open_items"):
    try:
        _parse_csv((folder / filename).read_bytes(), filename, kind)
    except ValidationError as error:
        assert "missing required columns" in str(error)
    else:
        raise AssertionError("Evaluation CSV was accepted as import data")

assert MaterialData.objects.count() == 152
assert OpenItem.objects.count() == 353
assert ImportBatch.objects.count() == 2
assert client.get("/data/uat_scenarios/realistic_v1/uat_scenarios.csv", secure=True).status_code == 404
assert Client(HTTP_HOST="localhost").get(
    "/data/uat_scenarios/realistic_v1/uat_scenarios.csv", secure=True
).status_code == 404

with (folder / "uat_scenarios.csv").open(newline="", encoding="utf-8-sig") as source:
    rows = list(csv.DictReader(source))
items = {
    (item.entity_code, item.accounting_document_number, item.line_number): item
    for item in OpenItem.objects.all()
}
matches = {match.open_item_id: match for match in MaterialMatch.objects.all()}
eligibility = get_gl_eligibility_map(item.gl_account for item in items.values())
assert len(items) == len(matches) == len(rows) == 353
for row in rows:
    item = items[(row["Entity"], row["Document Number"], row["Line Number"])]
    assert matches[item.pk].status == row["Expected MaterialData Match Status"]
    assert is_open_item_classifiable(item, eligibility, set()) == (
        row["Expected Classification Eligibility"] == "ELIGIBLE"
    )
expenses = [row for row in rows if row["Line Type"] == "ELIGIBLE_EXPENSE"]
counts = Counter((row["Entity"], row["Document Number"]) for row in expenses)
user = get_user_model().objects.get(username="demo-test")
for treatment in ("POSITIVE_RATE", "ZERO_RATE", "MISSING_MAPPING"):
    row = next(row for row in expenses if row["Expected WHT Treatment"] == treatment
               and (treatment == "MISSING_MAPPING"
                    or counts[(row["Entity"], row["Document Number"])] == 1))
    item = OpenItem.objects.get(
        entity_code=row["Entity"], accounting_document_number=row["Document Number"],
        line_number=row["Line Number"],
    )
    decision = save_final_classification(
        item, CostType.objects.get(code=row["Expected Final Cost Type"]),
        "Synthetic UAT validation", user,
    )
    result = determine_wht(item, decision, user=user)
    assert result.status == row["Expected WHT Status"]
    expected_amount = row["Expected Line WHT Amount"]
    assert result.wht_amount_snapshot == (Decimal(expected_amount) if expected_amount else None)
    complete_classification(item, user)
    if treatment != "MISSING_MAPPING":
        snapshot = finalize_invoice(item, user)
        assert snapshot.calculated_wht == Decimal(row["Expected Invoice Calculated WHT"])
        assert snapshot.booked_wht == Decimal(row["Expected Invoice Posted WHT Amount"])
        assert snapshot.modification_required == Decimal(row["Expected Reconciliation Difference"])
"""
    result = manage(env, "shell", "-c", script)
    assert result.returncode == 0, result.stderr

    again = manage(env, "bootstrap_heroku_demo")
    assert again.returncode == 0, again.stderr
    result = manage(
        env, "shell", "-c",
        "from transactions.models import ClassificationDecision, MaterialData, OpenItem, ReviewDocumentSnapshot; "
        "assert MaterialData.objects.count() == 152; "
        "assert OpenItem.objects.count() == 353; "
        "assert ClassificationDecision.objects.count() == 3; "
        "assert ReviewDocumentSnapshot.objects.count() == 2",
    )
    assert result.returncode == 0, result.stderr


def test_failed_approval_rolls_back_and_blocks_unsafe_retry(tmp_path):
    env = demo_environment(tmp_path)
    script = """
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from reference_data.management.commands import approve_draft_wht_mappings
from reference_data.models import CostType, SupplierCountry, WHTMapping

original = approve_draft_wht_mappings.approve_wht_rule
calls = 0
def fail_second(rule_id, user):
    global calls
    calls += 1
    if calls == 2:
        raise ValueError("synthetic approval failure")
    return original(rule_id, user)

with patch.object(approve_draft_wht_mappings, "approve_wht_rule", fail_second):
    try:
        call_command("bootstrap_heroku_demo")
    except CommandError as error:
        assert "rolled back" in str(error)
    else:
        raise AssertionError("Approval unexpectedly succeeded")
assert calls == 2
assert not get_user_model().objects.exists()
assert not CostType.objects.exists()
assert not SupplierCountry.objects.exists()
assert not WHTMapping.objects.exists()
"""
    result = manage(env, "shell", "-c", script)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "wht-heroku-demo.initialized").exists()
    retry = manage(env, "bootstrap_heroku_demo")
    assert retry.returncode != 0
    assert "Unmarked demo database already exists" in retry.stderr


def test_bootstrap_requires_credentials_before_creating_database(tmp_path):
    env = demo_environment(tmp_path)
    env.pop("DJANGO_SUPERUSER_PASSWORD")
    result = manage(env, "bootstrap_heroku_demo")
    assert result.returncode != 0
    assert "DJANGO_SUPERUSER_PASSWORD" in result.stderr
    assert not (tmp_path / "wht-heroku-demo.sqlite3").exists()


@pytest.mark.parametrize("demo_mode", [None, "false"])
@pytest.mark.skipif(os.name == "nt", reason="Bash startup is exercised on Linux")
def test_web_startup_skips_bootstrap_without_opt_in(tmp_path, demo_mode):
    for name in ("python", "gunicorn"):
        executable = tmp_path / name
        executable.write_text(f"#!/bin/sh\necho invoked-{name}\n")
        executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(tmp_path), env["PATH"]))
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


@pytest.mark.skipif(os.name == "nt", reason="Bash startup is exercised on Linux")
def test_web_startup_stops_when_bootstrap_fails(tmp_path):
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\necho 'synthetic bootstrap failure' >&2\nexit 7\n")
    python.chmod(0o755)
    gunicorn = tmp_path / "gunicorn"
    gunicorn.write_text("#!/bin/sh\necho unexpected-gunicorn\n")
    gunicorn.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(tmp_path), env["PATH"]))
    env["HEROKU_DEMO_MODE"] = "true"

    result = subprocess.run(
        ["bash", "scripts/start_heroku.sh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7
    assert "synthetic bootstrap failure" in result.stderr
    assert "unexpected-gunicorn" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Gunicorn startup is exercised on Linux")
def test_demo_web_startup_serves_static_and_accepts_login(tmp_path):
    env = demo_environment(tmp_path)
    env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), env["PATH"]))
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env.get("PYTHONPATH", "")))
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
