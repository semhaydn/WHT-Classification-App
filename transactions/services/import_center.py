import hashlib
import logging
import os
import re
import uuid
import csv
import io
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.db import DatabaseError, models, transaction
from django.db.models import Q
from django.db.models import Count
from django.utils import timezone

from transactions.management.commands.import_material_data import (
    Command as MaterialDataImportCommand,
)
from transactions.management.commands.import_open_items import (
    Command as OpenItemsImportCommand,
)
from transactions.models import (
    ImportBatch,
    ImportUploadSession,
    MaterialData,
    OpenItem,
)
from transactions.services.csv_imports import (
    keep_last_business_key_rows,
    read_csv_rows,
    reject_duplicate_business_keys,
)


logger = logging.getLogger(__name__)

_IMPORT_DEFINITIONS = {
    "material": {
        "command": MaterialDataImportCommand,
        "model": MaterialData,
        "type": ImportBatch.ImportType.MATERIAL_DATA,
        "key_fields": ("purchasing_document", "item"),
    },
    "open_items": {
        "command": OpenItemsImportCommand,
        "model": OpenItem,
        "type": ImportBatch.ImportType.OPEN_ITEMS,
        "key_fields": (
            "entity_code",
            "accounting_document_number",
            "line_number",
        ),
    },
}
_STAGED_NAME = re.compile(r"^[a-f0-9]{32}\.csv$")


def _staging_root() -> Path:
    root = Path(settings.WHT_IMPORT_STAGING_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_upload(upload: BinaryIO) -> tuple[bytes, str]:
    original_name = os.path.basename(upload.name.replace("\\", "/"))
    if not original_name or not original_name.lower().endswith(".csv"):
        raise ValidationError("Upload a CSV file.")
    if upload.size > settings.WHT_IMPORT_MAX_FILE_SIZE:
        raise ValidationError(
            f"{original_name} exceeds the configured file-size limit."
        )
    content = upload.read()
    if len(content) > settings.WHT_IMPORT_MAX_FILE_SIZE:
        raise ValidationError(
            f"{original_name} exceeds the configured file-size limit."
        )
    if not content:
        raise ValidationError(f"{original_name} is empty.")
    return content, original_name[:255]


def _parse_csv(
    content: bytes,
    original_name: str,
    kind: str,
) -> tuple[list[tuple[int, dict[str, object]]], list[tuple[int, str]], dict[int, dict[str, str]]]:
    command_class = _IMPORT_DEFINITIONS[kind]["command"]
    command = command_class()
    try:
        rows, errors = read_csv_rows(
            content,
            original_name,
            command.required_fields,
        )
    except ValueError as error:
        raise ValidationError(str(error)) from error

    parsed = []
    raw_rows = {}
    reader = csv.DictReader(
        io.StringIO(content.decode("utf-8-sig"), newline=""),
        strict=True,
    )
    for row_number, row in enumerate(reader, start=2):
        raw_rows[row_number] = {
            key: (
                str(value)
                if value is not None
                else "(missing)"
            )
            for key, value in row.items()
            if key is not None
        }
        if None in row:
            raw_rows[row_number]["extra_columns"] = ", ".join(row[None])
    for row_number, row in rows:
        try:
            values = command._parse_row(row, row_number)
        except ValueError as error:
            errors.append((row_number, str(error)))
            continue
        parsed.append((row_number, values))
    return parsed, errors, raw_rows


def _existing_by_key(
    model: type[models.Model],
    parsed: list[tuple[int, dict[str, object]]],
    key_fields: tuple[str, ...],
) -> dict[tuple[object, ...], models.Model]:
    keys = {
        tuple(values[field] for field in key_fields)
        for _, values in parsed
    }
    existing: dict[tuple[object, ...], models.Model] = {}
    key_list = list(keys)
    if not key_list:
        return existing
    for offset in range(0, len(key_list), 250):
        query = models.Q()
        for key in key_list[offset:offset + 250]:
            query |= models.Q(**dict(zip(key_fields, key)))
        for record in model.objects.filter(query):
            existing[tuple(getattr(record, field) for field in key_fields)] = record
    return existing


def _analyze_file(
    kind: str,
    content: bytes,
    original_name: str,
) -> tuple[dict, list[tuple[int, dict[str, object]]]]:
    definition = _IMPORT_DEFINITIONS[kind]
    parsed, errors, raw_rows = _parse_csv(content, original_name, kind)
    rows_read = len(parsed) + len(errors)
    key_fields = definition["key_fields"]
    key_label = (
        "Purchasing Document + Item"
        if kind == "material"
        else "Entity + Document Number + Line Number"
    )
    warnings: list[tuple[int, str]] = []
    if kind == "material":
        accepted, duplicate_warnings = keep_last_business_key_rows(
            parsed,
            key_fields,
            key_label,
        )
        warnings.extend(duplicate_warnings)
    else:
        accepted, duplicate_errors = reject_duplicate_business_keys(
            parsed,
            key_fields,
            key_label,
        )
        errors.extend(duplicate_errors)
    existing = _existing_by_key(definition["model"], accepted, key_fields)
    digest = hashlib.sha256(content).hexdigest()
    counts = Counter()
    outcomes: dict[int, dict[str, object]] = {}
    parsed_by_number = dict(parsed)
    error_reasons: dict[int, list[str]] = {}
    warning_reasons: dict[int, list[str]] = {}
    for row_number, message in errors:
        error_reasons.setdefault(row_number, []).append(message)
    for row_number, message in warnings:
        warning_reasons.setdefault(row_number, []).append(message)
    for row_number in set(raw_rows) | set(parsed_by_number) | set(error_reasons) | set(warning_reasons):
        values = parsed_by_number.get(row_number)
        outcomes[row_number] = {
            "row_number": row_number,
            "values": {
                key: str(value) if value is not None else ""
                for key, value in (values or raw_rows.get(row_number, {})).items()
            },
            "outcome": "reject" if row_number in error_reasons else "skip" if row_number in warning_reasons else "",
            "reason": "; ".join(
                error_reasons.get(row_number, warning_reasons.get(row_number, []))
            ),
            "material_match": "",
        }
    completed_documents = set()
    if kind == "open_items":
        completed_documents = set(
            OpenItem.objects.filter(
                status__in=[OpenItem.Status.COMPLETED, OpenItem.Status.REVIEWED]
            )
            .values_list("entity_code", "accounting_document_number")
            .distinct()
        )
    for row_number, values in accepted:
        key = tuple(values[field] for field in key_fields)
        record = existing.get(key)
        if (
            kind == "open_items"
            and (values["entity_code"], values["accounting_document_number"])
            in completed_documents
        ):
            counts["skipped"] += 1
            warnings.append(
                (
                    row_number,
                    f"Completed SAP document {values['entity_code']} + "
                    f"{values['accounting_document_number']} will be skipped.",
                )
            )
            outcome = "skip"
            reason = f"Completed SAP document {values['entity_code']} + {values['accounting_document_number']} will be skipped."
        elif record is None:
            counts["created"] += 1
            outcome = "create"
            reason = ""
        elif all(getattr(record, field) == value for field, value in values.items()):
            counts["skipped"] += 1
            outcome = "skip"
            reason = "Source values match the existing record."
        elif kind == "open_items":
            errors.append(
                (
                    row_number,
                    "Conflicting source content for SAP line "
                    f"{key[0]} + {key[1]} + {key[2]}; the entire file will be rejected.",
                )
            )
            outcome = "reject"
            reason = errors[-1][1]
        else:
            counts["updated"] += 1
            outcome = "update"
            reason = "Source values differ from the existing record."
        outcomes[row_number] = {
            "row_number": row_number,
            "values": {
                key: str(value) if value is not None else ""
                for key, value in values.items()
            },
            "outcome": outcome,
            "reason": reason,
            "material_match": "",
        }

    counts["skipped"] += len(warnings) if kind == "material" else 0

    return {
        "kind": kind,
        "filename": original_name,
        "sha256": digest,
        "rows_read": rows_read,
        "rows_created": counts["created"],
        "rows_updated": counts["updated"],
        "rows_skipped": counts["skipped"],
        "rows_rejected": len(errors),
        "errors": [
            {"row_number": row_number, "message": message}
            for row_number, message in sorted(errors)
        ],
        "warnings": [
            {"row_number": row_number, "message": message}
            for row_number, message in sorted(warnings)
        ],
        "rows": [
            outcomes[row_number]
            for row_number in sorted(outcomes)
        ],
    }, accepted


def _material_match_preview(
    open_items: list[tuple[int, dict[str, object]]],
    material_rows: list[tuple[int, dict[str, object]]],
) -> dict[str, object]:
    material_counts: Counter = Counter()
    material_keys = {
        (row["purchasing_document"], row["item"])
        for _, row in material_rows
        if row["purchasing_document"] and row["item"]
    }
    for offset in range(0, len(open_items), 250):
        chunk = open_items[offset:offset + 250]
        query = models.Q()
        for _, row in chunk:
            if row["purchasing_document"] and row["purchasing_document_item"]:
                query |= models.Q(
                    purchasing_document=row["purchasing_document"],
                    item=row["purchasing_document_item"],
                )
        if query:
            existing = (
                MaterialData.objects.filter(query)
                .values("purchasing_document", "item")
                .annotate(candidate_count=Count("pk"))
            )
            material_counts.update(
                {
                    (row["purchasing_document"], row["item"]):
                    row["candidate_count"]
                    for row in existing
                }
            )
    for key in material_keys:
        material_counts.setdefault(key, 1)

    outcomes = Counter()
    row_outcomes = {}
    for row_number, row in open_items:
        key = (row["purchasing_document"], row["purchasing_document_item"])
        candidate_count = material_counts[key]
        if candidate_count == 1:
            outcomes["matched"] += 1
            row_outcomes[str(row_number)] = "Exactly one material match"
        elif candidate_count > 1:
            outcomes["ambiguous"] += 1
            row_outcomes[str(row_number)] = (
                f"Ambiguous: {candidate_count} possible material matches; none selected"
            )
        else:
            outcomes["not_found"] += 1
            row_outcomes[str(row_number)] = "No material match"
    return {
        "matched": outcomes["matched"],
        "not_found": outcomes["not_found"],
        "ambiguous": outcomes["ambiguous"],
        "rows": row_outcomes,
    }


def preview_and_stage(
    user,
    material_upload: BinaryIO | None,
    open_items_upload: BinaryIO | None,
) -> ImportUploadSession:
    contents: dict[str, tuple[bytes, str]] = {}
    analyses: dict[str, dict] = {}
    accepted: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for kind, upload in (("material", material_upload), ("open_items", open_items_upload)):
        if upload is None:
            continue
        content, name = _read_upload(upload)
        contents[kind] = (content, name)
        analyses[kind], accepted[kind] = _analyze_file(kind, content, name)

    valid_rows = sum(len(rows) for rows in accepted.values())
    has_errors = any(
        analysis["rows_rejected"] for analysis in analyses.values()
    )

    preview = {
        "files": list(analyses.values()),
        "can_import": valid_rows > 0 and not has_errors,
        "material_matches": (
            _material_match_preview(
                accepted.get("open_items", []),
                accepted.get("material", []),
            )
            if "open_items" in analyses
            else None
        ),
        "notice": (
            "Open items were uploaded without MaterialData; existing purchasing "
            "data will be used and unmatched lines will remain unmatched."
            if "open_items" in analyses and "material" not in analyses
            else ""
        ),
    }
    if preview["material_matches"] is not None:
        match_rows = preview["material_matches"]["rows"]
        for analysis in preview["files"]:
            if analysis["kind"] == "open_items":
                for row in analysis["rows"]:
                    row["material_match"] = match_rows.get(
                        str(row["row_number"]),
                        "Not evaluated (row rejected or duplicate)",
                    )

    token = uuid.uuid4()
    staged_names: dict[str, str] = {}
    if valid_rows == 0 or has_errors:
        return ImportUploadSession.objects.create(
            token=token,
            uploaded_by=user,
            status=ImportUploadSession.Status.INVALID,
            material_original_name=contents.get("material", (b"", ""))[1],
            material_sha256=analyses.get("material", {}).get("sha256", ""),
            open_items_original_name=contents.get("open_items", (b"", ""))[1],
            open_items_sha256=analyses.get("open_items", {}).get("sha256", ""),
            preview=preview,
        )
    try:
        root = _staging_root()
        for kind, (content, _) in contents.items():
            staged_name = f"{uuid.uuid4().hex}.csv"
            staged_names[kind] = staged_name
            with (root / staged_name).open("xb") as staged_file:
                staged_file.write(content)
        session = ImportUploadSession.objects.create(
            token=token,
            uploaded_by=user,
            material_staged_name=staged_names.get("material", ""),
            material_original_name=contents.get("material", (b"", ""))[1],
            material_sha256=analyses.get("material", {}).get("sha256", ""),
            open_items_staged_name=staged_names.get("open_items", ""),
            open_items_original_name=contents.get("open_items", (b"", ""))[1],
            open_items_sha256=analyses.get("open_items", {}).get("sha256", ""),
            preview=preview,
        )
    except (DatabaseError, OSError):
        _remove_staged_names(staged_names.values())
        raise
    return session


def _staged_path(name: str) -> Path:
    if not _STAGED_NAME.fullmatch(name):
        raise ValidationError("The staged upload reference is invalid.")
    root = _staging_root()
    path = root / name
    if path.resolve().parent != root:
        raise ValidationError("The staged upload path is invalid.")
    return path


def _remove_staged_names(names) -> list[str]:
    errors = []
    for name in names:
        if not name:
            continue
        try:
            path = _staged_path(name)
            if path.exists() or path.is_symlink():
                path.unlink()
        except (OSError, ValidationError) as error:
            logger.exception("Unable to remove a staged WHT import file.")
            errors.append(str(error))
    return errors


def remove_staged_files(names) -> list[str]:
    return _remove_staged_names(names)


def confirm_import(session: ImportUploadSession, user) -> tuple[ImportUploadSession, list[str]]:
    session = ImportUploadSession.objects.get(pk=session.pk, uploaded_by=user)
    if session.status == ImportUploadSession.Status.COMPLETED:
        return session, []
    if (
        session.status == ImportUploadSession.Status.READY
        and session.created_at
        < timezone.now()
        - timedelta(hours=settings.WHT_IMPORT_STAGING_TTL_HOURS)
    ):
        cleanup_errors = _remove_staged_names(
            (session.material_staged_name, session.open_items_staged_name)
        )
        session.status = ImportUploadSession.Status.EXPIRED
        update_fields = ["status"]
        if not cleanup_errors:
            session.material_staged_name = ""
            session.open_items_staged_name = ""
            update_fields.extend(
                ["material_staged_name", "open_items_staged_name"]
            )
        session.save(update_fields=update_fields)
        if cleanup_errors:
            raise ValidationError(
                "This preview has expired and staged-file cleanup failed. "
                "The failure was logged; contact an administrator."
            )
        raise ValidationError(
            "This preview has expired. Upload the files again to start a new preview."
        )
    if session.status == ImportUploadSession.Status.IMPORTING:
        raise ValidationError("This upload is already being imported.")
    if session.status != ImportUploadSession.Status.READY:
        raise ValidationError("This upload is no longer available to import.")

    claimed = ImportUploadSession.objects.filter(
        pk=session.pk,
        uploaded_by=user,
        status=ImportUploadSession.Status.READY,
    ).update(status=ImportUploadSession.Status.IMPORTING)
    if claimed != 1:
        session.refresh_from_db()
        if session.status == ImportUploadSession.Status.IMPORTING:
            raise ValidationError("This upload is already being imported.")
        if session.status == ImportUploadSession.Status.COMPLETED:
            return session, []
        raise ValidationError("This upload is no longer available to import.")

    completed = False
    try:
        with transaction.atomic():
            session = ImportUploadSession.objects.get(
                pk=session.pk,
                uploaded_by=user,
                status=ImportUploadSession.Status.IMPORTING,
            )
            staged_files = (
                ("material", session.material_staged_name, session.material_sha256),
                ("open_items", session.open_items_staged_name, session.open_items_sha256),
            )
            paths = {}
            for kind, name, expected_digest in staged_files:
                if not name:
                    continue
                path = _staged_path(name)
                try:
                    content = path.read_bytes()
                except OSError as error:
                    raise ValidationError(
                        "A staged file is no longer available. Please upload it again."
                    ) from error
                if hashlib.sha256(content).hexdigest() != expected_digest:
                    raise ValidationError(
                        "A staged file changed after preview. Please upload it again."
                    )
                paths[kind] = path

            for kind in ("material", "open_items"):
                if kind not in paths:
                    continue
                command_name = (
                    "import_material_data"
                    if kind == "material"
                    else "import_open_items"
                )
                call_command(command_name, str(paths[kind]), verbosity=0)
                definition = _IMPORT_DEFINITIONS[kind]
                batch = ImportBatch.objects.get(
                    import_type=definition["type"],
                    source_sha256=hashlib.sha256(paths[kind].read_bytes()).hexdigest(),
                )
                if batch.rows_rejected:
                    row_errors = "; ".join(
                        batch.validation_errors.values_list("message", flat=True)
                    )
                    raise ValidationError(
                        "The import was rejected because the file changed or no "
                        f"longer passes validation. {row_errors}"
                    )
                batch.uploaded_by = user
                batch.upload_session = session
                batch.source_filename = (
                    session.material_original_name
                    if kind == "material"
                    else session.open_items_original_name
                )
                batch.save(
                    update_fields=[
                        "uploaded_by",
                        "upload_session",
                        "source_filename",
                    ]
                )
            session.status = ImportUploadSession.Status.COMPLETED
            session.completed_at = timezone.now()
            session.save(update_fields=["status", "completed_at"])
        completed = True
    finally:
        if not completed:
            ImportUploadSession.objects.filter(
                pk=session.pk,
                status=ImportUploadSession.Status.IMPORTING,
            ).update(status=ImportUploadSession.Status.READY)

    cleanup_errors = _remove_staged_names(
        (session.material_staged_name, session.open_items_staged_name)
    )
    if not cleanup_errors:
        session.material_staged_name = ""
        session.open_items_staged_name = ""
        session.save(update_fields=["material_staged_name", "open_items_staged_name"])
    return session, cleanup_errors


def cleanup_expired_uploads() -> list[str]:
    expires_at = timezone.now() - timedelta(
        hours=settings.WHT_IMPORT_STAGING_TTL_HOURS
    )
    sessions = list(
        ImportUploadSession.objects.filter(
            Q(
                status__in=[
                    ImportUploadSession.Status.READY,
                    ImportUploadSession.Status.IMPORTING,
                ],
                created_at__lt=expires_at,
            )
            | Q(
                status=ImportUploadSession.Status.COMPLETED,
                material_staged_name__gt="",
            )
            | Q(
                status=ImportUploadSession.Status.COMPLETED,
                open_items_staged_name__gt="",
            )
        )
        .order_by("created_at")[:100]
    )
    errors = []
    for session in sessions:
        cleanup_errors = _remove_staged_names(
            (session.material_staged_name, session.open_items_staged_name)
        )
        errors.extend(cleanup_errors)
        if not cleanup_errors:
            if session.status in {
                ImportUploadSession.Status.READY,
                ImportUploadSession.Status.IMPORTING,
            }:
                session.status = ImportUploadSession.Status.EXPIRED
            session.material_staged_name = ""
            session.open_items_staged_name = ""
            session.save(
                update_fields=[
                    "status",
                    "material_staged_name",
                    "open_items_staged_name",
                ]
            )
    return errors


def get_batch_match_counts(batch: ImportBatch) -> dict[str, int]:
    counts = Counter()
    for item in OpenItem.objects.filter(source_import_batch=batch).iterator():
        match = item.material_matches.order_by("-created_at", "-id").first()
        if match is None:
            counts["not_found"] += 1
        else:
            counts[match.status.lower()] += 1
    return {
        "matched": counts["matched"],
        "not_found": counts["not_found"],
        "ambiguous": counts["ambiguous"],
    }
