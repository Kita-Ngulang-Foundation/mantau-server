"""Offline database/recording backup and restore into an empty directory.

Stop the single server process before either operation. The archive contains
private household data and encrypted credentials; store it privately. Recover
the encryption and Firebase keys separately from the existing secret store.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Linked paths are not accepted')


def validate_database(path: Path) -> None:
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Database foreign key check failed')


def destination(path: Path) -> Path:
    path = path.absolute()
    for ancestor in (path, *path.parents):
        regular(ancestor)
    if path.exists():
        raise ValueError('Destination must not already exist')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def backup(database: Path, recordings: Path, target: Path, *, source_id: str) -> dict:
    database, recordings = database.absolute(), recordings.absolute()
    for source in (database, recordings):
        for ancestor in (source, *source.parents):
            regular(ancestor)
    if not database.is_file() or not recordings.is_dir():
        raise ValueError('Database and recordings directory must exist')
    target = destination(target)
    if target == recordings or recordings in target.parents:
        raise ValueError('Backup destination cannot be inside recordings')
    staging = Path(tempfile.mkdtemp(prefix='.mantau-backup-', dir=target.parent))
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(staging / 'mantau_ld.db')) as output:
                source.backup(output)
                # SQLite backup copies the source journal header. Make only
                # the snapshot self-contained before validation opens it.
                if output.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise ValueError('Backup database journal conversion failed')
        validate_database(staging / 'mantau_ld.db')
        (staging / 'recordings').mkdir()
        for path in sorted(recordings.rglob('*')):
            regular(path)
            relative = path.relative_to(recordings)
            copy = staging / 'recordings' / relative
            if path.is_dir():
                copy.mkdir(parents=True, exist_ok=True)
            elif path.is_file() and not path.name.endswith('.part'):
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, copy)
            elif not path.is_file():
                raise ValueError('Recording tree contains a nonregular entry')
        files = {p.relative_to(staging).as_posix(): digest(p)
                 for p in sorted(staging.rglob('*')) if p.is_file()}
        manifest = {'format': 1, 'source_id': source_id, 'files': files,
                    'keys': 'Not included; restore the matching operator-held keys separately'}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore(archive: Path, target: Path) -> dict:
    archive = archive.absolute()
    for ancestor in (archive, *archive.parents):
        regular(ancestor)
    for path in archive.rglob('*'):
        regular(path)
    manifest = json.loads((archive / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('Unsupported backup manifest')
    files = manifest['files']
    if 'mantau_ld.db' not in files:
        raise ValueError('Missing database')
    for name, expected in files.items():
        parts = PurePosixPath(name)
        if parts.is_absolute() or '\\' in name or ':' in name or '..' in parts.parts:
            raise ValueError('Unsafe manifest path')
        if name != 'mantau_ld.db' and not name.startswith('recordings/'):
            raise ValueError('Unexpected backup entry')
        path = archive.joinpath(*parts.parts)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('Backup file hash mismatch')
    actual = {p.relative_to(archive).as_posix() for p in archive.rglob('*')
              if p.is_file() and p.name != 'manifest.json'}
    if actual != set(files):
        raise ValueError('Unlisted files in backup')
    validate_database(archive / 'mantau_ld.db')
    target = destination(target)
    staging = Path(tempfile.mkdtemp(prefix='.mantau-restore-', dir=target.parent))
    try:
        (staging / 'recordings').mkdir()
        for name in files:
            output = staging.joinpath(*PurePosixPath(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive / name, output)
        validate_database(staging / 'mantau_ld.db')
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-stopped', action='store_true', required=True,
                        help='Confirm the server is stopped for filesystem/database consistency')
    commands = parser.add_subparsers(dest='command', required=True)
    save = commands.add_parser('backup')
    save.add_argument('--database', type=Path, required=True)
    save.add_argument('--recordings', type=Path, required=True)
    save.add_argument('--target', type=Path, required=True)
    save.add_argument('--source-id', required=True)
    load = commands.add_parser('restore')
    load.add_argument('--archive', type=Path, required=True)
    load.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'backup':
        result = backup(args.database, args.recordings, args.target, source_id=args.source_id)
    else:
        result = restore(args.archive, args.target)
    print(json.dumps({'verified_files': len(result['files']), 'source_id': result['source_id']}))


if __name__ == '__main__':
    main()
