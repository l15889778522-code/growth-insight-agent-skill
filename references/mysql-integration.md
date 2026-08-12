# MySQL Integration

## Status

Real MySQL integration was **NOT RUN** as part of DB-001/DB-002 implementation on 2026-08-09.
Passing unit tests use controlled connector doubles and must not be reported as a live MySQL result.

The opt-in test entry is:

```text
tests/test_database_v12.py::test_real_mysql_readonly_integration_entry
```

It remains skipped unless both the explicit run gate and an expected live fingerprint are supplied.

## Safety Preconditions

Use a disposable MySQL instance or a user-provided non-production instance. The configured account must have only the minimum read grants required for the selected database. Do not put credentials in the repository, command history, pytest arguments, reports, or captured logs.

For production-like TLS validation, use `VERIFY_IDENTITY` with a trusted CA. `PREFERRED` preserves compatibility with servers that do not require TLS, but it permits an unencrypted connection; the negotiated state is still part of the fingerprint, so a later TLS downgrade cannot reuse an earlier approval.

Supported TLS modes are:

```text
DISABLED
PREFERRED
REQUIRED
VERIFY_CA
VERIFY_IDENTITY
```

## Environment

Set secrets only in the process environment or an approved secret manager:

```powershell
$env:DB_TYPE = "mysql"
$env:DB_SOURCE_ID = "disposable-mysql-v12"
$env:MYSQL_HOST = "127.0.0.1"
$env:MYSQL_PORT = "3306"
$env:MYSQL_USER = "readonly_user"
$env:MYSQL_PASSWORD = "<secret>"
$env:MYSQL_DATABASE = "analytics"
$env:MYSQL_SSL_MODE = "VERIFY_IDENTITY"
$env:MYSQL_SSL_CA = "C:\path\to\ca.pem"
```

Optional client-certificate variables are `MYSQL_SSL_CERT`, `MYSQL_SSL_KEY`, and `MYSQL_SSL_KEY_PASSWORD`. `MYSQL_SSL_CERT` and `MYSQL_SSL_KEY` must be configured together. Client certificates require `REQUIRED`, `VERIFY_CA`, or `VERIFY_IDENTITY`.

## Bind The Expected Identity

First inspect the password-free live descriptor:

```powershell
python scripts/test_db_connection.py --db mysql
```

Review these fields before accepting the fingerprint:

- endpoint host, port, and database
- MySQL `@@server_uuid`
- `VERSION()`
- authenticated `CURRENT_USER()` account identifier
- requested TLS mode, negotiated TLS state, protocol, cipher, certificate verification, and hostname verification

The descriptor and its SHA-256 fingerprint never contain `MYSQL_PASSWORD` or the TLS private-key password.

After independently verifying the descriptor, bind the expected fingerprint:

```powershell
$env:MYSQL_EXPECTED_FINGERPRINT = "<64-character SHA-256 from the reviewed output>"
python scripts/test_db_connection.py --db mysql --expected-fingerprint $env:MYSQL_EXPECTED_FINGERPRINT
```

Changing the server UUID, server version, authenticated account, endpoint, database, requested TLS mode, or negotiated verification state changes the fingerprint and invalidates an old query approval.

## Run The Opt-In Test

Use the repository virtual environment and an in-repository pytest temp directory:

```powershell
$env:RUN_MYSQL_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp tests\.pytest-mysql-live `
  tests/test_database_v12.py::test_real_mysql_readonly_integration_entry
```

The test fails closed unless `MYSQL_EXPECTED_FINGERPRINT` is a 64-character SHA-256 value matching the live connection. It verifies the identity-bound connection, the non-buffered streaming path, exact `DECIMAL` handling, and `utf8mb4` text using read-only constant queries.

## Required Manual Evidence

The opt-in smoke test alone is not a complete release claim. A release integration record should also capture, against a disposable instance:

1. MySQL image or server build and exact version.
2. Read-only grant definition and a denied write attempt made only against disposable data.
3. TLS mode, CA source, negotiated protocol/cipher, and certificate/hostname verification state.
4. A streamed result larger than one fetch batch.
5. Approved connection/read/server-statement timeout failures.
6. Mid-stream disconnect behavior and successful clean retry under a new execution lease.
7. Decimal and `utf8mb4` assertions.
8. The command, timestamp, exit status, and reviewed expected fingerprint.

Do not mark MySQL integration complete until that evidence was produced by a real server run. Do not copy passwords, private keys, or connection strings into the evidence record.
