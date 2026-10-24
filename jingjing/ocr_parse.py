"""Pure, conservative parsing of OCR from explicitly located chat bubbles.

No desktop, image, network, or OCR runtime imports belong in this module.
OCR lines alone do not establish message direction: callers must supply the
text-bubble rectangles found by the image backend. Nicknames and centered
timestamps therefore never become messages merely because OCR can read them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import re
import unicodedata
from typing import Iterable


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: tuple[tuple[float, float], ...]
    confidence: float

    @property
    def left(self) -> float:
        return min(point[0] for point in self.box)

    @property
    def top(self) -> float:
        return min(point[1] for point in self.box)

    @property
    def right(self) -> float:
        return max(point[0] for point in self.box)

    @property
    def bottom(self) -> float:
        return max(point[1] for point in self.box)


@dataclass(frozen=True)
class BubbleBox:
    left: float
    top: float
    right: float
    bottom: float
    direction: str


@dataclass(frozen=True)
class ParsedMessage:
    text: str
    direction: str
    box: BubbleBox

    @property
    def incoming(self) -> bool:
        return self.direction == "incoming"


def _valid_bounds(left, top, right, bottom) -> bool:
    return all(math.isfinite(value) for value in (left, top, right, bottom)) and left < right and top < bottom


def _line_bounds(line: OcrLine):
    try:
        if len(line.box) < 4 or not math.isfinite(line.confidence):
            return None
        if not all(len(point) == 2 and all(math.isfinite(value) for value in point) for point in line.box):
            return None
        bounds = (line.left, line.top, line.right, line.bottom)
        return bounds if _valid_bounds(*bounds) else None
    except (TypeError, ValueError):
        return None


def _join_row(lines: list[OcrLine]) -> str:
    """Join separately detected runs while retaining Latin word boundaries."""
    result = ""
    for line in sorted(lines, key=lambda item: item.left):
        value = line.text.strip()
        if result and value and result[-1].isascii() and result[-1].isalnum() and value[0].isascii() and value[0].isalnum():
            result += " "
        result += value
    return result


def _bubble_text(lines: list[OcrLine]) -> str:
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda item: (item.top, item.left)):
        if rows:
            previous = rows[-1]
            row_top = min(item.top for item in previous)
            row_bottom = max(item.bottom for item in previous)
            intersection = min(line.bottom, row_bottom) - max(line.top, row_top)
            if intersection >= min(line.bottom - line.top, row_bottom - row_top) * 0.5:
                previous.append(line)
                continue
        rows.append([line])
    return "\n".join(_join_row(row) for row in rows).strip()


def parse_messages(
    lines: Iterable[OcrLine],
    width: float,
    height: float,
    bubbles: Iterable[BubbleBox] | None = None,
    *,
    min_confidence: float = 0.8,
) -> list[ParsedMessage]:
    """Return ordered complete text bubbles, including outgoing sequence anchors.

    There is deliberately no text-position fallback when ``bubbles`` is absent.
    A line must fit inside exactly one supplied bubble. A low-confidence line
    invalidates its entire bubble rather than silently shortening a question.
    Callers must not supply image/emoji/file tiles as text-bubble rectangles.
    """
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("消息区域宽高必须为有限正数。")
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence 必须介于 0 和 1。")
    boxes = []
    for bubble in bubbles or ():
        if bubble.direction not in ("incoming", "outgoing"):
            continue
        if not _valid_bounds(bubble.left, bubble.top, bubble.right, bubble.bottom):
            continue
        # A clipped first/last message may contain only part of a command.
        if bubble.left < 0 or bubble.top < 0 or bubble.right > width or bubble.bottom > height:
            continue
        # Text on the other side must never become inbound through a bad label.
        if bubble.direction == "incoming" and bubble.left >= width * 0.45:
            continue
        if bubble.direction == "outgoing" and bubble.right <= width * 0.55:
            continue
        boxes.append(bubble)
    assigned: list[list[OcrLine]] = [[] for _ in boxes]
    invalid: set[int] = set()
    tolerance = 2.0
    for line in lines:
        bounds = _line_bounds(line)
        if bounds is None or not line.text.strip():
            continue
        left, top, right, bottom = bounds
        owners = [
            index for index, box in enumerate(boxes)
            if left >= box.left - tolerance and top >= box.top - tolerance
            and right <= box.right + tolerance and bottom <= box.bottom + tolerance
        ]
        if len(owners) != 1:
            # Overlapping candidate boxes are not reliable message boundaries.
            invalid.update(owners)
            if not owners:
                # A run crossing a bubble's edge is incomplete, not an
                # unrelated line that can safely be omitted from its text.
                center_x, center_y = (left + right) / 2, (top + bottom) / 2
                invalid.update(
                    index for index, box in enumerate(boxes)
                    if box.left <= center_x <= box.right and box.top <= center_y <= box.bottom
                )
            continue
        index = owners[0]
        if line.confidence < min_confidence or line.confidence > 1:
            invalid.add(index)
        else:
            assigned[index].append(line)
    result = []
    for index, box in enumerate(boxes):
        if index in invalid or not assigned[index]:
            continue
        text = _bubble_text(assigned[index])
        if text:
            result.append(ParsedMessage(text, box.direction, box))
    return sorted(result, key=lambda message: (message.box.top, message.box.left))


def _key(message: ParsedMessage) -> tuple[str, str]:
    # Ignore wrapping/spacing noise, while preserving actual text and direction.
    text = unicodedata.normalize("NFKC", message.text)
    return message.direction, re.sub(r"\s+", " ", text).strip()


def _is_subsequence(needles, haystack) -> bool:
    remaining = iter(haystack)
    return all(any(item == needle for item in remaining) for needle in needles)


class SnapshotTracker:
    """Confirm stable screenshots and identify appended message occurrences.

    Start-up establishes a baseline and never replies to visible history. Both
    directions remain in the ordered sequence as anchors, but ``update`` emits
    incoming bubbles only. Repeated texts are individual sequence occurrences.

    A stable snapshot with no common sequence anchor becomes a fresh baseline,
    without emitting its contents. Temporary empty/incomplete OCR snapshots do
    not erase a known baseline. A viewport filled with identical messages at
    identical positions cannot reveal whether one scrolled out and another
    arrived; that visually indistinguishable case is intentionally suppressed.
    """

    def __init__(self, *, stable_frames: int = 2, position_tolerance: float = 3.0):
        if stable_frames < 2:
            raise ValueError("OCR 至少需要连续两帧确认。")
        if not math.isfinite(position_tolerance) or position_tolerance < 0:
            raise ValueError("位置容差必须为有限非负数。")
        self.stable_frames = stable_frames
        self.position_tolerance = position_tolerance
        self.reset()

    def reset(self, messages: Iterable[ParsedMessage] | None = None):
        self._committed = None if messages is None else tuple(messages)
        self._candidate = None
        self._count = 0
        self._known = deque(maxlen=128)
        if self._committed:
            self._remember(self._committed)
        self.rebaselined = False

    def baseline(self, messages: Iterable[ParsedMessage]):
        self.reset(messages)

    @property
    def ready(self) -> bool:
        return self._committed is not None

    def _same_frame(self, left, right) -> bool:
        if len(left) != len(right):
            return False
        for first, second in zip(left, right):
            if _key(first) != _key(second):
                return False
            if max(abs(getattr(first.box, name) - getattr(second.box, name)) for name in ("left", "top", "right", "bottom")) > self.position_tolerance:
                return False
        return True

    def _overlap(self, old, current) -> int:
        old_keys = tuple(map(_key, old))
        current_keys = tuple(map(_key, current))
        for count in range(min(len(old), len(current)), 0, -1):
            if old_keys[-count:] != current_keys[:count]:
                continue
            pairs = tuple(zip(old[-count:], current[:count]))
            shifts = [second.box.top - first.box.top for first, second in pairs]
            # Appending can move history upward, never downward. A downward
            # movement usually means the user scrolled into older history.
            if max(shifts) > self.position_tolerance or max(shifts) - min(shifts) > self.position_tolerance * 2:
                continue
            if any(abs(first.box.left - second.box.left) > self.position_tolerance for first, second in pairs):
                continue
            return count
        return 0

    def _remember(self, messages):
        keys = tuple(map(_key, messages))
        if keys and (not self._known or self._known[-1] != keys):
            self._known.append(keys)

    def update(self, messages: Iterable[ParsedMessage]) -> list[ParsedMessage]:
        current = tuple(messages)
        self.rebaselined = False
        if self._candidate is not None and self._same_frame(self._candidate, current):
            self._count += 1
        else:
            self._candidate = current
            self._count = 1
        if self._count < self.stable_frames:
            return []
        if self._committed is None:
            self._committed = current
            self._remember(current)
            return []
        old = self._committed
        if not current and old:
            return []
        old_keys = tuple(map(_key, old))
        current_keys = tuple(map(_key, current))
        if current_keys == old_keys:
            self._committed = current
            return []
        # A disappearing line/bubble must not replay when OCR recovers it.
        if current and _is_subsequence(current_keys, old_keys):
            return []
        # Returning from another page/history must not replay a known message
        # run. Retain a bounded set of previous ordered snapshots, rather than a
        # text set that would suppress legitimate repeated new occurrences.
        if current and any(_is_subsequence(current_keys, known) for known in self._known):
            self._committed = current
            return []
        if not old:
            new = current
        else:
            count = self._overlap(old, current)
            if not count:
                self._committed = current
                self._remember(current)
                self.rebaselined = True
                return []
            new = current[count:]
        self._committed = current
        self._remember(current)
        return [message for message in new if message.incoming]
