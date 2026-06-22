"""Optional built-in metadata/text adapters for governed media extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AdapterPayload, ArtifactBudget


def extract_pdf(
    path: Path,
    *,
    enable_ocr: bool,
    enable_transcription: bool,
    budget: ArtifactBudget,
) -> AdapterPayload:
    del enable_ocr, enable_transcription, budget
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    sections: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            sections.append(
                {
                    "kind": "pdf_page",
                    "text": text,
                    "metadata": {"page": page_number},
                }
            )
    return AdapterPayload(
        metadata={
            "pages": len(reader.pages),
            "encrypted": bool(reader.is_encrypted),
            "metadata": dict(reader.metadata or {}),
        },
        sections=sections,
    )


def extract_image(
    path: Path,
    *,
    enable_ocr: bool,
    enable_transcription: bool,
    budget: ArtifactBudget,
) -> AdapterPayload:
    del enable_ocr, enable_transcription, budget
    from PIL import Image

    with Image.open(path) as image:
        return AdapterPayload(
            metadata={
                "width": int(image.width),
                "height": int(image.height),
                "format": str(image.format or ""),
                "mode": str(image.mode or ""),
                "frames": int(getattr(image, "n_frames", 1) or 1),
            }
        )


def extract_video(
    path: Path,
    *,
    enable_ocr: bool,
    enable_transcription: bool,
    budget: ArtifactBudget,
) -> AdapterPayload:
    del enable_ocr, enable_transcription, budget
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("video could not be opened")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = frames / fps if fps > 0 else 0
        keyframes = []
        if duration > 0:
            for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
                keyframes.append(
                    {
                        "kind": "keyframe_locator",
                        "text": f"keyframe at {duration * ratio:.3f}s",
                        "metadata": {"seconds": round(duration * ratio, 3)},
                    }
                )
        return AdapterPayload(
            metadata={
                "fps": fps,
                "frames": frames,
                "width": width,
                "height": height,
                "duration_seconds": round(duration, 3),
            },
            sections=keyframes,
        )
    finally:
        capture.release()


def default_artifact_adapters() -> dict[str, Any]:
    return {
        "pdf": extract_pdf,
        "image": extract_image,
        "video": extract_video,
    }
