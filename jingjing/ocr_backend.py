"""Local, CPU-only OCR and conservative light-theme bubble segmentation.

Images stay in memory. This module never reads screenshots from disk, downloads
models, or logs recognized text. Heavy optional dependencies load only on use.
NumPy inputs, like PIL screenshots, use RGB channel order; RapidOCR receives BGR.
"""

from __future__ import annotations

import math

from .ocr_parse import BubbleBox, OcrLine


class OcrError(RuntimeError):
    """A local OCR dependency, image, or inference failure."""


def _numpy():
    try:
        import numpy as np
    except ImportError:
        raise OcrError("缺少本地 OCR 依赖，请先重新运行 setup.ps1。") from None
    return np


def _rgb(image):
    """Return contiguous uint8 RGB; paths/URLs/encoded bytes are not accepted."""
    np = _numpy()
    if isinstance(image, np.ndarray):
        array = image
        if array.dtype != np.uint8:
            raise OcrError("OCR 图片必须使用 uint8 像素。")
        if array.ndim == 2:
            array = np.repeat(array[..., None], 3, axis=2)
        elif array.ndim == 3 and array.shape[2] == 4:
            alpha = array[..., 3:4].astype(np.float32) / 255.0
            array = np.rint(array[..., :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
        elif array.ndim != 3 or array.shape[2] != 3:
            raise OcrError("OCR 图片必须是灰度、RGB 或 RGBA。")
    else:
        try:
            from PIL import Image
        except ImportError:
            raise OcrError("缺少本地 OCR 依赖，请先重新运行 setup.ps1。") from None
        if not isinstance(image, Image.Image):
            raise OcrError("OCR 只接受内存中的图片，不接受文件路径或网址。")
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba)
        array = np.asarray(image.convert("RGB"))
    if array.shape[0] < 1 or array.shape[1] < 1:
        raise OcrError("OCR 图片为空。")
    return np.ascontiguousarray(array)


def locate_bubbles(image) -> list[BubbleBox]:
    """Find isolated neutral incoming / green outgoing text-bubble candidates.

    ``image`` must be the message viewport, without a window title or composer.
    This targets the standard Weixin light theme. Ambiguous areas, avatars,
    colored image cards and crop-edge fragments are excluded. On the newer
    near-white backdrop only gray bubbles qualify, so white image cards are
    ignored. A changed theme can yield no candidates; it must not turn into
    whole-window OCR, which would treat group nicknames as messages.
    """
    np = _numpy()
    try:
        import cv2
    except ImportError:
        raise OcrError("缺少本地 OCR 图像依赖，请先重新运行 setup.ps1。") from None
    rgb = _rgb(image)
    height, width = rgb.shape[:2]
    if width < 120 or height < 40:
        return []
    colors = rgb.astype(np.int16)
    red, green, blue = (colors[..., channel] for channel in range(3))
    neutral = colors.max(axis=2) - colors.min(axis=2)
    # Weixin 4.1 uses #ededed bubbles on #fafafa; older light themes use white
    # bubbles on a darker gray backdrop. Do not accept white picture backgrounds
    # as bubbles when the current theme uses gray incoming message fill.
    edges = np.concatenate((colors[0], colors[-1], colors[:, 0], colors[:, -1]))
    backdrop = float(np.median(edges))
    if backdrop >= 247:
        incoming = ((colors.min(axis=2) >= 223) & (colors.max(axis=2) <= 246)
                    & (neutral <= 6) & (colors.mean(axis=2) <= backdrop - 6))
    else:
        incoming = (colors.min(axis=2) >= 253) & (neutral <= 3)
    outgoing = (green >= 165) & (green - red >= 15) & (green - blue >= 28)
    candidates = []
    for direction, mask in (("incoming", incoming), ("outgoing", outgoing)):
        binary = mask.astype(np.uint8) * 255
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            left, top, box_width, box_height = cv2.boundingRect(contour)
            right, bottom = left + box_width, top + box_height
            if box_width < 18 or box_height < 18 or box_width * box_height < 350:
                continue
            if left <= 1 or top <= 1 or right >= width - 1 or bottom >= height - 1:
                continue
            if box_width > width * 0.86 or box_height > height * 0.70:
                continue
            if cv2.contourArea(contour) < box_width * box_height * 0.70:
                continue
            if np.mean(mask[top:bottom, left:right]) < 0.65:
                continue
            if direction == "incoming":
                # The avatar column is farther left than the bubble column.
                if not max(42, min(84, width * 0.08)) <= left < width * 0.32:
                    continue
                if right > width * 0.90:
                    continue
                interior = neutral[top + 2:bottom - 2, left + 2:right - 2]
                if np.mean(interior > 20) > 0.03:
                    continue
            elif not (right > width * 0.65 and left > width * 0.10):
                continue
            candidates.append(BubbleBox(left, top, right, bottom, direction))
    return sorted(candidates, key=lambda bubble: (bubble.top, bubble.left))


class LocalOcrBackend:
    """Lazy RapidOCR 1.4.4 wrapper using the three bundled ONNX models."""

    def __init__(self, *, min_confidence: float = 0.72):
        if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        self.min_confidence = min_confidence
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                from onnxruntime import disable_telemetry_events
            except ImportError:
                raise OcrError("缺少本地 OCR 依赖，请先重新运行 setup.ps1。") from None
            try:
                disable_telemetry_events()
                self._engine = RapidOCR(
                    det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False,
                    det_use_dml=False, cls_use_dml=False, rec_use_dml=False,
                    intra_op_num_threads=2, inter_op_num_threads=1,
                    # Keep low-score lines visible to our whole-bubble gate;
                    # filtering them upstream would silently truncate messages.
                    print_verbose=False, text_score=0.0,
                    det_limit_type="max", det_limit_side_len=960,
                )
            except Exception:
                raise OcrError("本地 OCR 模型加载失败，请重新安装固定版本 OCR 依赖。") from None
        return self._engine

    def recognize(self, image) -> list[OcrLine]:
        """Recognize an in-memory image; return only finite, confident results."""
        return self._recognize(image, min_confidence=self.min_confidence)

    def _recognize(self, image, *, min_confidence: float) -> list[OcrLine]:
        np = _numpy()
        rgb = _rgb(image)
        # RapidOCR treats ndarray as BGR, unlike its PIL/path image loader.
        bgr = np.ascontiguousarray(rgb[..., ::-1])
        engine = self._get_engine()
        try:
            rows, _ = engine(bgr, use_det=True, use_cls=True, use_rec=True)
        except Exception:
            raise OcrError("本地 OCR 识别失败，已停止此次图片读取。") from None
        result = []
        for row in rows or []:
            try:
                points, text, raw_score = row[:3]
                score = float(raw_score)
                box = tuple((float(point[0]), float(point[1])) for point in points)
                if not isinstance(text, str) or not text.strip() or len(box) != 4:
                    continue
                if not math.isfinite(score) or not min_confidence <= score <= 1:
                    continue
                if not all(math.isfinite(value) for point in box for value in point):
                    continue
                height, width = rgb.shape[:2]
                if any(x < 0 or y < 0 or x > width or y > height for x, y in box):
                    continue
                result.append(OcrLine(text, box, score))
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
        return result

    def recognize_bubbles(self, image) -> tuple[list[OcrLine], list[BubbleBox]]:
        """OCR only bubble interiors; restore boxes to viewport coordinates."""
        rgb = _rgb(image)
        bubbles = locate_bubbles(rgb)
        lines = []
        for bubble in bubbles:
            # Exclude outline/tail pixels while retaining text padding. Nicknames
            # live outside this rectangle and never enter the OCR model.
            left, top = int(bubble.left) + 2, int(bubble.top) + 2
            right, bottom = int(bubble.right) - 2, int(bubble.bottom) - 2
            if right <= left or bottom <= top:
                continue
            recognized = self._recognize(rgb[top:bottom, left:right], min_confidence=0.0)
            if any(line.confidence < self.min_confidence for line in recognized):
                continue
            for line in recognized:
                translated = tuple((x + left, y + top) for x, y in line.box)
                lines.append(OcrLine(line.text, translated, line.confidence))
        return lines, bubbles
