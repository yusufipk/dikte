#!/usr/bin/env python3
"""Render the SVG app icon into the iconset layout expected by iconutil."""

import pathlib
import sys

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer


def render(svg_path, output_dir):
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise SystemExit(f"Invalid SVG: {svg_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    for points in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = points * scale
            image = QImage(
                pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied
            )
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter, QRectF(0, 0, pixels, pixels))
            painter.end()
            suffix = "@2x" if scale == 2 else ""
            destination = output_dir / f"icon_{points}x{points}{suffix}.png"
            if not image.save(str(destination)):
                raise SystemExit(f"Could not write {destination}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: render_macos_icon.py SOURCE.svg OUTPUT.iconset")
    render(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
