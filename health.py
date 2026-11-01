#!/usr/bin/env python3
"""Local health diary. Measurements are supplied by the caller, never inferred."""
import csv
import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = '1'
MAX_METRIC_VALUE = 1_000_000_000
SCHEMA_SQL = '''
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
      source_id TEXT UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      deleted INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT,
      action TEXT, before_json TEXT, after_json TEXT, at TEXT);
    '''

KINDS = {'profile', 'meal', 'sleep', 'activity', 'workout', 'body', 'daily'}
METRICS = {
    'meal': {'kcal', 'protein_g', 'carbs_g', 'fat_g'},
    'sleep': {'hours'}, 'activity': {'steps', 'active_minutes'},
    'workout': {'minutes', 'exercise_kcal'},
    'body': {'weight_kg', 'body_fat_pct', 'waist_cm'},
    'daily': {'expenditure_kcal'}, 'profile': set(),
}

def now():
    return datetime.now(timezone.utc).isoformat()

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

def database_path(path=None):
    p = Path(path or os.environ.get('HEALTH_AGENT_DB', str(Path.home() / '.local/share/health-agent/diary.sqlite3'))).expanduser().resolve()
    checkout = Path(__file__).resolve().parent
    if p == checkout or checkout in p.parents:
        raise ValueError('Health data must be stored outside the code checkout')
    return p

def connect(path=None):
    p = database_path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(p, timeout=30)
    os.chmod(p, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if row and row[0] != SCHEMA_VERSION:
        db.close()
        raise ValueError('Unsupported schema version')
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    db.commit()
    return db

def validate_day(day):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('day must be YYYY-MM-DD (user local calendar day)')

def validate(kind, day, payload):
    if kind not in KINDS:
        raise ValueError('Unknown kind')
    validate_day(day)
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    dumps(payload)
    allowed = {'metrics', 'provenance', 'note', 'source_refs', 'label'}
    if kind == 'profile':
        allowed |= {'timezone', 'height_cm', 'birth_year', 'goals', 'preferences'}
    if kind == 'daily':
        allowed |= {'intake_complete', 'expenditure_basis'}
    if set(payload) - allowed:
        raise ValueError('Unknown payload fields: ' + ', '.join(sorted(set(payload) - allowed)))
    provenance = payload.get('provenance')
    if not isinstance(provenance, dict) or not isinstance(provenance.get('method'), str) or not provenance['method'].strip():
        raise ValueError('provenance.method required (e.g. user_report, label, agent_estimate, device)')
    if provenance.get('method') == 'agent_estimate' and not provenance.get('uncertainty'):
        raise ValueError('agent_estimate requires provenance.uncertainty')
    metrics = payload.get('metrics', {})
    if not isinstance(metrics, dict) or set(metrics) - METRICS[kind]:
        raise ValueError('Invalid metrics for kind')
    for name, bounds in metrics.items():
        if bounds is None:  # explicit unknown, not zero
            continue
        if not isinstance(bounds, dict) or set(bounds) != {'low', 'high'}:
            raise ValueError(name + ' must be null or {low, high}')
        low, high = bounds['low'], bounds['high']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= MAX_METRIC_VALUE or not math.isfinite(v) for v in (low, high)) or not 0 <= low <= high:
            raise ValueError('Ranges must be finite, 0..1e9, and low <= high')
        if name == 'body_fat_pct' and high > 100:
            raise ValueError('body_fat_pct cannot exceed 100')
    if kind == 'daily':
        if 'intake_complete' in payload and type(payload['intake_complete']) is not bool:
            raise ValueError('intake_complete must be boolean')
        if metrics.get('expenditure_kcal') is not None and payload.get('expenditure_basis') != 'total_including_activity_and_exercise':
            raise ValueError('Expenditure must explicitly be total_including_activity_and_exercise')
    if kind == 'profile' and 'timezone' in payload:
        from zoneinfo import ZoneInfo
        ZoneInfo(payload['timezone'])
    if 'source_refs' in payload and (not isinstance(payload['source_refs'], list) or not all(isinstance(x, str) for x in payload['source_refs'])):
        raise ValueError('source_refs must be string references; photos are not ingested')

def decode(row):
    result = dict(row)
    result['payload'] = json.loads(result['payload'])
    result['deleted'] = bool(result['deleted'])
    return result

def audit(db, record_id, action, before, after):
    db.execute('INSERT INTO audit(record_id, action, before_json, after_json, at) VALUES(?,?,?,?,?)',
               (record_id, action, dumps(before) if before else None, dumps(after) if after else None, now()))

def add(db, kind, day, payload, source_id):
    validate(kind, day, payload)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError('source_id required; use stable message ID plus item index')
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute('SELECT * FROM records WHERE source_id=?', (source_id,)).fetchone()
        if existing:
            r = decode(existing)
            if (r['kind'], r['day'], r['payload'], r['deleted']) != (kind, day, payload, False):
                raise ValueError('source_id conflict; use explicit edit rather than replay')
            return {'record': r, 'deduplicated': True}
        if kind == 'daily' and db.execute("SELECT 1 FROM records WHERE kind='daily' AND day=? AND deleted=0", (day,)).fetchone():
            raise ValueError('A daily record already exists; edit it')
        stamp, ident = now(), str(uuid.uuid4())
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,0)', (ident, kind, day, source_id, dumps(payload), stamp, stamp))
        r = get(db, ident)
        audit(db, ident, 'create', None, r)
    return {'record': r, 'deduplicated': False}

def get(db, ident):
    row = db.execute('SELECT * FROM records WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('Record not found')
    return decode(row)

def edit(db, ident, payload, expected_updated_at):
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = get(db, ident)
        if old['deleted'] or old['updated_at'] != expected_updated_at:
            raise ValueError('Record deleted or changed; reread before editing')
        validate(old['kind'], old['day'], payload)
        db.execute('UPDATE records SET payload=?,updated_at=? WHERE id=?', (dumps(payload), now(), ident))
        new = get(db, ident)
        audit(db, ident, 'edit', old, new)
    return new

def delete(db, ident, expected_updated_at):
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = get(db, ident)
        if old['deleted'] or old['updated_at'] != expected_updated_at:
            raise ValueError('Record deleted or changed; reread before deleting')
        db.execute('UPDATE records SET deleted=1,updated_at=? WHERE id=?', (now(), ident))
        audit(db, ident, 'delete', old, get(db, ident))
    return {'deleted': ident}

def records(db, start=None, end=None, kind=None, include_deleted=False):
    clauses, values = [], []
    for value, op in ((start, '>='), (end, '<=')):
        if value is not None:
            validate_day(value)
            clauses.append('day' + op + '?')
            values.append(value)
    if kind:
        if kind not in KINDS:
            raise ValueError('Unknown kind')
        clauses.append('kind=?'); values.append(kind)
    if not include_deleted:
        clauses.append('deleted=0')
    return [decode(r) for r in db.execute('SELECT * FROM records' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY day,created_at,id', values)]

def daily_summary(db, day):
    validate_day(day)
    rows = records(db, day, day)
    meals = [r for r in rows if r['kind'] == 'meal']
    daily = [r for r in rows if r['kind'] == 'daily']
    status = daily[-1]['payload'] if daily else {}
    complete = status.get('intake_complete', False)
    totals = {}
    for metric in sorted(METRICS['meal']):
        known = [r['payload'].get('metrics', {}).get(metric) for r in meals]
        present = [v for v in known if v is not None]
        all_known = len(present) == len(meals)
        totals[metric] = {'range': {'low': sum(v['low'] for v in present), 'high': sum(v['high'] for v in present)} if present or complete and not meals else None,
                          'complete': complete and all_known, 'known_meals': len(present), 'unknown_meals': len(meals)-len(present)}
    expenditure = status.get('metrics', {}).get('expenditure_kcal')
    kcal = totals['kcal']
    balance = None
    deficit = None
    if kcal['complete'] and expenditure is not None:
        balance = {'low': kcal['range']['low'] - expenditure['high'], 'high': kcal['range']['high'] - expenditure['low']}
        deficit = {'low': expenditure['low'] - kcal['range']['high'], 'high': expenditure['high'] - kcal['range']['low']}
    return {'day': day, 'meal_count': len(meals), 'intake_complete': complete, 'intake': totals,
            'total_expenditure_kcal': expenditure, 'energy_balance_kcal': balance, 'calorie_deficit_kcal': deficit,
            'exercise_added_to_expenditure': False,
            'other_records': [r for r in rows if r['kind'] not in {'meal','daily'}]}

def trends(db, start, end):
    validate_day(start); validate_day(end)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first or (last-first).days > 365:
        raise ValueError('Trend window must be 1..366 days')
    days = [daily_summary(db, (first+timedelta(days=i)).isoformat()) for i in range((last-first).days+1)]
    complete = [d['intake']['kcal']['range'] for d in days if d['intake']['kcal']['complete']]
    series = {}
    for r in records(db, start, end):
        if r['kind'] in {'body','sleep','activity','workout'}:
            for name, val in r['payload'].get('metrics', {}).items():
                if val is not None:
                    series.setdefault(r['kind']+'.'+name, []).append({'day':r['day'], 'range':val, 'record_id':r['id']})
    return {'start':start, 'end':end, 'days':days, 'complete_intake_days':len(complete),
            'mean_kcal_complete_days': {k: sum(v[k] for v in complete)/len(complete) for k in ('low','high')} if complete else None,
            'measurement_series':series, 'interpretation':'Descriptive only; missing days are not zero. No diagnosis or prescription.'}

# Portable, checksum-verified backups (private, unencrypted data).
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
COLUMNS = {
    "records": ["id", "kind", "day", "source_id", "payload", "created_at", "updated_at", "deleted"],
    "audit": ["id", "record_id", "action", "before_json", "after_json", "at"],
    "metadata": ["key", "value"],
}
FILES = {"database.sqlite3", "data.json", "records.csv", "audit.csv", "README.txt"}


def _connect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _validate(conn):
    # Accept only the schema emitted by this version, including keys, defaults,
    # indexes and constraints. Matching column names alone is insufficient.
    reference = sqlite3.connect(':memory:')
    try:
        reference.executescript(SCHEMA_SQL)
        query = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        expected = [tuple(row) for row in reference.execute(query)]
    finally:
        reference.close()
    actual = [tuple(row) for row in conn.execute(query)]
    if actual != expected:
        raise ValueError('Unsupported or modified database schema')
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Invalid SQLite integrity")
    objects = conn.execute("SELECT type,name,tbl_name FROM sqlite_master").fetchall()
    for obj in objects:
        if obj[0] in {"view", "trigger"}:
            raise ValueError("Unexpected executable database schema")
        if obj[0] == "table" and obj[1] not in {*COLUMNS, "sqlite_sequence"}:
            raise ValueError("Unexpected database table")
    for table, columns in COLUMNS.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if [row[1] for row in info] != columns:
            raise ValueError(f"Unsupported schema for {table}")
    version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if not version or version[0] != SCHEMA_VERSION:
        raise ValueError("Unsupported database schema version")
    for row in conn.execute("SELECT * FROM records"):
        if not isinstance(row["id"], str) or not row["id"] or row["deleted"] not in (0, 1):
            raise ValueError("Invalid record")
        payload = json.loads(row["payload"])
        validate(row["kind"], row["day"], payload)
        if not isinstance(row["source_id"], str) or not row["source_id"].strip():
            raise ValueError("Missing source ID")
        if not isinstance(payload, dict):
            raise ValueError("Record payload must be an object")
    for field in ('id', 'source_id'):
        if conn.execute(f'SELECT 1 FROM records GROUP BY {field} HAVING COUNT(*)>1').fetchone():
            raise ValueError('Duplicate record identity')
    if conn.execute("SELECT 1 FROM records WHERE kind='daily' AND deleted=0 GROUP BY day HAVING COUNT(*)>1").fetchone():
        raise ValueError('Duplicate daily summary')
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in COLUMNS}


def _export(conn):
    return {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {columns[0]}")]
            for table, columns in COLUMNS.items()}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def _csv(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    # Prefix potentially active spreadsheet formulas. JSON/SQLite preserve exact originals.
    for row in rows:
        writer.writerow({k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                         for k, v in row.items()})
    return output.getvalue().encode("utf-8-sig")


def create_backup(db_path, output_path):
    """Create a consistent snapshot ZIP; refuses to overwrite an existing file."""
    db_path, output_path = database_path(db_path), database_path(output_path)
    if not db_path.is_file():
        raise ValueError("Database does not exist")
    if output_path.exists():
        raise ValueError("Backup destination already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        snapshot = Path(temp) / "snapshot.sqlite3"
        with _connect(db_path) as source, sqlite3.connect(snapshot) as dest:
            source.backup(dest)
        with _connect(snapshot) as conn:
            counts = _validate(conn)
            data = _export(conn)
        payloads = {
            "database.sqlite3": snapshot.read_bytes(),
            "data.json": _json({"schema_version": SCHEMA_VERSION, **data}),
            "records.csv": _csv(data["records"], COLUMNS["records"]),
            "audit.csv": _csv(data["audit"], COLUMNS["audit"]),
            "README.txt": ("健康日记备份 / Health diary backup\n"
                           "Contains private, unencrypted health data. Keep securely; never commit to Git.\n"
                           "Restore with health.py action=restore, explicitly choosing merge or replace.\n"
                           "SQLite and JSON preserve original values. CSV neutralizes spreadsheet formulas.\n"
                           "SHA-256 detects corruption, not authenticity. Use only trusted backups.\n").encode("utf-8"),
        }
        if any(len(v) > MAX_MEMBER_BYTES for v in payloads.values()) or sum(map(len, payloads.values())) > MAX_ARCHIVE_BYTES:
            raise ValueError("Backup exceeds supported size")
        manifest = {"format": "health-agent-backup", "format_version": 1,
                    "schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                    "files": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                              for name, raw in payloads.items()}}
        # Exclusive open prevents accidental overwrite; unlink a partially written archive on failure.
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, raw in payloads.items():
                    archive.writestr(name, raw)
                archive.writestr("manifest.json", _json(manifest))
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise
    return {"path": str(output_path), "counts": counts, "schema_version": SCHEMA_VERSION,
            "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(), "encrypted": False}


def _read_archive(path):
    if Path(path).stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive exceeds supported size")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) != len(FILES) + 1 or {i.filename for i in entries} != FILES | {"manifest.json"}:
            raise ValueError("Unexpected, duplicate, or unsafe archive members")
        if any(i.file_size > MAX_MEMBER_BYTES or i.flag_bits & 1 for i in entries) or sum(i.file_size for i in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("Oversize or encrypted archive")
        payloads = {i.filename: archive.read(i) for i in entries}
    manifest = json.loads(payloads.pop("manifest.json"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ValueError("Invalid backup manifest object")
    if (manifest.get("format") != "health-agent-backup" or manifest.get("format_version") != 1
            or manifest.get("schema_version") != SCHEMA_VERSION or set(manifest.get("files", {})) != FILES):
        raise ValueError("Unsupported backup format")
    for name, raw in payloads.items():
        expected = manifest["files"][name]
        if not isinstance(expected, dict):
            raise ValueError("Invalid backup file manifest")
        if expected.get("bytes") != len(raw) or expected.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError(f"Checksum mismatch: {name}")
    return manifest, payloads


def _validated_snapshot(path, directory):
    manifest, payloads = _read_archive(path)
    snapshot = Path(directory) / "verified.sqlite3"
    snapshot.write_bytes(payloads["database.sqlite3"])
    with _connect(snapshot) as conn:
        counts = _validate(conn)
        exported = {"schema_version": SCHEMA_VERSION, **_export(conn)}
        if json.loads(payloads["data.json"]) != exported:
            raise ValueError("JSON export does not match SQLite snapshot")
    return snapshot, manifest, counts


def inspect_backup(zip_path):
    """Read/validate archive without extracting archive-controlled paths."""
    with tempfile.TemporaryDirectory() as temp:
        _, manifest, counts = _validated_snapshot(zip_path, temp)
    return {"valid": True, "manifest": manifest, "counts": counts}


def restore_backup(zip_path, db_path, mode):
    """Restore verified data. Merge keeps local conflicts; replace uses SQLite transaction."""
    if mode not in {"merge", "replace"}:
        raise ValueError("Explicit restore mode must be merge or replace")
    db_path = database_path(db_path)
    if db_path == Path(zip_path).resolve():
        raise ValueError("Database and archive paths must differ")
    with tempfile.TemporaryDirectory() as temp:
        snapshot, _, counts = _validated_snapshot(zip_path, temp)
        prior = None
        if db_path.exists():
            with _connect(db_path) as conn:
                _validate(conn)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            prior = create_backup(db_path, db_path.parent / f"pre-restore-{stamp}-{uuid.uuid4().hex[:8]}.zip")["path"]
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conflicts, imported, skipped = [], 0, 0
        with _connect(snapshot) as source, sqlite3.connect(db_path) as target:
            target.execute("PRAGMA trusted_schema=OFF")
            if mode == "replace" or prior is None:
                source.backup(target)
                imported = counts["records"]
            else:
                target.row_factory = sqlite3.Row
                with target:
                    target.execute("BEGIN IMMEDIATE")
                    imported_ids = set()
                    for row in source.execute("SELECT * FROM records"):
                        existing = target.execute("SELECT * FROM records WHERE id=? OR (source_id IS NOT NULL AND source_id=?)",
                                                  (row["id"], row["source_id"])).fetchall()
                        if row["kind"] == "daily" and not row["deleted"] and not existing:
                            existing = target.execute("SELECT * FROM records WHERE kind='daily' AND day=? AND deleted=0", (row["day"],)).fetchall()
                        if existing:
                            if len(existing) == 1 and dict(existing[0]) == dict(row):
                                skipped += 1
                            else:
                                conflicts.append({"id": row["id"], "source_id": row["source_id"], "reason": "existing local record retained"})
                            continue
                        target.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)", tuple(row))
                        imported_ids.add(row["id"])
                        imported += 1
                    for row in source.execute("SELECT * FROM audit ORDER BY id"):
                        if row["record_id"] in imported_ids:
                            target.execute("INSERT INTO audit(record_id,action,before_json,after_json,at) VALUES (?,?,?,?,?)", tuple(row)[1:])
        os.chmod(db_path, 0o600)
    return {"mode": mode, "path": str(db_path), "prior_backup": prior, "imported": imported,
            "skipped_identical": skipped, "conflicts": conflicts}


def dispatch(request, path=None):
    """One JSON request, one JSON result; no model/network dependencies."""
    if not isinstance(request, dict):
        raise ValueError('Request must be a JSON object')
    args = dict(request)
    action = args.pop('action', None)
    path = database_path(path)
    if action == 'inspect':
        return inspect_backup(**args)
    if action == 'restore':
        return restore_backup(db_path=path, **args)
    if action == 'backup':
        return create_backup(db_path=path, **args)
    with connect(path) as db:
        if action == 'init':
            if args:
                raise ValueError('init takes no arguments')
            return {'schema_version': SCHEMA_VERSION, 'path': str(path)}
        operations = {'add':add, 'get':get, 'edit':edit, 'delete':delete,
                      'list':records, 'daily':daily_summary, 'trends':trends}
        if action in operations:
            return operations[action](db, **args)
        if action == 'audit':
            ident = args.pop('ident', None)
            if args:
                raise ValueError('Unknown audit arguments')
            rows = db.execute('SELECT * FROM audit' + (' WHERE record_id=?' if ident else '') + ' ORDER BY id', (ident,) if ident else ())
            return [dict(row) for row in rows]
        raise ValueError('Unknown action')


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Health diary: JSON request on stdin, JSON response on stdout. See SKILL.md.')
    parser.add_argument('--db', help='SQLite path outside this checkout; alternatively HEALTH_AGENT_DB')
    options = parser.parse_args()
    os.umask(0o077)
    try:
        request = json.load(sys.stdin)
        result = dispatch(request, options.db)
        print(dumps({'ok':True, 'result':result}))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, zipfile.BadZipFile, OverflowError, RecursionError) as error:
        print(dumps({'ok':False, 'error':str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
