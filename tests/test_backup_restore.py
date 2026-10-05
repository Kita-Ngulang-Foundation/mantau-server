import importlib.util
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location('backup_restore', Path(__file__).parents[1] / 'ops/backup_restore.py')
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)


def source(tmp_path):
    database = tmp_path / 'live.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE household_data(value TEXT)')
        connection.execute("INSERT INTO household_data VALUES('retained')")
    recordings = tmp_path / 'recordings'
    (recordings / 'family').mkdir(parents=True)
    (recordings / 'family/event.mp4').write_bytes(b'isolated-test-clip')
    (recordings / 'family/interrupted.part').write_bytes(b'partial')
    return database, recordings


def test_isolated_restore_preserves_database_and_clip(tmp_path):
    database, recordings = source(tmp_path)
    archive, target = tmp_path / 'backup', tmp_path / 'restored'
    manifest = operations.backup(database, recordings, archive, source_id='candidate-test')
    operations.restore(archive, target)
    assert manifest['source_id'] == 'candidate-test'
    with sqlite3.connect(target / 'mantau_ld.db') as connection:
        assert connection.execute('SELECT value FROM household_data').fetchone()[0] == 'retained'
    assert (target / 'recordings/family/event.mp4').read_bytes() == b'isolated-test-clip'
    assert not (target / 'recordings/family/interrupted.part').exists()
    assert database.exists() and recordings.exists()


def test_tampered_restore_fails_before_creating_destination(tmp_path):
    database, recordings = source(tmp_path)
    archive, target = tmp_path / 'backup', tmp_path / 'restored'
    operations.backup(database, recordings, archive, source_id='candidate-test')
    (archive / 'recordings/family/event.mp4').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='hash mismatch'):
        operations.restore(archive, target)
    assert not target.exists()


def test_restore_refuses_overwrite_and_backup_recursion(tmp_path):
    database, recordings = source(tmp_path)
    with pytest.raises(ValueError):
        operations.backup(database, recordings, recordings / 'backup', source_id='test')
    archive = tmp_path / 'backup'
    operations.backup(database, recordings, archive, source_id='test')
    with pytest.raises(ValueError, match='already exist'):
        operations.restore(archive, recordings)
