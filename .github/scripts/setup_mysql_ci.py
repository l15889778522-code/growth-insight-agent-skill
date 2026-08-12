#!/usr/bin/env python3
"""Prepare an ephemeral MySQL release-gate fixture and export its fingerprint."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pymysql


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from db_common import data_source_descriptor, data_source_fingerprint, load_config


def connect_admin() -> pymysql.Connection:
    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return pymysql.connect(
                host=os.environ["MYSQL_HOST"],
                port=int(os.environ.get("MYSQL_PORT", "3306")),
                user=os.environ["MYSQL_CI_ADMIN_USER"],
                password=os.environ["MYSQL_CI_ADMIN_PASSWORD"],
                charset="utf8mb4",
                autocommit=True,
                ssl_disabled=False,
                ssl={"check_hostname": False},
            )
        except pymysql.MySQLError as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"MySQL did not become ready: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()

    database = os.environ["MYSQL_DATABASE"]
    readonly_user = os.environ["MYSQL_USER"]
    readonly_password = os.environ["MYSQL_PASSWORD"]
    if database != "analytics" or readonly_user != "ci_readonly":
        raise RuntimeError("CI fixture identifiers must match the reviewed static SQL below.")

    connection = connect_admin()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS `analytics` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            cursor.execute("DROP USER IF EXISTS 'ci_readonly'@'%'")
            cursor.execute(
                "CREATE USER 'ci_readonly'@'%' IDENTIFIED BY %s REQUIRE SSL",
                (readonly_password,),
            )
            cursor.execute("GRANT SELECT ON `analytics`.* TO 'ci_readonly'@'%'")
            cursor.execute("DROP TABLE IF EXISTS `analytics`.`ci_values`")
            cursor.execute(
                "CREATE TABLE `analytics`.`ci_values` ("
                "id BIGINT PRIMARY KEY, "
                "exact_decimal DECIMAL(20,2) NOT NULL, "
                "unicode_text VARCHAR(128) NOT NULL, "
                "payload VARCHAR(512) NOT NULL"
                ") CHARACTER SET utf8mb4"
            )
            unicode_text = "\u6570\u636e\u5e93-\U0001f642"
            rows = [
                (
                    index,
                    Decimal("9007199254740993.01") + index,
                    f"{unicode_text}-{index}",
                    "x" * 256,
                )
                for index in range(1, 2506)
            ]
            cursor.executemany(
                "INSERT INTO `analytics`.`ci_values` "
                "(id, exact_decimal, unicode_text, payload) VALUES (%s, %s, %s, %s)",
                rows,
            )
    finally:
        connection.close()

    config = load_config("mysql")
    descriptor = data_source_descriptor(config)
    fingerprint = data_source_fingerprint(config)
    if descriptor["tls"]["active"] is not True:
        raise RuntimeError("The readonly CI connection did not negotiate TLS.")
    with args.github_env.open("a", encoding="utf-8") as handle:
        handle.write(f"MYSQL_EXPECTED_FINGERPRINT={fingerprint}\n")

    print(
        json.dumps(
            {
                "ok": True,
                "row_count": 2505,
                "data_source_fingerprint": fingerprint,
                "server_identity": descriptor["server_identity"],
                "server_version": descriptor["server_version"],
                "account_identifier": descriptor["account_identifier"],
                "tls": descriptor["tls"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
