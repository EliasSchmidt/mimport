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
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)


class OcrError(RuntimeError):
    """OCR kann nicht ausgeführt werden."""


@dataclass
class OcrResult:
    """Erkanntes OCR-Ergebnis."""

    text: str
    lines: list[str]
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


def _extract_lines(raw: object) -> list[str]:
    """Extrahiert Textzeilen aus den unterschiedlichen PaddleOCR-Rückgaben."""

    lines: list[str] = []

    # Typisch: [ [ [box], (text, score) ], ... ]
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, list):
                # Ebene einer Seite oder einer Detection-Liste.
                for item in entry:
                    if (
                        isinstance(item, list)
                        and len(item) >= 2
                        and isinstance(item[1], (list, tuple))
                        and item[1]
                    ):
                        lines.append(str(item[1][0]))
                # Manche Versionen liefern schon direkt line-Items in entry.
                if (
                    isinstance(entry, list)
                    and len(entry) >= 2
                    and isinstance(entry[1], (list, tuple))
                    and entry[1]
                ):
                    lines.append(str(entry[1][0]))

    return _normalize_lines(lines)


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
        lines = _extract_lines(raw)
        log.info("OCR-Inferenz fertig | zeilen=%d", len(lines))

        warnings: list[str] = []
        if not lines:
            warnings.append("Kein Text erkannt. Bitte anderes Foto (schärfer/gerader/heller).")

        return OcrResult(text="\n".join(lines), lines=lines, warnings=warnings)
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
