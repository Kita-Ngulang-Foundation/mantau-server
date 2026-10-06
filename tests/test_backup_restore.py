import importlib.util
from contextlib import closing
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


def test_live_wal_backup_is_portable_and_preserves_uncheckpointed_commits(tmp_path):
    database, recordings = source(tmp_path)
    archive, target = tmp_path / 'backup', tmp_path / 'restored'
    wal = Path(str(database) + '-wal')
    with closing(sqlite3.connect(database)) as live:
        assert live.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        live.execute('PRAGMA wal_autocheckpoint=0')
        live.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        live.execute("INSERT INTO household_data VALUES('committed-in-wal')")
        live.commit()
        assert wal.is_file() and wal.stat().st_size > 0
        # An immutable reader ignores the live WAL: the new row is committed
        # but is demonstrably absent from the main database file.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as main:
            assert main.execute('SELECT value FROM household_data').fetchall() == [('retained',)]
        source_database_hash, source_wal_hash = operations.digest(database), operations.digest(wal)

        manifest = operations.backup(database, recordings, archive, source_id='live-wal-test')
        assert set(manifest['files']) == {'mantau_ld.db', 'recordings/family/event.mp4'}
        assert not (archive / 'mantau_ld.db-wal').exists()
        assert not (archive / 'mantau_ld.db-shm').exists()
        operations.restore(archive, target)
        with closing(sqlite3.connect(target / 'mantau_ld.db')) as restored:
            assert restored.execute('SELECT value FROM household_data ORDER BY rowid').fetchall() == [
                ('retained',), ('committed-in-wal',)]
            assert restored.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert (target / 'recordings/family/event.mp4').read_bytes() == b'isolated-test-clip'
        assert not (target / 'mantau_ld.db-wal').exists()
        assert not (target / 'mantau_ld.db-shm').exists()
        assert live.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert operations.digest(database) == source_database_hash
        assert operations.digest(wal) == source_wal_hash
        assert live.execute('SELECT value FROM household_data ORDER BY rowid').fetchall() == [
            ('retained',), ('committed-in-wal',)]


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
