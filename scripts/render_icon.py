#!/usr/bin/env python3
"""Render sandbox-ai's icon (sandbox/web/icon.svg) to PNG files with Qt's SVG renderer (PySide6).

  python3 -I scripts/render_icon.py --pyside DIR --svg sandbox/web/icon.svg --png OUT.png [--size 256]
  python3 -I scripts/render_icon.py --pyside DIR --svg sandbox/web/icon.svg --iconset OUT.iconset

--iconset writes the sizes macOS' iconutil turns into an .icns (icon_16x16.png ... icon_512x512@2x.png).
--pyside names a folder PySide6 was installed into with pip --target (the packages' own), searched first.
"""
import argparse
import os
from pathlib import Path
import sys

ICONSET = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"), (128, "128x128"),
           (256, "128x128@2x"), (256, "256x256"), (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]


def renderer(svg):
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication(["render_icon"])
    svg_renderer = QSvgRenderer(str(svg))
    if not svg_renderer.isValid():
        sys.exit(f"render_icon: {svg} is not a valid SVG")
    return app, svg_renderer


def render(svg_renderer, size, path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    svg_renderer.render(painter)
    painter.end()
    if not image.save(str(path), "PNG"):
        sys.exit(f"render_icon: cannot write {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the icon to PNG")
    parser.add_argument("--pyside", type=Path, help="A folder with PySide6 (pip --target)")
    parser.add_argument("--svg", type=Path, required=True)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument("--iconset", type=Path)
    a = parser.parse_args(argv)
    if a.pyside:
        sys.path.insert(0, str(a.pyside.resolve()))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app, svg_renderer = renderer(a.svg)
    if a.png:
        render(svg_renderer, a.size, a.png)
    if a.iconset:
        a.iconset.mkdir(parents=True, exist_ok=True)
        for size, name in ICONSET:
            render(svg_renderer, size, a.iconset / f"icon_{name}.png")
    if not (a.png or a.iconset):
        sys.exit("render_icon: give --png or --iconset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
