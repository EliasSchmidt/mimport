"""OCR auf Backcover-Bildern mit RapidOCR/ONNX.

Das Ergebnis bleibt bewusst schlicht: Rohtext plus Zeilen. Die Semantik
(Track/Artist/Duration) erledigen danach explizite Parser-Modi.
"""

from __future__ import annotations

import importlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Lock

from PIL import Image, ImageOps

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
    preview_bytes: bytes = b""
    preview_content_type: str = "image/jpeg"


_ocr_engine_cache = None
_OCR_LOCK = Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _ocr_model_dir() -> str:
    return (
        os.environ.get("MIMPORT_OCR_MODEL_DIR")
        or os.environ.get("PADDLE_OCR_BASE_DIR")
        or str(Path.home() / ".rapidocr")
    )


def _engine():
    global _ocr_engine_cache
    if _ocr_engine_cache is not None:
        return _ocr_engine_cache

    with _OCR_LOCK:
        if _ocr_engine_cache is not None:
            return _ocr_engine_cache

        use_angle_cls = _env_bool("MIMPORT_OCR_ANGLE_CLS", False)
        model_dir = _ocr_model_dir()
        max_side = _env_int("MIMPORT_OCR_MAX_IMAGE_SIDE", 1400)
        threads = _env_int("MIMPORT_OCR_THREADS", 2)
        log.info(
            "OCR-Engine wird geladen | engine=rapidocr | cls=%s | basis=%s | threads=%d | max_side=%d",
            "an" if use_angle_cls else "aus",
            model_dir,
            threads,
            max_side,
        )
        try:
            rapidocr_module = importlib.import_module("rapidocr")
            rapid_ocr_class = rapidocr_module.RapidOCR
            model_type_enum = rapidocr_module.ModelType
            lang_det_enum = rapidocr_module.LangDet
            lang_rec_enum = rapidocr_module.LangRec
            ocr_version_enum = rapidocr_module.OCRVersion
        except Exception as exc:
            raise OcrError(
                "RapidOCR ist nicht installiert. Bitte 'rapidocr' und 'onnxruntime' "
                "im Container installieren."
            ) from exc

        params: dict[str, object] = {
            "Global.model_root_dir": model_dir,
            "Global.max_side_len": max_side,
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": threads,
            # RapidOCR >=3.9 nutzt für PP-OCRv6 die kleinen Stufen tiny/small/medium.
            # Für europäische Tracklisten ist die lateinische Variante passender
            # als reines Englisch. Bei RapidOCR ist sie für DET als 'la' und für
            # REC als LATIN verfügbar.
            "Det.model_type": model_type_enum.SMALL,
            "Det.lang_type": "la",
            "Det.ocr_version": ocr_version_enum.PPOCRV6,
            "Rec.model_type": model_type_enum.SMALL,
            "Rec.lang_type": lang_rec_enum.LATIN,
            "Rec.ocr_version": ocr_version_enum.PPOCRV6,
        }

        if use_angle_cls:
            params["Cls.model_type"] = getattr(model_type_enum, "MOBILE", "mobile")
            params["Cls.lang_type"] = lang_det_enum.CH
            params["Cls.ocr_version"] = ocr_version_enum.PPOCRV4

        _ocr_engine_cache = rapid_ocr_class(params=params)
        log.info("OCR-Engine geladen | engine=rapidocr")
        return _ocr_engine_cache


def _normalize_lines(lines: list[str]) -> list[str]:
    normalized = [" ".join(str(line).split()) for line in lines if str(line).strip()]
    return [line for line in normalized if line]


def _extract_detections(raw: object) -> list[OcrDetection]:
    """Extrahiert Boxen, Text und Score aus der RapidOCR-Rückgabe."""
    detections: list[OcrDetection] = []
    if raw is None:
        return detections

    boxes = getattr(raw, "boxes", None)
    txts = getattr(raw, "txts", None)
    scores = getattr(raw, "scores", None)

    if boxes is None or txts is None or scores is None:
        to_json = getattr(raw, "to_json", None)
        if callable(to_json):
            payload = to_json() or []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                try:
                    box = [
                        (float(point[0]), float(point[1]))
                        for point in item.get("box", [])
                        if isinstance(point, (list, tuple)) and len(point) >= 2
                    ]
                    text = str(item.get("txt", "")).strip()
                    score = float(item.get("score", 0.0))
                except Exception:
                    continue
                if len(box) == 4 and text:
                    detections.append(OcrDetection(box=box, text=text, score=score))
            return detections
        return detections

    for box_points, text, score in zip(boxes, txts, scores, strict=False):
        try:
            box = [
                (float(point[0]), float(point[1]))
                for point in box_points
                if len(point) >= 2
            ]
            text_value = str(text).strip()
            score_value = float(score)
        except Exception:
            continue
        if len(box) == 4 and text_value:
            detections.append(OcrDetection(box=box, text=text_value, score=score_value))

    return detections


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


def _prepare_image(image_bytes: bytes) -> tuple[bytes, str, int, int, list[str]]:
    """Bereitet ein Bild speicherschonend für OCR und Overlay auf."""
    max_side = _env_int("MIMPORT_OCR_MAX_IMAGE_SIDE", 1400)
    warnings: list[str] = []
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            original_width = int(image.width)
            original_height = int(image.height)
            largest_side = max(original_width, original_height)
            if largest_side > max_side:
                scale = max_side / float(largest_side)
                new_width = max(1, round(original_width * scale))
                new_height = max(1, round(original_height * scale))
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
                warnings.append(
                    f"Bild wurde für OCR von {original_width}×{original_height} auf {new_width}×{new_height} verkleinert."
                )

            if image.mode != "RGB":
                image = image.convert("RGB")

            out = BytesIO()
            image.save(out, format="JPEG", quality=88, optimize=True)
            prepared = out.getvalue()
            return prepared, "image/jpeg", int(image.width), int(image.height), warnings
    except Exception:
        log.exception("Backcover-Bildvorbereitung fehlgeschlagen")
        width, height = _image_size(image_bytes)
        return image_bytes, "image/jpeg", width, height, warnings


def preload() -> None:
    """Lädt die OCR-Engine vorab und protokolliert Dauer/Fehler."""
    start = time.perf_counter()
    try:
        _engine()
    except Exception:
        log.exception("OCR-Preload fehlgeschlagen")
        return

    log.info("OCR-Preload abgeschlossen | dauer_s=%.2f", time.perf_counter() - start)


def recognize(image_bytes: bytes, *, suffix: str = ".jpg") -> OcrResult:
    """Liest Text aus einem Bild."""
    if not image_bytes:
        raise OcrError("Leeres Bild empfangen.")

    tmp_path: Path | None = None
    try:
        original_width, original_height = _image_size(image_bytes)
        prepared_bytes, preview_content_type, width, height, warnings = _prepare_image(image_bytes)
        with tempfile.NamedTemporaryFile(suffix=suffix or ".jpg", delete=False) as tmp:
            tmp.write(prepared_bytes)
            tmp_path = Path(tmp.name)

        use_angle_cls = _env_bool("MIMPORT_OCR_ANGLE_CLS", False)
        log.info(
            "OCR startet | datei=%s | original=%dx%d | vorbereitet=%dx%d | original_kb=%d | vorbereitet_kb=%d",
            tmp_path.suffix or suffix,
            original_width,
            original_height,
            width,
            height,
            len(image_bytes) // 1024,
            len(prepared_bytes) // 1024,
        )
        engine = _engine()
        log.info(
            "OCR-Inferenz beginnt | engine=rapidocr | cls=%s | pfad=%s",
            "an" if use_angle_cls else "aus",
            tmp_path,
        )
        raw = engine(str(tmp_path), use_cls=use_angle_cls, use_det=True, use_rec=True)
        detections = _extract_detections(raw)
        lines = _normalize_lines([item.text for item in detections])
        log.info("OCR-Inferenz fertig | zeilen=%d | boxen=%d", len(lines), len(detections))

        if not lines:
            warnings.append("Kein Text erkannt. Bitte anderes Foto (schärfer/gerader/heller).")

        return OcrResult(
            text="\n".join(lines),
            lines=lines,
            detections=detections,
            image_width=width,
            image_height=height,
            warnings=warnings,
            preview_bytes=prepared_bytes,
            preview_content_type=preview_content_type,
        )
    except OcrError:
        raise
    except Exception as exc:
        log.exception("OCR fehlgeschlagen")
        raise OcrError(f"OCR fehlgeschlagen: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                log.warning("Temporäres OCR-Bild konnte nicht gelöscht werden: %s", tmp_path)
