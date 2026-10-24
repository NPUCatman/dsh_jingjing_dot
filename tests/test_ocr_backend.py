"""Local OCR checks with mocked inference and synthetic message viewports."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError:
    np = Image = ImageDraw = None

from jingjing.ocr_backend import LocalOcrBackend, OcrError, locate_bubbles


def row(text="你好", score=0.95, box=None):
    return [box or [[3, 4], [20, 4], [20, 18], [3, 18]], text, score]


@unittest.skipIf(np is None or Image is None, "optional image dependencies not installed")
class OcrEngineTests(unittest.TestCase):
    def backend(self, rows=None):
        backend = LocalOcrBackend()
        backend._engine = Mock(return_value=(rows, [0.01, 0.01, 0.01]))
        return backend

    def test_initialization_is_lazy_cpu_only_and_cached(self):
        factory = Mock(return_value=Mock(return_value=(None, None)))
        module = SimpleNamespace(RapidOCR=factory)
        disable_telemetry = Mock()
        with patch.dict(sys.modules, {
            "rapidocr_onnxruntime": module,
            "onnxruntime": SimpleNamespace(disable_telemetry_events=disable_telemetry),
        }):
            backend = LocalOcrBackend()
            factory.assert_not_called()
            backend.recognize(Image.new("RGB", (30, 30)))
            backend.recognize(Image.new("RGB", (30, 30)))
        factory.assert_called_once()
        disable_telemetry.assert_called_once()
        options = factory.call_args.kwargs
        for name in ("det", "cls", "rec"):
            self.assertIs(options[f"{name}_use_cuda"], False)
            self.assertIs(options[f"{name}_use_dml"], False)
        self.assertFalse(options["print_verbose"])
        self.assertEqual(options["text_score"], 0.0)

    def test_pil_and_numpy_are_rgb_then_converted_to_contiguous_bgr(self):
        for image in (Image.new("RGB", (30, 30), (11, 22, 33)),
                      np.full((30, 30, 3), (11, 22, 33), dtype=np.uint8)):
            backend = self.backend([row()])
            lines = backend.recognize(image)
            pixels = backend._engine.call_args.args[0]
            self.assertEqual(pixels[0, 0].tolist(), [33, 22, 11])
            self.assertTrue(pixels.flags.c_contiguous)
            self.assertEqual(lines[0].text, "你好")
            self.assertEqual(lines[0].box[0], (3.0, 4.0))

    def test_alpha_is_composited_over_white_and_grayscale_works(self):
        for image in (Image.new("RGBA", (30, 30), (0, 0, 0, 0)),
                      np.zeros((30, 30, 4), dtype=np.uint8)):
            backend = self.backend()
            backend.recognize(image)
            self.assertEqual(backend._engine.call_args.args[0][0, 0].tolist(), [255] * 3)
        backend = self.backend()
        backend.recognize(np.full((30, 30), 42, dtype=np.uint8))
        self.assertEqual(backend._engine.call_args.args[0][0, 0].tolist(), [42] * 3)

    def test_paths_urls_and_encoded_images_are_rejected_before_engine(self):
        for image in ("https://example.invalid/a.png", "C:/chat.png", b"image", object()):
            backend = self.backend()
            with self.assertRaises(OcrError):
                backend.recognize(image)
            backend._engine.assert_not_called()

    def test_invalid_array_format_is_rejected(self):
        for image in (np.zeros((3, 3), dtype=float), np.zeros((3,), dtype=np.uint8),
                      np.zeros((3, 3, 2), dtype=np.uint8), np.zeros((0, 3, 3), dtype=np.uint8)):
            with self.assertRaises(OcrError):
                self.backend().recognize(image)

    def test_low_confidence_malformed_and_nonfinite_results_are_filtered(self):
        backend = self.backend([
            row("保留", 0.72), row("低分", 0.71), row("无穷", float("inf")),
            row("空分", float("nan")), row("越界", 0.9, [[-1, 0]] * 4),
            row("坏框", 0.9, [[float("nan"), 0]] * 4), row("高分", 2),
            row("", 0.99), [[], "无框", 0.99], None,
        ])
        lines = backend.recognize(Image.new("RGB", (30, 30)))
        self.assertEqual([line.text for line in lines], ["保留"])

    def test_empty_result_is_empty(self):
        self.assertEqual(self.backend().recognize(Image.new("RGB", (30, 30))), [])

    def test_inference_error_does_not_disclose_underlying_content(self):
        backend = self.backend()
        backend._engine.side_effect = RuntimeError("private recognized text")
        with self.assertRaises(OcrError) as caught:
            backend.recognize(Image.new("RGB", (30, 30)))
        self.assertNotIn("private", str(caught.exception))

    def test_confidence_validation(self):
        for confidence in (-1, 1.1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                LocalOcrBackend(min_confidence=confidence)


@unittest.skipIf(np is None or Image is None or importlib.util.find_spec("cv2") is None,
                 "optional OCR image dependencies not installed")
class BubbleSegmentationTests(unittest.TestCase):
    @staticmethod
    def viewport():
        return Image.new("RGB", (600, 400), (250, 250, 250))

    def test_incoming_outgoing_and_multiline_bubbles_with_avatar_exclusion(self):
        image = self.viewport()
        draw = ImageDraw.Draw(image)
        draw.rectangle((18, 50, 50, 83), fill="white")  # avatar
        draw.rounded_rectangle((70, 50, 220, 101), 6, fill=(237, 237, 237))
        draw.rectangle((80, 67, 150, 72), fill="black")  # simulated text
        draw.rounded_rectangle((390, 140, 540, 221), 6, fill=(149, 236, 105))
        draw.rectangle((403, 160, 470, 165), fill="black")
        draw.rectangle((403, 184, 500, 189), fill="black")
        boxes = locate_bubbles(image)
        self.assertEqual([box.direction for box in boxes], ["incoming", "outgoing"])
        self.assertEqual((boxes[0].left, boxes[0].top, boxes[0].right, boxes[0].bottom),
                         (70, 50, 221, 102))

    def test_background_nicknames_edges_and_wrong_side_are_not_bubbles(self):
        image = self.viewport()
        draw = ImageDraw.Draw(image)
        draw.text((70, 25), "group nickname", fill="black")
        draw.rectangle((70, 0, 220, 40), fill=(237, 237, 237))  # clipped history
        draw.rectangle((390, 140, 540, 190), fill=(237, 237, 237))  # wrong-side gray
        draw.rectangle((20, 240, 150, 290), fill=(149, 236, 105))  # wrong side
        self.assertEqual(locate_bubbles(image), [])
        self.assertEqual(locate_bubbles(Image.new("RGB", (600, 400), "white")), [])

    def test_colored_image_card_is_excluded(self):
        image = self.viewport()
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 50, 250, 180), fill=(237, 237, 237))
        draw.rectangle((90, 70, 230, 160), fill="red")
        self.assertEqual(locate_bubbles(image), [])

    def test_separate_bubbles_remain_separate(self):
        image = self.viewport()
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 50, 150, 85), fill=(237, 237, 237))
        draw.rectangle((70, 96, 150, 131), fill=(237, 237, 237))
        self.assertEqual(len(locate_bubbles(image)), 2)

    def test_bubble_ocr_excludes_nickname_and_restores_coordinates(self):
        image = self.viewport()
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 50, 220, 101), fill=(237, 237, 237))
        draw.rectangle((70, 25, 200, 35), fill="black")  # nickname
        backend = LocalOcrBackend()
        backend._engine = Mock(return_value=([row()], None))
        lines, bubbles = backend.recognize_bubbles(image)
        self.assertEqual(len(bubbles), 1)
        pixels = backend._engine.call_args.args[0]
        self.assertEqual(pixels.shape, (48, 147, 3))
        self.assertTrue(np.all(pixels == 237))  # outside nickname was not passed
        self.assertEqual(lines[0].box[0], (75.0, 56.0))

    def test_new_theme_white_image_and_gray_border_image_are_excluded(self):
        image = self.viewport()
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 50, 220, 150), fill="white")
        draw.rectangle((70, 180, 220, 280), fill=(237, 237, 237))
        draw.rectangle((72, 182, 218, 278), fill="white")
        self.assertEqual(locate_bubbles(image), [])

    def test_older_theme_accepts_white_bubbles_on_gray_backdrop(self):
        image = Image.new("RGB", (600, 400), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((70, 50, 220, 101), 6, fill="white")
        boxes = locate_bubbles(image)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].direction, "incoming")

    def test_no_bubbles_does_not_load_or_invoke_ocr(self):
        backend = LocalOcrBackend()
        with patch.object(backend, "_get_engine") as factory:
            self.assertEqual(backend.recognize_bubbles(self.viewport()), ([], []))
        factory.assert_not_called()

    def test_low_confidence_line_discards_the_entire_bubble(self):
        image = self.viewport()
        ImageDraw.Draw(image).rectangle((70, 50, 220, 101), fill=(237, 237, 237))
        backend = LocalOcrBackend()
        backend._engine = Mock(return_value=([row("clear", 0.99), row("unclear", 0.6)], None))
        lines, bubbles = backend.recognize_bubbles(image)
        self.assertEqual(len(bubbles), 1)
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
