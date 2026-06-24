"""Debug media artifact helper for VLM model-call logging.

In ``metadata_only`` mode (default/production): build media refs containing only
path, MIME type, lightweight file metadata (size/hash), and — when available —
frame provenance/selection scalars (frame_index, timestamp_ms, motion/scene/blur/
brightness, decode_backend, selection_reason). No base64, no artifact copy.

In ``debug_full_media`` mode (explicit, opt-in): copy each frame file into a
subdirectory of ``artifact_root`` keyed by the model_call_id, and add an
``artifact_uri`` field to each ref pointing to the copy. The original source
files are preserved unchanged.

Safety rules baked in (table-schema-spec §4.5):
- Never stores base64 in the returned dicts regardless of mode.
- Never exposes the raw ``source_uri`` (internal video path) in refs.
- ``artifact_root`` is required and used as the only write target in debug mode.
- If a frame file is missing/unreadable the ref entry is still created without
  crashing; the artifact_uri field is simply omitted with an ``error`` key.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from cctv_memory.services.frame_stream import SelectedFrame


def build_media_refs(
    frames: list[str] | list[SelectedFrame],
    *,
    model_call_id: str,
    debug_media_retention: bool,
    artifact_root: str,
) -> list[dict[str, object]]:
    """Build media refs for a VLM call.

    Args:
        frames: Either selected-frame paths (``list[str]``, e.g. whole-clip/video
            mode or legacy callers) OR ``list[SelectedFrame]`` carrying frame
            provenance + selection scalars (the OpenCV FrameStream path).
        model_call_id: Unique ID for this call; used as subdirectory name in debug mode.
        debug_media_retention: Whether to copy artifacts and add artifact_uri refs.
        artifact_root: Base directory under which debug artifacts are written.

    Returns:
        List of dicts suitable for ``ModelCallLog.media_refs``. Never contains
        base64 data, never contains source_uri.
    """
    refs: list[dict[str, object]] = []
    artifact_dir: Path | None = None

    if debug_media_retention and frames:
        artifact_dir = Path(artifact_root) / "model_call_media" / model_call_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

    for item in frames:
        frame_path = item.uri if isinstance(item, SelectedFrame) else item
        ref: dict[str, object] = {
            "uri": frame_path,
            "mime": _guess_mime(frame_path),
        }
        if isinstance(item, SelectedFrame):
            # Frame provenance + selection scalars (no pixels, no base64).
            ref["frame_index"] = item.frame_index
            ref["timestamp_ms"] = item.timestamp_ms
            ref["decode_backend"] = item.decode_backend
            ref["selection_reason"] = item.selection_reason
            ref["motion_score"] = item.motion
            ref["scene_score"] = item.scene
            ref["blur_score"] = item.blur
            ref["brightness"] = item.brightness
        try:
            stat = os.stat(frame_path)
            ref["size_bytes"] = stat.st_size
            if stat.st_size < 4 * 1024 * 1024:  # only hash small files
                with open(frame_path, "rb") as fh:  # noqa: WPS515
                    ref["sha256"] = hashlib.sha256(fh.read()).hexdigest()[:16]
        except OSError:
            pass  # file may be a placeholder path in tests/mock mode

        if debug_media_retention and artifact_dir is not None:
            dest = artifact_dir / Path(frame_path).name
            try:
                if not dest.exists():
                    shutil.copy2(frame_path, dest)
                ref["artifact_uri"] = str(dest)
            except OSError as exc:
                ref["artifact_copy_error"] = str(exc)

        refs.append(ref)
    return refs


def _guess_mime(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".mp4"):
        return "video/mp4"
    return "application/octet-stream"
