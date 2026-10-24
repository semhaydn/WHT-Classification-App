from collections import defaultdict
from collections.abc import Sequence
import csv
import io


def reject_duplicate_business_keys(
    parsed_rows: list[tuple[int, dict[str, object]]],
    key_fields: Sequence[str],
    key_label: str,
) -> tuple[list[tuple[int, dict[str, object]]], list[tuple[int, str]]]:
    rows_by_key: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for row_number, values in parsed_rows:
        key = tuple(values[field] for field in key_fields)
        rows_by_key[key].append(row_number)

    duplicate_rows: set[int] = set()
    errors: list[tuple[int, str]] = []
    for key, row_numbers in rows_by_key.items():
        if len(row_numbers) == 1:
            continue
        duplicate_rows.update(row_numbers)
        message = (
            f"Rejected duplicate {key_label} {' + '.join(map(str, key))} "
            f"at CSV rows {', '.join(map(str, row_numbers))}."
        )
        errors.extend((row_number, message) for row_number in row_numbers)

    return (
        [
            (row_number, values)
            for row_number, values in parsed_rows
            if row_number not in duplicate_rows
        ],
        errors,
    )


def keep_last_business_key_rows(
    parsed_rows: list[tuple[int, dict[str, object]]],
    key_fields: Sequence[str],
    key_label: str,
) -> tuple[list[tuple[int, dict[str, object]]], list[tuple[int, str]]]:
    rows_by_key: dict[tuple[object, ...], list[tuple[int, dict[str, object]]]] = (
        defaultdict(list)
    )
    for row in parsed_rows:
        row_number, values = row
        rows_by_key[tuple(values[field] for field in key_fields)].append(row)

    selected_rows = []
    warnings = []
    for key, rows in rows_by_key.items():
        selected_rows.append(rows[-1])
        if len(rows) == 1:
            continue
        selected_row_number = rows[-1][0]
        for row_number, _ in rows[:-1]:
            warnings.append(
                (
                    row_number,
                    f"Ignored duplicate {key_label} {' + '.join(map(str, key))} "
                    f"at CSV row {row_number}; row {selected_row_number} is used.",
                )
            )

    selected_rows.sort(key=lambda row: row[0])
    return selected_rows, warnings


def read_csv_rows(
    content: bytes,
    filename: str,
    required_fields: Sequence[str],
) -> tuple[list[tuple[int, dict[str, str]]], list[tuple[int, str]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{filename} must be a UTF-8 encoded CSV file.") from error

    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if not reader.fieldnames:
            raise ValueError(f"{filename} has no CSV header.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{filename} contains duplicate column names.")
        missing = [field for field in required_fields if field not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"{filename} is missing required columns: {', '.join(missing)}."
            )

        parsed_rows = []
        errors = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                errors.append(
                    (row_number, f"Rejected CSV row {row_number}: too many columns.")
                )
                continue
            if any(value is None for value in row.values()):
                errors.append(
                    (row_number, f"Rejected CSV row {row_number}: too few columns.")
                )
                continue
            parsed_rows.append((row_number, row))
        if not parsed_rows and not errors:
            errors.append((2, f"{filename} contains no data rows."))
        return parsed_rows, errors
    except csv.Error as error:
        raise ValueError(f"{filename} is malformed CSV: {error}.") from error
