"""
Rekordbox MCP Server

A FastMCP-based server for rekordbox database management with real-time database access.
"""

import asyncio
import os
from functools import wraps
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastmcp import FastMCP
from loguru import logger
from pydantic import BaseModel, Field

from .database import RekordboxDatabase
from .relink import RelinkReplacement, find_candidates, inspect_audio, relink, library_lock, require_closed
from .models import (
    Track,
    Playlist,
    SearchOptions,
    HistorySession,
    HistoryTrack,
    HistoryStats,
)


# Initialize FastMCP server
mcp = FastMCP("Rekordbox Database MCP Server")

# Global database instance
db: Optional[RekordboxDatabase] = None
_db_initialized = False
_relink_lock = asyncio.Lock()
_database_tool_lock = asyncio.Lock()


def database_tool(*args, **kwargs):
    """Serialize tools sharing the ORM session; coordinate writers across clients."""
    mutations = {"create_playlist", "add_tracks_to_playlist", "add_track_to_playlist",
                 "remove_track_from_playlist", "delete_playlist", "import_track",
                 "import_tracks", "cleanup_orphaned_playlist_entries", "remove_broken_tracks"}
    def decorate(function):
        @wraps(function)
        async def serialized(*call_args, **call_kwargs):
            async with _database_tool_lock:
                if function.__name__ in mutations:
                    await ensure_database_connected()
                    with library_lock(db.db):
                        require_closed()
                        return await function(*call_args, **call_kwargs)
                return await function(*call_args, **call_kwargs)
        return mcp.tool(*args, **kwargs)(serialized)
    return decorate


class ServerConfig(BaseModel):
    """Server configuration model."""

    database_path: Optional[Path] = Field(
        default=None, description="Path to rekordbox database directory"
    )
    auto_detect: bool = Field(
        default=True, description="Automatically detect rekordbox database location"
    )
    backup_enabled: bool = Field(
        default=True, description="Enable automatic database backups before mutations"
    )


@database_tool()
async def search_tracks(
    query: str = "",
    artist: Optional[str] = None,
    title: Optional[str] = None,
    genre: Optional[str] = None,
    key: Optional[str] = None,
    bpm_min: Optional[float] = None,
    bpm_max: Optional[float] = None,
    rating_min: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Search tracks in the rekordbox database.

    Args:
        query: General search query (searches across multiple fields)
        artist: Filter by artist name
        title: Filter by track title
        genre: Filter by genre
        key: Filter by musical key (e.g., "5A", "12B")
        bpm_min: Minimum BPM
        bpm_max: Maximum BPM
        rating_min: Minimum rating (0-5)
        limit: Maximum number of results to return

    Returns:
        List of matching tracks with metadata
    """
    await ensure_database_connected()

    search_options = SearchOptions(
        query=query,
        artist=artist,
        title=title,
        genre=genre,
        key=key,
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        rating_min=rating_min,
        limit=limit,
    )

    tracks = await db.search_tracks(search_options)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_track_details(track_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific track.

    Args:
        track_id: The unique track identifier

    Returns:
        Detailed track information including metadata, cue points, and play history
    """
    await ensure_database_connected()

    track = await db.get_track_by_id(track_id)
    if not track:
        raise ValueError(f"Track with ID {track_id} not found")

    return track.model_dump()


@database_tool()
async def get_tracks_by_key(key: str) -> List[Dict[str, Any]]:
    """
    Get all tracks in a specific musical key.

    Args:
        key: Musical key (e.g., "5A", "12B")

    Returns:
        List of tracks in the specified key
    """
    await ensure_database_connected()

    search_options = SearchOptions(key=key, limit=1000)
    tracks = await db.search_tracks(search_options)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_tracks_by_bpm_range(
    bpm_min: float, bpm_max: float
) -> List[Dict[str, Any]]:
    """
    Get tracks within a specific BPM range.

    Args:
        bpm_min: Minimum BPM
        bpm_max: Maximum BPM

    Returns:
        List of tracks within the BPM range
    """
    await ensure_database_connected()

    search_options = SearchOptions(bpm_min=bpm_min, bpm_max=bpm_max, limit=1000)
    tracks = await db.search_tracks(search_options)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_genre_filepaths(genre: str) -> List[str]:
    """
    Get filepaths for all tracks matching a genre.

    This tool is optimized for token efficiency - returns only filepaths.
    Useful for creating export scripts for selective library exports.

    Args:
        genre: Genre term to search for (case-insensitive substring match)

    Returns:
        List of absolute filepaths for matching tracks
    """
    await ensure_database_connected()

    filepaths = await db.get_tracks_by_genre(genre)
    return filepaths


@database_tool()
async def get_most_played_tracks(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Get the most played tracks in the library.

    Args:
        limit: Maximum number of tracks to return

    Returns:
        List of most played tracks
    """
    await ensure_database_connected()

    tracks = await db.get_most_played_tracks(limit)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_top_rated_tracks(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Get the highest rated tracks in the library.

    Args:
        limit: Maximum number of tracks to return

    Returns:
        List of top rated tracks
    """
    await ensure_database_connected()

    tracks = await db.get_top_rated_tracks(limit)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_unplayed_tracks(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get tracks that have never been played.

    Args:
        limit: Maximum number of tracks to return

    Returns:
        List of unplayed tracks
    """
    await ensure_database_connected()

    tracks = await db.get_unplayed_tracks(limit)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_track_file_path(track_id: str) -> Dict[str, str]:
    """
    Get the file system path for a specific track.

    Args:
        track_id: The unique track identifier

    Returns:
        Dictionary containing file path information
    """
    await ensure_database_connected()

    track = await db.get_track_by_id(track_id)
    if not track:
        raise ValueError(f"Track with ID {track_id} not found")

    return {
        "track_id": track_id,
        "file_path": track.file_path or "",
        "file_name": track.file_path.split("/")[-1] if track.file_path else "",
    }


@database_tool()
async def search_tracks_by_filename(filename: str) -> List[Dict[str, Any]]:
    """
    Search for tracks by filename.

    Args:
        filename: Filename to search for (partial match)

    Returns:
        List of tracks matching the filename
    """
    await ensure_database_connected()

    tracks = await db.search_tracks_by_filename(filename)
    return [track.model_dump() for track in tracks]


@database_tool()
async def analyze_library(
    group_by: str = "genre", aggregate_by: str = "count", top_n: int = 10
) -> Dict[str, Any]:
    """
    Analyze library with grouping and aggregation.

    Args:
        group_by: Field to group by (genre, key, year, artist, rating)
        aggregate_by: Aggregation method (count, playCount, totalTime)
        top_n: Number of top results to return

    Returns:
        Analysis results
    """
    await ensure_database_connected()

    analysis = await db.analyze_library(group_by, aggregate_by, top_n)
    return analysis


@database_tool()
async def validate_track_ids(track_ids: List[str]) -> Dict[str, Any]:
    """
    Validate a list of track IDs and show which are valid/invalid.

    Args:
        track_ids: List of track IDs to validate

    Returns:
        Validation results with valid and invalid IDs
    """
    await ensure_database_connected()

    validation = await db.validate_track_ids(track_ids)
    return validation


@database_tool()
async def get_playlists() -> List[Dict[str, Any]]:
    """
    Get all playlists from the rekordbox database.

    Returns:
        List of playlists with metadata
    """
    await ensure_database_connected()

    playlists = await db.get_playlists()
    return [playlist.model_dump() for playlist in playlists]


@database_tool()
async def get_playlist_tracks(playlist_id: str) -> List[Dict[str, Any]]:
    """
    Get all tracks in a specific playlist.

    Args:
        playlist_id: The unique playlist identifier

    Returns:
        List of tracks in the playlist
    """
    await ensure_database_connected()

    tracks = await db.get_playlist_tracks(playlist_id)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_library_stats() -> Dict[str, Any]:
    """
    Get comprehensive library statistics.

    Returns:
        Dictionary containing various library statistics
    """
    await ensure_database_connected()

    stats = await db.get_library_stats()
    return stats.model_dump()


@database_tool()
async def connect_database(database_path: Optional[str] = None) -> Dict[str, str]:
    """
    Connect to the rekordbox database.

    Args:
        database_path: Optional path to database directory. If not provided, auto-detection is used.

    Returns:
        Connection status message
    """
    global db, _db_initialized

    try:
        if db:
            await db.disconnect()
        db = RekordboxDatabase()
        path = Path(database_path) if database_path else None
        await db.connect(database_path=path)
        _db_initialized = True

        return {
            "status": "success",
            "message": f"Connected to rekordbox database at {db.database_path}",
            "total_tracks": str(await db.get_track_count()),
        }
    except Exception as e:
        _db_initialized = False
        logger.error(f"Failed to connect to database: {e}")
        return {"status": "error", "message": f"Failed to connect: {str(e)}"}


@database_tool()
async def get_history_sessions(
    include_folders: bool = False, limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Get DJ history sessions from rekordbox.

    Args:
        include_folders: Whether to include folder entries (years/months)
        limit: Maximum number of sessions to return

    Returns:
        List of history sessions with metadata
    """
    await ensure_database_connected()

    sessions = await db.get_history_sessions(include_folders=include_folders)
    # Sort by date created, most recent first
    sessions.sort(key=lambda x: x.date_created or "", reverse=True)
    return [session.model_dump() for session in sessions[:limit]]


@database_tool()
async def get_session_tracks(session_id: str) -> List[Dict[str, Any]]:
    """
    Get all tracks from a specific DJ history session.

    Args:
        session_id: The session's unique identifier

    Returns:
        List of tracks in the session with performance context
    """
    await ensure_database_connected()

    tracks = await db.get_session_tracks(session_id)
    return [track.model_dump() for track in tracks]


@database_tool()
async def get_recent_sessions(days: int = 30) -> List[Dict[str, Any]]:
    """
    Get recent DJ history sessions within the specified number of days.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        List of recent history sessions
    """
    await ensure_database_connected()

    from datetime import datetime, timedelta

    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")

    sessions = await db.get_history_sessions(include_folders=False)

    # Filter by date
    recent_sessions = [
        s for s in sessions if s.date_created and s.date_created >= cutoff_str
    ]

    # Sort by date, most recent first
    recent_sessions.sort(key=lambda x: x.date_created or "", reverse=True)
    return [session.model_dump() for session in recent_sessions]


@database_tool()
async def get_history_stats() -> Dict[str, Any]:
    """
    Get comprehensive statistics about DJ history sessions.

    Returns:
        Statistics about all history sessions including totals and trends
    """
    await ensure_database_connected()

    stats = await db.get_history_stats()
    return stats.model_dump()


@database_tool()
async def search_history_sessions(
    query: str = "",
    year: Optional[str] = None,
    month: Optional[str] = None,
    min_tracks: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Search DJ history sessions with various filters.

    Args:
        query: Search query for session names
        year: Filter by year (e.g., "2025")
        month: Filter by month (e.g., "08" for August)
        min_tracks: Minimum number of tracks in session
        limit: Maximum number of results

    Returns:
        List of matching history sessions
    """
    await ensure_database_connected()

    sessions = await db.get_history_sessions(include_folders=False)

    # Apply filters
    filtered_sessions = []
    for session in sessions:
        # Text search
        if query and query.lower() not in session.name.lower():
            continue

        # Date filters
        if session.date_created:
            if year and not session.date_created.startswith(year):
                continue
            if month and year:
                month_str = f"{year}-{month.zfill(2)}"
                if not session.date_created.startswith(month_str):
                    continue
        elif year or month:
            # Skip if date filters specified but no date available
            continue

        # Track count filter
        if min_tracks and session.track_count < min_tracks:
            continue

        filtered_sessions.append(session)

    # Sort by date, most recent first
    filtered_sessions.sort(key=lambda x: x.date_created or "", reverse=True)
    return [session.model_dump() for session in filtered_sessions[:limit]]


# Playlist Mutation Tools (with safety annotations)


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    }
)
async def create_playlist(
    name: str, parent_id: Optional[str] = None, is_folder: bool = False
) -> Dict[str, Any]:
    """
    Create a new playlist or folder in rekordbox.

    ⚠️ CAUTION: This modifies your rekordbox database!

    Args:
        name: Name for the new playlist or folder
        parent_id: Optional parent folder ID (omit for root level)
        is_folder: If True, create as a folder that can contain other playlists

    Returns:
        Information about the created playlist or folder
    """
    await ensure_database_connected()

    if not name.strip():
        raise ValueError("Playlist name cannot be empty")

    try:
        playlist_id = await db.create_playlist(name.strip(), parent_id, is_folder)
        item_type = "folder" if is_folder else "playlist"
        return {
            "status": "success",
            "message": f"Created {item_type} '{name}'",
            "playlist_id": playlist_id,
            "playlist_name": name,
            "is_folder": is_folder,
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to create playlist: {str(e)}"}


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    }
)
async def add_tracks_to_playlist(
    playlist_id: str, track_ids: List[str]
) -> Dict[str, Any]:
    """
    Add multiple tracks to an existing playlist in one operation.

    ⚠️ CAUTION: This modifies your rekordbox database!

    Args:
        playlist_id: ID of the playlist to modify
        track_ids: List of track IDs to add

    Returns:
        Detailed results of the batch operation
    """
    await ensure_database_connected()

    try:
        results = await db.add_tracks_to_playlist(playlist_id, track_ids)

        return {
            "status": "success",
            "message": f"Batch add completed: {len(results['added'])} added, {len(results['skipped'])} skipped, {len(results['failed'])} failed",
            "playlist_id": playlist_id,
            "summary": {
                "added_count": len(results["added"]),
                "skipped_count": len(results["skipped"]),
                "failed_count": len(results["failed"]),
            },
            "details": results,
        }

    except Exception as e:
        logger.error(f"Failed to add tracks to playlist: {e}")
        return {
            "status": "error",
            "message": f"Failed to add tracks to playlist: {str(e)}",
        }


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    }
)
async def add_track_to_playlist(playlist_id: str, track_id: str) -> Dict[str, Any]:
    """
    Add a track to an existing playlist.

    ⚠️ CAUTION: This modifies your rekordbox database!

    Args:
        playlist_id: ID of the playlist to modify
        track_id: ID of the track to add

    Returns:
        Result of the operation
    """
    await ensure_database_connected()

    try:
        success = await db.add_track_to_playlist(playlist_id, track_id)
        if success:
            return {
                "status": "success",
                "message": f"Added track {track_id} to playlist {playlist_id}",
                "playlist_id": playlist_id,
                "track_id": track_id,
            }
        else:
            return {"status": "error", "message": "Failed to add track to playlist"}
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to add track to playlist: {str(e)}",
        }


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    }
)
async def remove_track_from_playlist(playlist_id: str, track_id: str) -> Dict[str, Any]:
    """
    Remove a track from a playlist.

    ⚠️ CAUTION: This modifies your rekordbox database!

    Args:
        playlist_id: ID of the playlist to modify
        track_id: ID of the track to remove

    Returns:
        Result of the operation
    """
    await ensure_database_connected()

    try:
        success = await db.remove_track_from_playlist(playlist_id, track_id)
        if success:
            return {
                "status": "success",
                "message": f"Removed track {track_id} from playlist {playlist_id}",
                "playlist_id": playlist_id,
                "track_id": track_id,
            }
        else:
            return {
                "status": "error",
                "message": "Failed to remove track from playlist",
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to remove track from playlist: {str(e)}",
        }


@database_tool(
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}
)
async def delete_playlist(playlist_id: str) -> Dict[str, Any]:
    """
    Delete a playlist from rekordbox.

    ⚠️ DANGER: This permanently deletes a playlist and cannot be undone!

    Args:
        playlist_id: ID of the playlist to delete

    Returns:
        Result of the operation
    """
    await ensure_database_connected()

    try:
        # Get playlist info before deletion for confirmation
        playlists = await db.get_playlists()
        target_playlist = next((p for p in playlists if p.id == playlist_id), None)

        if not target_playlist:
            return {"status": "error", "message": f"Playlist {playlist_id} not found"}

        # Prevent deletion of smart playlists for safety
        if target_playlist.is_smart_playlist:
            return {
                "status": "error",
                "message": "Cannot delete smart playlists - they are managed by rekordbox",
            }

        success = await db.delete_playlist(playlist_id)
        if success:
            return {
                "status": "success",
                "message": f"Deleted playlist '{target_playlist.name}' ({playlist_id})",
                "deleted_playlist": {
                    "id": playlist_id,
                    "name": target_playlist.name,
                    "track_count": target_playlist.track_count,
                },
            }
        else:
            return {"status": "error", "message": "Failed to delete playlist"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete playlist: {str(e)}"}


# Import Tools


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    }
)
async def import_track(
    path: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    genre: Optional[str] = None,
    label: Optional[str] = None,
    bpm: Optional[float] = None,
    rating: Optional[int] = None,
    comments: Optional[str] = None,
    year: Optional[int] = None,
    auto_tag: bool = True,
) -> Dict[str, Any]:
    """
    Import a single audio file into the rekordbox library.

    ⚠️ CAUTION: This modifies your rekordbox database — rekordbox must be closed.

    The track is added as unanalyzed; open rekordbox and run "Analyze Tracks"
    to generate waveforms, beatgrids, and hot cues.

    Args:
        path: Absolute path to the audio file (mp3/m4a/flac/wav/aiff)
        title: Override the title (defaults to ID3 tag if auto_tag=True)
        artist: Override the artist (creates artist row if new)
        album: Override the album
        genre: Override the genre
        label: Override the record label
        bpm: Override the BPM (float, e.g. 128.0)
        rating: Override the rating (0–5)
        comments: Track comments
        year: Release year
        auto_tag: Read ID3 tags via mutagen and prefill metadata (default True).
            Explicit arguments always win over tag values.

    Returns:
        Result with status (success/skipped/error), track_id, and resolved metadata.
    """
    await ensure_database_connected()

    metadata = {
        "title": title,
        "artist": artist,
        "album": album,
        "genre": genre,
        "label": label,
        "bpm": bpm,
        "rating": rating,
        "comments": comments,
        "year": year,
    }

    return await db.import_track(path, metadata=metadata, auto_tag=auto_tag)


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    }
)
async def import_tracks(
    paths: List[str],
    recursive: bool = True,
    auto_tag: bool = True,
    extensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Batch-import audio files and/or directories into the rekordbox library.

    ⚠️ CAUTION: This modifies your rekordbox database — rekordbox must be closed.

    Tracks are added as unanalyzed. Open rekordbox afterwards and run
    "Analyze Tracks" on the new imports to generate waveforms/beatgrids/hot cues.
    Tip: follow up with add_tracks_to_playlist using the returned track_ids to
    stage them for easy selection in rekordbox.

    Args:
        paths: List of file paths and/or directory paths.
        recursive: If a directory is given, descend into subdirectories (default True).
        auto_tag: Read ID3 tags via mutagen for each file (default True).
        extensions: Restrict directory scans to these extensions
            (e.g. ["mp3", "flac"]). Defaults to all supported types.

    Returns:
        Summary with counts and per-file details for imported / skipped / failed.
    """
    await ensure_database_connected()
    return await db.import_tracks(
        paths=paths,
        recursive=recursive,
        auto_tag=auto_tag,
        extensions=extensions,
    )


@database_tool(annotations={"readOnlyHint": True, "destructiveHint": False})
async def inspect_audio_files(paths: List[str], fingerprint: bool = False) -> Dict[str, Any]:
    """Inspect codec, duration, tags and SHA256; optionally compute Chromaprint.

    Does not require a database. A lossless format does not prove a lossless
    source. Fingerprints are evidence for manual comparison, not timing proof.
    """
    if not 1 <= len(paths) <= 100:
        raise ValueError("Supply 1..100 audio paths")
    results = []
    for path in paths:
        try:
            results.append(await asyncio.to_thread(inspect_audio, path, fingerprint, True))
        except Exception as exc:
            results.append({"path": path, "error": str(exc)})
    return {"files": results}


@database_tool(annotations={"readOnlyHint": True, "destructiveHint": False})
async def find_relink_candidates(
    source_directories: List[str], track_ids: Optional[List[str]] = None,
    top_n: int = 5, max_files: int = 50000,
) -> Dict[str, Any]:
    """Find differently named source files using tags, filenames and duration.

    Defaults to missing tracks. Supply track_ids to also inspect existing tracks
    for upgrades. Candidates are suggestions: ambiguous matches, alternate
    edits and timing need review. Does not modify any track or source file.
    """
    await ensure_database_connected()
    async with _relink_lock:
        return await asyncio.to_thread(find_candidates, db.db, source_directories,
                                       track_ids, top_n, max_files)


@database_tool(annotations={"readOnlyHint": False, "destructiveHint": False})
async def relink_tracks(
    replacements: List[RelinkReplacement], dry_run: bool = True,
    accept_timing_risk: bool = False, only_missing: bool = True,
    duration_tolerance: float = 2.0,
) -> Dict[str, Any]:
    """Preview/apply selected replacements without deleting/re-importing tracks.

    Always preview first. Pass preview's apply_replacements, including hashes,
    to apply with dry_run=false. Rekordbox must be closed for writes. Existing
    IDs, playlists, ratings, cues and beatgrids are retained; path references in
    ANLZ files and physical file properties are updated. Different file formats
    or unknown/different durations require accept_timing_risk=true. Matching
    duration does not guarantee cue alignment: verify playback and grid/cues.
    Database/WAL/SHM and affected ANLZ files are backed up, failing closed if
    backup fails. A write failure rolls back SQL and restores affected ANLZ.
    Set only_missing=false explicitly to upgrade a file that is still present.
    """
    await ensure_database_connected()
    async with _relink_lock:
        result = await asyncio.to_thread(relink, db.db, replacements, dry_run,
                                        accept_timing_risk, only_missing, duration_tolerance)
        if not dry_run:
            db._invalidate_content_cache()
        return result


# Cleanup Tools


@database_tool()
async def find_broken_tracks() -> Dict[str, Any]:
    """
    Scan the library for broken tracks and orphaned playlist references.

    Detects:
    - Tracks with empty file paths
    - Apple Music streaming references (can't export to USB/CDJ)
    - Tracks pointing to files that no longer exist on disk
    - Orphaned playlist entries referencing deleted tracks (causes blank USB export errors)

    Returns:
        Report with broken tracks grouped by category and a summary with counts
    """
    await ensure_database_connected()

    return await db.find_broken_tracks()


@database_tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    }
)
async def cleanup_orphaned_playlist_entries() -> Dict[str, Any]:
    """
    Remove orphaned playlist entries that reference deleted tracks.

    These orphaned entries cause blank '[1] The file doesn't exist' errors
    when exporting to USB. This tool removes the stale PlaylistSong rows
    while preserving all active tracks and playlists.

    ⚠️ CAUTION: This modifies your rekordbox database! A backup is created automatically.

    Returns:
        Count and details of removed orphaned entries
    """
    await ensure_database_connected()

    return await db.remove_orphaned_playlist_entries()


@database_tool(
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}
)
async def remove_broken_tracks(track_ids: List[str]) -> Dict[str, Any]:
    """
    Soft-delete tracks by ID and remove them from all playlists.

    Use find_broken_tracks first to identify which tracks to remove.
    Tracks are soft-deleted (marked as deleted in the database, not permanently erased).

    ⚠️ DANGER: This removes tracks from your rekordbox library!

    Args:
        track_ids: List of track IDs to remove

    Returns:
        Details of removed tracks and any IDs that were not found
    """
    await ensure_database_connected()

    return await db.remove_tracks_by_ids(track_ids)


@mcp.resource("file://database-status")
async def database_status() -> str:
    """Get the current database connection status."""
    if not db:
        return (
            "Database not connected. Use connect_database tool to establish connection."
        )

    if await db.is_connected():
        track_count = await db.get_track_count()
        return f"Connected to rekordbox database at {db.database_path}. Total tracks: {track_count}"
    else:
        return "Database connection lost. Please reconnect."


async def ensure_database_connected():
    """Ensure database is connected, initialize if not."""
    global db, _db_initialized

    if _db_initialized and db and await db.is_connected():
        return

    if not _db_initialized:
        logger.info("Initializing database connection...")

        try:
            db = RekordboxDatabase()
            await db.connect(database_path=Path(os.environ["REKORDBOX_DATABASE_PATH"])
                             if os.environ.get("REKORDBOX_DATABASE_PATH") else None)

            track_count = await db.get_track_count()
            playlist_count = len(await db.get_playlists())

            logger.success(f"✅ Connected to rekordbox database!")
            logger.info(
                f"📊 Database contains {track_count} tracks and {playlist_count} playlists"
            )
            _db_initialized = True

        except Exception as e:
            logger.error(f"❌ Failed to connect to rekordbox database: {e}")
            logger.error("🔧 Please ensure:")
            logger.error("   - Rekordbox is closed")
            logger.error(
                "   - Database key is available (run: uv run python -m pyrekordbox download-key)"
            )
            logger.error("   - Database path is accessible")
            raise RuntimeError(f"Database initialization failed: {str(e)}")

    elif db and not await db.is_connected():
        # Reconnect if connection was lost
        await db.connect()


def main():
    """Main entry point for the MCP server."""
    import sys
    import asyncio
    import argparse

    parser = argparse.ArgumentParser(description="Rekordbox database MCP server")
    parser.add_argument("--database-path", help="Rekordbox database directory")
    args = parser.parse_args()
    if args.database_path:
        os.environ["REKORDBOX_DATABASE_PATH"] = args.database_path

    # Configure logging
    logger.remove()
    logger.add(
        sink=sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level="INFO",
    )

    try:
        # Run the FastMCP server (database will be initialized on first tool call)
        mcp.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
    finally:
        # Final cleanup
        if db:
            try:
                asyncio.run(db.disconnect())
            except Exception as e:
                logger.warning(f"Error during final cleanup: {e}")


if __name__ == "__main__":
    main()
