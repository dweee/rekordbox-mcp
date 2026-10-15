"""Read-only candidate discovery and explicit, recoverable path replacement.

Matching hints never prove identical audio or timing. No source files are moved,
renamed, deleted, converted, retagged or re-imported by these operations.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata

from mutagen import File as AudioFile
from pydantic import BaseModel, ConfigDict
from pyrekordbox.utils import get_rekordbox_pid
from pyrekordbox.db6.tables import FileType

EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav", ".aif", ".aiff"}
TIMING_WARNING = (
    "Matching tags and duration do not prove identical audio timing. Different "
    "encodes, edits or leading silence can shift cues and beatgrids. Verify in Rekordbox."
)


class RelinkReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track_id: str
    expected_old_path: str
    new_path: str
    expected_sha256: str | None = None


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def similarity(left, right):
    left, right = normalize(left), normalize(right)
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def inspect_audio(path, fingerprint=False, hash_file=False):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        raise ValueError(f"Unsupported audio file: {path}")
    before = signature(path)
    # ffprobe provides container-independent codec and physical audio parameters.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    data = json.loads(probe.stdout)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        raise ValueError(f"No audio stream: {path}")
    fmt = data.get("format", {})
    tags = {k.casefold(): v for k, v in {**fmt.get("tags", {}), **audio.get("tags", {})}.items()}
    # Easy tags cover e.g. ID3 artist/title names when ffprobe differs by format.
    easy = AudioFile(path, easy=True)
    if easy and easy.tags:
        for key in ("artist", "title", "album"):
            if easy.get(key):
                tags[key] = "; ".join(easy[key])
    duration = float(audio.get("duration") or fmt.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Unknown audio duration: {path}")
    codec = audio.get("codec_name", "")
    result = {
        "path": str(path), "title": tags.get("title", ""), "artist": tags.get("artist", ""),
        "album": tags.get("album", ""), "duration_seconds": duration,
        "codec": codec, "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
        "bit_depth": int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0),
        "bitrate_bps": int(audio.get("bit_rate") or fmt.get("bit_rate") or 0),
        "lossless_container": codec in {"flac", "alac"} or codec.startswith("pcm_"),
        "file_size": path.stat().st_size,
        "quality_note": "Codec properties measured; provenance, transcodes and spectral quality not verified.",
    }
    if hash_file:
        result["sha256"] = sha256(path)
    if fingerprint:
        fp = subprocess.run(["fpcalc", "-json", str(path)], capture_output=True,
                            text=True, check=True, timeout=120)
        result["chromaprint"] = json.loads(fp.stdout)
    if signature(path) != before:
        raise ValueError(f"Audio changed during inspection: {path}")
    return result


def describe(content):
    return {
        "track_id": str(content.ID), "title": content.Title or "",
        "artist": getattr(content, "ArtistName", "") or "",
        "old_path": content.FolderPath or "",
        "duration_seconds": int(getattr(content, "Length", 0) or 0),
    }


def catalog_audio(path):
    """Fast tag/header scan; full ffprobe/hash inspection happens at preview."""
    audio = AudioFile(path, easy=True)
    if audio is None or not getattr(audio, "info", None):
        raise ValueError("Unrecognized audio header")
    info = audio.info
    def tag(key):
        return "; ".join(audio.get(key, [])) if audio.tags else ""
    codec = type(info).__name__
    duration = float(getattr(info, "length", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Unknown audio duration")
    return {"path": str(path), "title": tag("title"), "artist": tag("artist"),
            "album": tag("album"), "duration_seconds": duration,
            "codec": codec, "sample_rate": int(getattr(info, "sample_rate", 0)),
            "bit_depth": int(getattr(info, "bits_per_sample", 0)),
            "bitrate_bps": int(getattr(info, "bitrate", 0)),
            "lossless_container": path.suffix.lower() in {".flac", ".wav", ".aif", ".aiff"},
            "quality_note": "Tag/header scan only; run inspect_audio_files for full properties."}


def find_candidates(db, roots, track_ids=None, top_n=5, max_files=50000):
    if not 1 <= top_n <= 20 or not 1 <= max_files <= 200000:
        raise ValueError("top_n must be 1..20; max_files must be 1..200000")
    if not roots:
        raise ValueError("Supply explicit source directories")
    contents = [c for c in db.get_content() if not getattr(c, "rb_local_deleted", 0)]
    if track_ids is not None:
        requested = set(map(str, track_ids))
        available = {str(c.ID) for c in contents}
        if requested - available:
            raise ValueError(f"Unknown or deleted track IDs: {sorted(requested - available)}")
        contents = [c for c in contents if str(c.ID) in requested]
    else:
        contents = [c for c in contents if not c.FolderPath or not Path(c.FolderPath).is_file()]
    paths = set()
    truncated = False
    for root in roots:
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"Not a source directory: {root}")
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix.lower() in EXTENSIONS:
                    paths.add(path.resolve())
                    if len(paths) >= max_files:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break
    audio_files, errors = [], []
    for path in sorted(paths):
        try:
            audio_files.append(catalog_audio(path))
        except Exception as exc:
            db.registry.enable_tracking()
            errors.append({"path": str(path), "error": str(exc)})
    result = []
    for content in contents:
        track = describe(content)
        ranked = []
        for audio in audio_files:
            title_score = similarity(track["title"], audio["title"])
            artist_score = similarity(track["artist"], audio["artist"])
            name_score = max(similarity(Path(track["old_path"]).stem, Path(audio["path"]).stem),
                             similarity(f'{track["artist"]} {track["title"]}', Path(audio["path"]).stem))
            if max(title_score, name_score) < 0.5:
                continue
            delta = abs(track["duration_seconds"] - audio["duration_seconds"]) if track["duration_seconds"] else None
            duration_score = max(0, 1 - delta / 10) if delta is not None else 0
            score = max(0.55 * title_score + 0.3 * artist_score + 0.15 * duration_score,
                        0.8 * name_score + 0.2 * duration_score)
            ranked.append({**audio, "match_score": round(score, 4),
                           "duration_delta_seconds": delta,
                           "identity_verified": False, "timing_verified": False})
        ranked.sort(key=lambda a: (-a["match_score"], not a["lossless_container"], a["path"]))
        result.append({**track, "candidates": ranked[:top_n],
                       "ambiguous": len(ranked) > 1 and ranked[0]["match_score"] - ranked[1]["match_score"] < 0.08})
    return {"tracks": result, "files_scanned": len(paths), "truncated": truncated,
            "errors": errors, "warning": TIMING_WARNING,
            "next_step": "Select exact replacements, then call relink_tracks with dry_run=true."}


@contextmanager
def library_lock(db):
    directory = Path(db.db_directory)
    fd = os.open(directory / ".rekordbox-mcp-relink.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def require_closed():
    if get_rekordbox_pid():
        raise RuntimeError("Close Rekordbox before applying relinks")


def backup_library(db, rows, plan):
    # Snapshot encrypted database, WAL/SHM and only the analysis files touched.
    # No key or decrypted database is written to disk.
    database = Path(db.engine.url.database).resolve(strict=True)
    sources = [database]
    sources += [Path(str(database) + suffix) for suffix in ("-wal", "-shm")
                if Path(str(database) + suffix).exists()]
    xml = Path(db.db_directory) / "masterPlaylists6.xml"
    if xml.exists():
        sources.append(xml)
    analysis = set()
    for row in rows:
        analysis.update(Path(p).resolve(strict=True) for p in db.read_anlz_files(row.ID))
    sources += sorted(analysis)
    base = Path(db.db_directory) / "mcp-relink-backups"
    base.mkdir(mode=0o700, exist_ok=True)
    base.chmod(0o700)
    backup = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-"), dir=base))
    signatures = {p: signature(p) for p in sources}
    copies = {}
    for index, source in enumerate(sources):
        target = backup / f"{index:04d}-{source.name}"
        shutil.copy2(source, target)
        target.chmod(0o600)
        if sha256(source) != sha256(target):
            raise RuntimeError("Backup verification failed")
        copies[source] = target
    if any(signature(p) != signatures[p] for p in sources):
        raise RuntimeError("Library changed during backup; no relinks applied")
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "plan": plan,
                "files": [{"original": str(p), "backup": str(t), "sha256": sha256(t)}
                          for p, t in copies.items()],
                "restore": "Close Rekordbox and all MCP clients before restoring all listed files together. "
                           "Remove master.db-wal/master.db-shm first; restore the backed-up sidecars if listed."}
    path = backup / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    path.chmod(0o600)
    return backup, {p: copies[p] for p in analysis}


def relink(db, replacements, dry_run=True, accept_timing_risk=False,
           only_missing=True, duration_tolerance=2.0):
    if not replacements or len(replacements) > 500:
        raise ValueError("Supply between 1 and 500 explicit replacements")
    if not math.isfinite(duration_tolerance) or not 0 <= duration_tolerance <= 10:
        raise ValueError("duration_tolerance must be 0..10 seconds")
    replacements = [RelinkReplacement.model_validate(r) for r in replacements]
    if len({r.track_id for r in replacements}) != len(replacements):
        raise ValueError("Duplicate track IDs")
    # Serialize across all clients. Refresh stale ORM entries before validation.
    with library_lock(db):
        db.rollback()
        rows, plan = [], []
        signatures = {}
        for replacement in replacements:
            row = db.get_content(ID=replacement.track_id)
            if row is None or getattr(row, "rb_local_deleted", 0):
                raise ValueError(f"Unknown or deleted track: {replacement.track_id}")
            old = row.FolderPath or ""
            if old != replacement.expected_old_path:
                raise ValueError(f"Stale old path for track {replacement.track_id}; preview again")
            new = Path(replacement.new_path).expanduser().resolve(strict=True)
            if old and new == Path(old).expanduser().resolve():
                raise ValueError(f"Track {replacement.track_id} already uses this file")
            if only_missing and old and Path(old).is_file():
                raise ValueError(f"Track {replacement.track_id} is present; set only_missing=false for an upgrade")
            signatures[new] = signature(new)
            audio = inspect_audio(new, hash_file=True)
            if replacement.expected_sha256 and audio["sha256"] != replacement.expected_sha256:
                raise ValueError(f"Replacement changed for track {replacement.track_id}")
            if not dry_run and not replacement.expected_sha256:
                raise ValueError("Apply requires expected_sha256 from a dry-run preview")
            length = int(getattr(row, "Length", 0) or 0)
            delta = abs(length - audio["duration_seconds"]) if length else None
            changed_format = Path(old).suffix.lower() != new.suffix.lower()
            timing_risk = delta is None or delta > duration_tolerance or changed_format
            if timing_risk and not accept_timing_risk and not dry_run:
                raise ValueError(f"Track {replacement.track_id}: duration unknown/different or format changed; "
                                 "review cues/grid and explicitly set accept_timing_risk=true")
            # Selection is explicit. Identity hints are reported, never auto-applied.
            plan.append({"track_id": replacement.track_id, "expected_old_path": old,
                         "new_path": str(new), "expected_sha256": audio["sha256"],
                         "duration_delta_seconds": delta, "format_changed": changed_format,
                         "requires_timing_risk_acceptance": timing_risk,
                         "title_similarity": similarity(row.Title, audio["title"]),
                         "artist_similarity": similarity(getattr(row, "ArtistName", ""), audio["artist"]),
                         "audio": audio, "warning": TIMING_WARNING})
            rows.append(row)
        if dry_run:
            return {"status": "preview", "replacements": plan, "library_modified": False,
                    "apply_replacements": [{k: p[k] for k in ("track_id", "expected_old_path", "new_path", "expected_sha256")}
                                           for p in plan]}
        require_closed()
        backup, analysis_copies = backup_library(db, rows, plan)
        # Recheck after backup; never apply a file that changed after inspection.
        require_closed()
        for path, before in signatures.items():
            if signature(path) != before:
                raise ValueError(f"Replacement changed after preview: {path}")
        committed = False
        try:
            for row, item in zip(rows, plan):
                db.update_content_path(row, item["new_path"], save=True, commit=False)
                audio = item["audio"]
                row.FileType = FileType[Path(item["new_path"]).suffix[1:].upper()].value
                row.FileSize = audio["file_size"]
                row.Length = round(audio["duration_seconds"])
                row.SampleRate = audio["sample_rate"]
                row.BitRate = round(audio["bitrate_bps"] / 1000)
                row.BitDepth = audio["bit_depth"]
            require_closed()
            # Same USN handling as pyrekordbox.commit, but paths do not change
            # playlists, so do not rewrite masterPlaylists6.xml after SQL commit.
            db.registry.autoincrement_local_update_count(set_row_usn=True)
            db.session.commit()
            committed = True
            db.registry.clear_buffer()
        except Exception as exc:
            if committed:
                raise RuntimeError(f"Relinks committed but post-commit cleanup failed; backup: {backup}") from exc
            db.rollback()
            for original, copy in analysis_copies.items():
                shutil.copy2(copy, original)
            raise RuntimeError(f"Relink failed; SQL rolled back and analysis restored. Backup: {backup}") from exc
        return {"status": "applied", "count": len(plan), "replacements": plan,
                "backup_manifest": str(backup / "manifest.json"), "warning": TIMING_WARNING}
