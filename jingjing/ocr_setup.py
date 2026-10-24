"""Local screenshot region picker; no message text is saved or transmitted."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .config import ConfigError
from .ocr_wechat import is_input_empty, validate_calibration


def save_calibrations(root: Path, chats: dict):
    """Atomically replace the local, ignored calibration file."""
    for entry in chats.values():
        validate_calibration(entry)
    folder = Path(root) / "data"
    folder.mkdir(exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"version": 1, "chats": chats}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / "ocr-calibration.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _pick(image, name):
    # This window is shown only when the operator runs ocr-setup. Captures remain
    # in memory; the saved result contains coordinates and one background color.
    import tkinter as tk
    from PIL import ImageTk

    window = tk.Tk()
    window.title(f"小鲸鱼 · 校准 {name}")
    scale = min(1.0, (window.winfo_screenwidth() - 80) / image.width,
                (window.winfo_screenheight() - 180) / image.height)
    shown = image.resize((round(image.width * scale), round(image.height * scale)))
    label = tk.Label(window, text="第一步：拖框选择消息区域（排除顶部标题和底部输入框）", font=("Microsoft YaHei", 12))
    label.pack(padx=12, pady=10)
    photo = ImageTk.PhotoImage(shown)
    canvas = tk.Canvas(window, width=shown.width, height=shown.height, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)
    result = []
    anchor = []
    rectangle = []

    def start(event):
        anchor[:] = [event.x, event.y]
        if rectangle:
            canvas.delete(rectangle.pop())
        rectangle.append(canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#1677ff", width=2))

    def move(event):
        if anchor and rectangle:
            canvas.coords(rectangle[0], anchor[0], anchor[1], event.x, event.y)

    def end(event):
        if not anchor:
            return
        x0, y0 = anchor
        anchor.clear()
        box = [round(min(x0, event.x) / scale), round(min(y0, event.y) / scale),
               round(max(x0, event.x) / scale), round(max(y0, event.y) / scale)]
        if box[2] - box[0] < 24 or box[3] - box[1] < 24:
            label.config(text="区域太小，请重新拖框。")
            return
        result.append(box)
        if len(result) == 1:
            label.config(text="第二步：选择空白文字输入区内部（排除边框、工具栏和发送按钮）")
            canvas.itemconfig(rectangle[0], outline="#38a169")
            rectangle.clear()
        else:
            window.destroy()

    canvas.bind("<ButtonPress-1>", start)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", end)
    window.mainloop()
    if len(result) != 2:
        raise ConfigError("已取消校准，原有校准文件没有修改。")
    return result


def setup(config, root: Path, chat_name: str | None = None):
    from .ocr_desktop import WindowsDesktop
    from PIL import ImageStat

    names = (*config.friends, *config.groups)
    if chat_name is not None:
        if chat_name not in names:
            raise ConfigError("请先把该完整备注或群名加入 config.json。")
        names = (chat_name,)
    if not names:
        raise ConfigError("请先配置好友备注或群名。")
    path = Path(root) / "data" / "ocr-calibration.json"
    chats = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8-sig"))
            if old.get("version") != 1 or not isinstance(old.get("chats"), dict):
                raise ValueError
            chats = old["chats"]
        except (OSError, UnicodeError, ValueError, AttributeError):
            raise ConfigError("原校准文件无法读取，请先保留原文件并检查格式。") from None
    desktop = WindowsDesktop()
    for name in names:
        native = desktop.find(name)
        desktop.activate(native)
        image = desktop.capture(native)
        messages, composer = _pick(image, name)
        color = tuple(round(v) for v in ImageStat.Stat(image.crop(composer)).median)
        entry = {"size": list(native.size), "messages": messages, "input": composer, "empty_rgb": list(color)}
        validate_calibration(entry)
        if not is_input_empty(image.crop(composer), color):
            raise ConfigError("所选输入区包含草稿、边框或工具栏，请清空草稿并重新校准。原文件没有修改。")
        chats[name] = entry
    save_calibrations(root, chats)
    print("校准已保存到本地 data/ocr-calibration.json。接下来运行 ocr-probe。")
    return 0
