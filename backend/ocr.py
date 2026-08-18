"""OCR auf Backcover-Bildern mit PaddleOCR.

Das Ergebnis bleibt bewusst schlicht: Rohtext plus Zeilen. Die Semantik
(Track/Artist/Duration) erledigen danach explizite Parser-Modi.
"""

from __future__ import annotations

import importlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)


class OcrError(RuntimeError):
    """OCR kann nicht ausgeführt werden."""


@dataclass
class OcrDetection:
    """Eine einzelne erkannte Textbox."""

    box: list[tuple[float, float]]
    text: str
    score: float


@dataclass
class OcrResult:
    """Erkanntes OCR-Ergebnis."""

    text: str
    lines: list[str]
    detections: list[OcrDetection] = field(default_factory=list)
    image_width: int = 0
    image_height: int = 0
    warnings: list[str] = field(default_factory=list)


_ocr_engine_cache = None
_OCR_LOCK = Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _engine():
    global _ocr_engine_cache
    if _ocr_engine_cache is not None:
        return _ocr_engine_cache

    with _OCR_LOCK:
        if _ocr_engine_cache is not None:
            return _ocr_engine_cache
        use_angle_cls = _env_bool("MIMPORT_OCR_ANGLE_CLS", False)
        base_dir = os.environ.get("PADDLE_OCR_BASE_DIR") or str(Path.home() / ".paddleocr")
        log.info(
            "OCR-Engine wird geladen | angle_cls=%s | basis=%s",
            "an" if use_angle_cls else "aus",
            base_dir,
        )
        try:
            paddleocr_module = importlib.import_module("paddleocr")
            paddle_ocr_class = getattr(paddleocr_module, "PaddleOCR")
        except Exception as exc:
            raise OcrError(
                "PaddleOCR ist nicht installiert. Bitte 'paddleocr' und "
                "eine passende 'paddlepaddle'-Variante installieren."
            ) from exc

        # Das "mobile" Modell ist das Standardmodell von PaddleOCR und klein.
        _ocr_engine_cache = paddle_ocr_class(
            use_angle_cls=use_angle_cls,
            lang="en",
            show_log=False,
            use_gpu=False,
        )
        log.info("OCR-Engine geladen")
        return _ocr_engine_cache


def _normalize_lines(lines: list[str]) -> list[str]:
    normalized = [" ".join(str(line).split()) for line in lines if str(line).strip()]
    return [line for line in normalized if line]


def _extract_detections(raw: object) -> list[OcrDetection]:
    """Extrahiert Boxen, Text und Score aus der PaddleOCR-Rückgabe."""
    detections: list[OcrDetection] = []

    if not isinstance(raw, list):
        return detections

    for entry in raw:
        if not isinstance(entry, list):
            continue
        for item in entry:
            if not (
                isinstance(item, list)
                and len(item) >= 2
                and isinstance(item[0], list)
                and isinstance(item[1], (list, tuple))
                and item[1]
            ):
                continue

            try:
                box = [
                    (float(point[0]), float(point[1]))
                    for point in item[0]
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                text = str(item[1][0])
                score = float(item[1][1]) if len(item[1]) > 1 else 0.0
            except Exception:
                continue
            if len(box) == 4 and text.strip():
                detections.append(OcrDetection(box=box, text=text.strip(), score=score))

    return detections


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


def recognize(image_bytes: bytes, *, suffix: str = ".jpg") -> OcrResult:
    """Liest Text aus einem Bild."""
    if not image_bytes:
        raise OcrError("Leeres Bild empfangen.")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = Path(tmp.name)

        log.info(
            "OCR startet | datei=%s | groesse_kb=%d",
            tmp_path.suffix or suffix,
            len(image_bytes) // 1024,
        )
        engine = _engine()
        log.info("OCR-Inferenz beginnt | cls=%s | pfad=%s", "an" if _env_bool("MIMPORT_OCR_ANGLE_CLS", False) else "aus", tmp_path)
        raw = engine.ocr(str(tmp_path), cls=_env_bool("MIMPORT_OCR_ANGLE_CLS", False))
        detections = _extract_detections(raw)
        lines = _normalize_lines([item.text for item in detections])
        width, height = _image_size(image_bytes)
        log.info("OCR-Inferenz fertig | zeilen=%d | boxen=%d", len(lines), len(detections))

        warnings: list[str] = []
        if not lines:
            warnings.append("Kein Text erkannt. Bitte anderes Foto (schärfer/gerader/heller).")

        return OcrResult(
            text="\n".join(lines),
            lines=lines,
            detections=detections,
            image_width=width,
            image_height=height,
            warnings=warnings,
        )
    except OcrError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("OCR fehlgeschlagen")
        raise OcrError(f"OCR fehlgeschlagen: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                log.warning("Temporäres OCR-Bild konnte nicht gelöscht werden: %s", tmp_path)
