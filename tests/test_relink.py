"""Relink regression tests use real audio and a disposable SQL database."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import wave

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from rekordbox_mcp import relink as module


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "tracks"
    ID = Column(String, primary_key=True)
    Title = Column(String)
    ArtistName = Column(String)
    FolderPath = Column(String)
    FileNameL = Column(String)
    OrgFolderPath = Column(String)
    Length = Column(Integer)
    FileType = Column(Integer)
    FileSize = Column(Integer)
    SampleRate = Column(Integer)
    BitRate = Column(Integer)
    BitDepth = Column(Integer)
    Rating = Column(Integer)
    CuePosition = Column(Integer)
    rb_local_deleted = Column(Integer, default=0)


def wav(path, seconds=3):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "master.db"}')
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Track(ID="1", Title="Song", ArtistName="Artist",
                      FolderPath="/missing/old.wav", FileNameL="old.wav",
                      OrgFolderPath="/missing/old.wav", Length=3, Rating=5,
                      CuePosition=1234, FileType=11))
    session.commit()
    analysis = tmp_path / "ANLZ.DAT"
    analysis.write_bytes(b"original-analysis-path")
    db = SimpleNamespace(session=session, engine=engine, db_directory=tmp_path,
                         registry=MagicMock(), rollback=session.rollback)
    db.get_content = lambda **kw: session.get(Track, str(kw["ID"])) if kw else session.query(Track).all()
    db.read_anlz_files = lambda cid: {str(analysis): None}
    def update(row, path, **kwargs):
        row.FolderPath = str(path)
        row.FileNameL = Path(path).name
        row.OrgFolderPath = str(path)
        analysis.write_bytes(str(path).encode())
    db.update_content_path = update
    monkeypatch.setattr(module, "get_rekordbox_pid", lambda: 0)
    yield db, analysis
    session.close()
    engine.dispose()


def replacement(path):
    return {"track_id": "1", "expected_old_path": "/missing/old.wav", "new_path": str(path)}


def preview(db, path):
    return module.relink(db, [replacement(path)])


def test_real_audio_inspection(tmp_path):
    path = wav(tmp_path / "different name.wav")
    info = module.inspect_audio(path, hash_file=True)
    assert info["duration_seconds"] == 3
    assert info["sample_rate"] == 8000
    assert info["bit_depth"] == 16
    assert info["lossless_container"]
    assert len(info["sha256"]) == 64


def test_preview_then_apply_preserves_metadata(library, tmp_path):
    db, analysis = library
    path = wav(tmp_path / "better named.wav")
    plan = preview(db, path)
    assert plan["library_modified"] is False
    assert analysis.read_bytes() == b"original-analysis-path"
    assert db.get_content(ID="1").FolderPath == "/missing/old.wav"
    result = module.relink(db, plan["apply_replacements"], dry_run=False)
    row = db.get_content(ID="1")
    assert row.ID == "1" and row.Rating == 5 and row.CuePosition == 1234
    assert row.FolderPath == str(path)
    assert row.FileNameL == path.name
    assert row.FileType == 11 and row.SampleRate == 8000 and row.BitDepth == 16
    manifest = json.loads(Path(result["backup_manifest"]).read_text())
    assert len(manifest["files"]) == 2
    for entry in manifest["files"]:
        assert module.sha256(Path(entry["backup"])) == entry["sha256"]
    assert analysis.read_bytes() == str(path).encode()


def test_write_requires_preview_hash(library, tmp_path):
    db, analysis = library
    with pytest.raises(ValueError, match="expected_sha256"):
        module.relink(db, [replacement(wav(tmp_path / "new.wav"))], dry_run=False)
    assert analysis.read_bytes() == b"original-analysis-path"


def test_changed_file_rejected(library, tmp_path):
    db, analysis = library
    path = wav(tmp_path / "new.wav")
    plan = preview(db, path)
    wav(path, 4)
    with pytest.raises(ValueError, match="changed"):
        module.relink(db, plan["apply_replacements"], dry_run=False)
    assert analysis.read_bytes() == b"original-analysis-path"


def test_stale_path_rejected(library, tmp_path):
    db, _ = library
    item = replacement(wav(tmp_path / "new.wav"))
    item["expected_old_path"] = "/wrong/path.wav"
    with pytest.raises(ValueError, match="Stale"):
        module.relink(db, [item])


def test_running_rekordbox_rejected_before_backup(library, tmp_path, monkeypatch):
    db, analysis = library
    plan = preview(db, wav(tmp_path / "new.wav"))
    monkeypatch.setattr(module, "get_rekordbox_pid", lambda: 12)
    with pytest.raises(RuntimeError, match="Close Rekordbox"):
        module.relink(db, plan["apply_replacements"], dry_run=False)
    assert not (tmp_path / "mcp-relink-backups").exists()
    assert analysis.read_bytes() == b"original-analysis-path"


def test_duration_risk_is_visible_and_blocks_apply(library, tmp_path):
    db, _ = library
    plan = preview(db, wav(tmp_path / "extended mix.wav", 20))
    assert plan["replacements"][0]["requires_timing_risk_acceptance"]
    with pytest.raises(ValueError, match="accept_timing_risk"):
        module.relink(db, plan["apply_replacements"], dry_run=False)


def test_sql_failure_restores_analysis_and_rolls_back(library, tmp_path, monkeypatch):
    db, analysis = library
    plan = preview(db, wav(tmp_path / "new.wav"))
    def fail():
        raise RuntimeError("simulated SQL failure")
    monkeypatch.setattr(db.session, "commit", fail)
    with pytest.raises(RuntimeError, match="SQL rolled back"):
        module.relink(db, plan["apply_replacements"], dry_run=False)
    assert db.get_content(ID="1").FolderPath == "/missing/old.wav"
    assert analysis.read_bytes() == b"original-analysis-path"


def test_backup_failure_never_updates_analysis(library, tmp_path, monkeypatch):
    db, analysis = library
    plan = preview(db, wav(tmp_path / "new.wav"))
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(module, "backup_library", fail)
    with pytest.raises(OSError, match="disk full"):
        module.relink(db, plan["apply_replacements"], dry_run=False)
    assert analysis.read_bytes() == b"original-analysis-path"
    assert db.get_content(ID="1").FolderPath == "/missing/old.wav"


def test_candidate_discovery_renamed_and_ambiguous(library, tmp_path):
    db, _ = library
    source = tmp_path / "music"
    source.mkdir()
    wav(source / "Artist - Song.wav")
    wav(source / "Artist Song.wav")
    result = module.find_candidates(db, [str(source)])
    assert result["files_scanned"] == 2
    assert result["tracks"][0]["ambiguous"]
    assert result["tracks"][0]["candidates"][0]["identity_verified"] is False


def test_duplicate_ids_and_present_upgrade_guard(library, tmp_path):
    db, _ = library
    path = wav(tmp_path / "new.wav")
    with pytest.raises(ValueError, match="Duplicate"):
        module.relink(db, [replacement(path), replacement(path)])
    row = db.get_content(ID="1")
    row.FolderPath = str(wav(tmp_path / "existing.wav"))
    db.session.commit()
    item = replacement(path)
    item["expected_old_path"] = row.FolderPath
    with pytest.raises(ValueError, match="is present"):
        module.relink(db, [item])
    assert module.relink(db, [item], only_missing=False)["status"] == "preview"


def test_two_clients_cannot_relink_concurrently(library, tmp_path):
    db, _ = library
    with module.library_lock(db):
        with pytest.raises(BlockingIOError):
            module.relink(db, [replacement(wav(tmp_path / "new.wav"))])


def test_pyrekordbox_real_schema_and_anlz_path_update(tmp_path, monkeypatch):
    """Exercise the pinned library's real path setter and USN registry/schema."""
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables
    from datetime import datetime, timezone
    engine = create_engine(f'sqlite:///{tmp_path / "master.db"}')
    tables.Base.metadata.create_all(engine)
    engine.dispose()
    db = Rekordbox6Database(path=tmp_path / "master.db", db_dir=tmp_path, unlock=False)
    analysis = tmp_path / "ANLZ.DAT"
    analysis.write_text('/missing/old.wav')
    class Analysis:
        def set_path(self, path):
            self.path = path
        def save(self, path):
            Path(path).write_text(self.path)
    monkeypatch.setattr(db, 'read_anlz_files', lambda cid: {str(analysis): Analysis()})
    monkeypatch.setattr(module, 'get_rekordbox_pid', lambda: 0)
    def record(cls, **values):
        for column in cls.__table__.columns:
            if column.name in values:
                continue
            if isinstance(column.type, tables.DateTime):
                values[column.name] = datetime.now(timezone.utc)
            elif not column.nullable:
                values[column.name] = '' if column.type.python_type is str else 0
        return cls(**values)
    with db.registry.disabled():
        db.session.add_all([
            record(tables.AgentRegistry, registry_id='localUpdateCount', int_1=10,
                                 date_1=datetime.now(timezone.utc), date_2=datetime.now(timezone.utc)),
            record(tables.DjmdContent, ID='1', Title='Song', FolderPath='/missing/old.wav',
                               OrgFolderPath='/missing/old.wav', FileNameL='old.wav',
                               Length=3, Rating=5, rb_local_deleted=0),
            record(tables.DjmdCue, ID='cue', ContentID='1', InMsec=1234),
            record(tables.DjmdSongPlaylist, ID='playlist-song', ContentID='1', PlaylistID='p', TrackNo=7),
        ])
        db.session.commit()
    db.registry.clear_buffer()
    try:
        path = wav(tmp_path / 'Artist - Song better source.wav')
        plan = preview(db, path)
        result = module.relink(db, plan['apply_replacements'], dry_run=False)
        db.session.expire_all()
        row = db.get_content(ID='1')
        assert row.FolderPath == str(path) and row.OrgFolderPath == str(path)
        assert row.FileNameL == path.name and row.Rating == 5
        assert db.session.get(tables.DjmdCue, 'cue').InMsec == 1234
        assert db.session.get(tables.DjmdSongPlaylist, 'playlist-song').TrackNo == 7
        assert db.registry.get_local_update_count() > 10
        assert analysis.read_text() == str(path)
        assert result['status'] == 'applied'
    finally:
        db.close()
        db.engine.dispose()


def test_format_upgrade_requires_explicit_risk_acceptance(library, tmp_path):
    import subprocess
    db, analysis = library
    original = wav(tmp_path / 'source.wav')
    target = tmp_path / 'renamed high quality.flac'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(original),
                    '-metadata', 'artist=Artist', '-metadata', 'title=Song', str(target)], check=True)
    plan = preview(db, target)
    assert plan['replacements'][0]['format_changed'] is True
    assert plan['replacements'][0]['title_similarity'] == 1.0
    with pytest.raises(ValueError, match='accept_timing_risk'):
        module.relink(db, plan['apply_replacements'], dry_run=False)
    module.relink(db, plan['apply_replacements'], dry_run=False, accept_timing_risk=True)
    assert db.get_content(ID='1').FileType == 5
    assert db.get_content(ID='1').CuePosition == 1234


def test_batch_write_failure_rolls_back_all_tracks(library, tmp_path):
    db, analysis = library
    db.session.add(Track(ID='2', Title='Another Song', ArtistName='Artist',
                         FolderPath='/missing/second.wav', Length=3, rb_local_deleted=0))
    db.session.commit()
    items = [replacement(wav(tmp_path / 'first.wav')),
             {'track_id': '2', 'expected_old_path': '/missing/second.wav',
              'new_path': str(wav(tmp_path / 'second.wav'))}]
    plan = module.relink(db, items)
    update = db.update_content_path
    def fail_second(row, *args, **kwargs):
        if row.ID == '2':
            raise RuntimeError('second track failed')
        return update(row, *args, **kwargs)
    db.update_content_path = fail_second
    with pytest.raises(RuntimeError, match='SQL rolled back'):
        module.relink(db, plan['apply_replacements'], dry_run=False)
    assert db.get_content(ID='1').FolderPath == '/missing/old.wav'
    assert db.get_content(ID='2').FolderPath == '/missing/second.wav'
    assert analysis.read_bytes() == b'original-analysis-path'
