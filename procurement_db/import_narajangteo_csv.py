"""Validate and transactionally import the preprocessed Narajangteo CSV."""

from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path
from typing import Any

from .connection import get_connection


CSV_HEADERS = (
    "업체명",
    "사업자등록번호",
    "업체소재시군구",
    "본사지사구분",
    "업체국가",
    "기업구분",
    "대표업종",
    "제조업체여부",
    "대표세부품명번호",
    "대표세부품명",
    "나라장터등록일자",
    "사업자상태코드",
)

TABLE_COLUMNS = (
    "bizno",
    "company_name",
    "sigungu",
    "head_branch_cls",
    "country",
    "company_type",
    "main_biz_type",
    "is_manufacturer",
    "main_item_no",
    "main_item_name",
    "reg_date",
    "biz_status_code",
)

KEY_COLUMNS = ("bizno", "main_item_no", "main_biz_type")


def _optional(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None


def _normalize_item_no(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith(".0") and normalized[:-2].isdigit():
        return normalized[:-2]
    return normalized


def _parse_date(value: str, line_number: int) -> date | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(
            f"CSV line {line_number}: invalid registration date {normalized!r}"
        ) from error


def read_and_validate(csv_path: Path) -> list[tuple[Any, ...]]:
    """Return normalized rows, rejecting malformed or duplicate logical keys."""

    normalized_rows: list[tuple[Any, ...]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        actual_headers = tuple(reader.fieldnames or ())
        if actual_headers != CSV_HEADERS:
            raise ValueError(
                "CSV headers do not match the Narajangteo import schema. "
                f"Expected {CSV_HEADERS!r}, got {actual_headers!r}."
            )

        for line_number, raw_row in enumerate(reader, start=2):
            row = {key: (value or "").strip() for key, value in raw_row.items()}
            bizno = row["사업자등록번호"]
            company_name = row["업체명"]
            main_item_no = _normalize_item_no(row["대표세부품명번호"])
            main_biz_type = row["대표업종"]

            if not bizno or not company_name or not main_item_no or not main_biz_type:
                raise ValueError(
                    f"CSV line {line_number}: bizno, company name, main item number, "
                    "and main business type are required."
                )
            if len(bizno) > 140 or len(company_name) > 140:
                raise ValueError(f"CSV line {line_number}: key text exceeds DB limits.")

            manufacturer = _optional(row["제조업체여부"])
            if manufacturer not in {"Y", "N"}:
                raise ValueError(
                    f"CSV line {line_number}: manufacturer must be Y or N."
                )

            logical_key = (bizno, main_item_no, main_biz_type)
            if logical_key in seen_keys:
                raise ValueError(
                    f"CSV line {line_number}: duplicate logical key {logical_key!r}."
                )
            seen_keys.add(logical_key)

            normalized_rows.append(
                (
                    bizno,
                    company_name,
                    _optional(row["업체소재시군구"]),
                    _optional(row["본사지사구분"]),
                    _optional(row["업체국가"]),
                    _optional(row["기업구분"]),
                    main_biz_type,
                    manufacturer,
                    main_item_no,
                    _optional(row["대표세부품명"]),
                    _parse_date(row["나라장터등록일자"], line_number),
                    _optional(row["사업자상태코드"]),
                )
            )

    return normalized_rows


def import_rows(rows: list[tuple[Any, ...]]) -> dict[str, int]:
    """Copy rows through a temporary table and upsert them atomically."""

    column_sql = ", ".join(TABLE_COLUMNS)
    key_join_sql = " AND ".join(f"target.{name} = source.{name}" for name in KEY_COLUMNS)
    non_key_columns = tuple(name for name in TABLE_COLUMNS if name not in KEY_COLUMNS)
    changed_sql = " OR ".join(
        f"target.{name} IS DISTINCT FROM source.{name}" for name in non_key_columns
    )
    update_sql = ", ".join(f"{name} = EXCLUDED.{name}" for name in non_key_columns)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE narajangteo_company_import
                (LIKE procurement.narajangteo_company_info INCLUDING DEFAULTS)
                ON COMMIT DROP
                """
            )
            with cursor.copy(
                f"COPY narajangteo_company_import ({column_sql}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)

            cursor.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE target.bizno IS NULL) AS inserts,
                    count(*) FILTER (
                        WHERE target.bizno IS NOT NULL AND ({changed_sql})
                    ) AS updates,
                    count(*) FILTER (
                        WHERE target.bizno IS NOT NULL AND NOT ({changed_sql})
                    ) AS unchanged
                FROM narajangteo_company_import AS source
                LEFT JOIN procurement.narajangteo_company_info AS target
                    ON {key_join_sql}
                """
            )
            plan = dict(cursor.fetchone())

            cursor.execute(
                f"""
                INSERT INTO procurement.narajangteo_company_info ({column_sql})
                SELECT {column_sql}
                FROM narajangteo_company_import
                ON CONFLICT ({', '.join(KEY_COLUMNS)}) DO UPDATE
                SET {update_sql}
                """
            )

            cursor.execute(
                "SELECT count(*) AS total FROM procurement.narajangteo_company_info"
            )
            plan["total"] = cursor.fetchone()["total"]
            return {key: int(value) for key, value in plan.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="Path to exported_data.csv")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to PostgreSQL. Without this flag, validation is read-only.",
    )
    args = parser.parse_args()

    rows = read_and_validate(args.csv_path)
    print(f"Validated rows: {len(rows):,}")
    if not args.apply:
        print("Dry run only; PostgreSQL was not modified. Use --apply to import.")
        return

    result = import_rows(rows)
    print(
        "Import complete: "
        f"inserted={result['inserts']:,}, updated={result['updates']:,}, "
        f"unchanged={result['unchanged']:,}, total={result['total']:,}"
    )


if __name__ == "__main__":
    main()
