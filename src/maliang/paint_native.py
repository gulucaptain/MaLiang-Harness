"""ctypes adapter for the stable libmypaint 1.6 ABI. Run rendering in a worker.

Only public functions and the public MyPaintTileRequest layout are used. Tile
pixels are 15-bit premultiplied RGBA, not ordinary 16-bit straight-alpha RGBA.
"""

from __future__ import annotations

import colorsys
import ctypes as C
import ctypes.util
import math
import os

from PIL import Image

from .paint import PRESETS

INSTALL_HELP = (
    "缺少兼容的 libmypaint 1.6。macOS: brew install libmypaint；"
    "Debian/Ubuntu: apt install libmypaint-1.5-1。"
    "自定义安装可设置 MALIANG_MYPAINT_LIBRARY 为共享库绝对路径。"
)


class TileRequest(C.Structure):
    _fields_ = [
        ("tx", C.c_int),
        ("ty", C.c_int),
        ("readonly", C.c_int),
        ("buffer", C.POINTER(C.c_uint16)),
        ("context", C.c_void_p),
        ("thread_id", C.c_int),
        ("mipmap_level", C.c_int),
    ]


def require_library():
    override = os.environ.get("MALIANG_MYPAINT_LIBRARY")
    candidates = (
        [override]
        if override
        else [
            "/opt/homebrew/opt/libmypaint/lib/libmypaint.0.dylib",
            "/usr/local/opt/libmypaint/lib/libmypaint.0.dylib",
            ctypes.util.find_library("mypaint-1.5"),
            ctypes.util.find_library("mypaint"),
            "libmypaint-1.5.so.1",
        ]
    )
    for path in candidates:
        if not path:
            continue
        try:
            lib = C.CDLL(path)
            # This symbol belongs to the 1.6 API. Do not call the incompatible
            # development 2.x stroke_to ABI accidentally.
            getattr(lib, "mypaint_brush_stroke_to_2")
            _bind(lib)
            return lib
        except (OSError, AttributeError):
            continue
    raise ValueError(INSTALL_HELP)


def native_status():
    try:
        lib = require_library()
        return {"available": True, "engine": "libmypaint-1.6", "library": lib._name}
    except ValueError as exc:
        return {"available": False, "engine": "libmypaint-1.6", "message": str(exc)}


def _bind(lib):
    p, f, i = C.c_void_p, C.c_float, C.c_int
    signatures = {
        "mypaint_fixed_tiled_surface_new": (p, [i, i]),
        "mypaint_fixed_tiled_surface_interface": (p, [p]),
        "mypaint_surface_unref": (None, [p]),
        "mypaint_surface_begin_atomic": (None, [p]),
        "mypaint_surface_end_atomic": (None, [p, p]),
        "mypaint_tile_request_init": (None, [C.POINTER(TileRequest), i, i, i, i]),
        "mypaint_tiled_surface_tile_request_start": (None, [p, C.POINTER(TileRequest)]),
        "mypaint_tiled_surface_tile_request_end": (None, [p, C.POINTER(TileRequest)]),
        "mypaint_brush_new": (p, []),
        "mypaint_brush_unref": (None, [p]),
        "mypaint_brush_from_defaults": (None, [p]),
        "mypaint_brush_new_stroke": (None, [p]),
        "mypaint_brush_setting_from_cname": (i, [C.c_char_p]),
        "mypaint_brush_set_base_value": (None, [p, i, f]),
        "mypaint_brush_stroke_to": (i, [p, p, f, f, f, f, f, C.c_double]),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args


class NativeSurface:
    TILE = 64  # libmypaint 1.6 MYPAINT_TILE_SIZE

    def __init__(self, lib, width, height):
        self.lib, self.width, self.height = lib, width, height
        self.ptr = lib.mypaint_fixed_tiled_surface_new(width, height)
        if not self.ptr:
            raise MemoryError("Cannot allocate paint surface")
        self.surface = lib.mypaint_fixed_tiled_surface_interface(self.ptr)
        # The testing surface defaults to 0xffff; initialize real transparent
        # paint storage explicitly before drawing or taking any smudge samples.
        for ty in range((height + 63) // 64):
            for tx in range((width + 63) // 64):
                req = self.tile(tx, ty, False)
                C.memset(req.buffer, 0, 64 * 64 * 4 * 2)
                self.release(req)

    def tile(self, tx, ty, readonly):
        req = TileRequest()
        self.lib.mypaint_tile_request_init(C.byref(req), 0, tx, ty, int(readonly))
        self.lib.mypaint_tiled_surface_tile_request_start(self.ptr, C.byref(req))
        return req

    def release(self, req):
        self.lib.mypaint_tiled_surface_tile_request_end(self.ptr, C.byref(req))

    def close(self):
        if self.ptr:
            self.lib.mypaint_surface_unref(self.surface)
            self.ptr = None

    def stroke(self, stroke):
        lib = self.lib
        brush = lib.mypaint_brush_new()
        if not brush:
            raise MemoryError("Cannot allocate brush")
        try:
            lib.mypaint_brush_from_defaults(brush)
            rgb = tuple(int(stroke.color[n : n + 2], 16) / 255 for n in (1, 3, 5))
            h, s, v = colorsys.rgb_to_hsv(*rgb)
            values = {
                "color_h": h,
                "color_s": s,
                "color_v": v,
                "radius_logarithmic": math.log(stroke.size / 2),
                "opaque": stroke.opacity,
                "hardness": PRESETS[stroke.brush]["hardness"],
                "dabs_per_actual_radius": 3,
                "dabs_per_basic_radius": 0,
                "radius_by_random": 0,
                "offset_by_random": 0,
                "smudge": 0.95 if stroke.brush == "smudge" else 0,
                "smudge_length": 0.5,
                "eraser": 1 if stroke.brush == "eraser" else 0,
            }
            for name, value in values.items():
                setting = lib.mypaint_brush_setting_from_cname(name.encode())
                if setting < 0:
                    raise ValueError(f"Unsupported libmypaint setting: {name}")
                lib.mypaint_brush_set_base_value(brush, setting, value)
            lib.mypaint_brush_new_stroke(brush)
            first = stroke.points[0]
            lib.mypaint_surface_begin_atomic(self.surface)
            try:
                lib.mypaint_brush_stroke_to(brush, self.surface, first.x, first.y, 0, 0, 0, 1)
                for point in stroke.points:
                    lib.mypaint_brush_stroke_to(
                        brush,
                        self.surface,
                        point.x,
                        point.y,
                        point.pressure,
                        point.xtilt,
                        point.ytilt,
                        point.dt,
                    )
            finally:
                roi = (C.c_int * 4)()
                lib.mypaint_surface_end_atomic(self.surface, roi)
        finally:
            lib.mypaint_brush_unref(brush)

    def image(self):
        image = Image.new("RGBA", (self.width, self.height))
        for ty in range((self.height + 63) // 64):
            for tx in range((self.width + 63) // 64):
                req = self.tile(tx, ty, True)
                try:
                    data = bytearray(64 * 64 * 4)
                    for offset in range(0, len(data), 4):
                        a = min(32768, req.buffer[offset + 3])
                        if a:
                            for c in range(3):
                                data[offset + c] = min(255, (req.buffer[offset + c] * 255 + a // 2) // a)
                            data[offset + 3] = (a * 255 + 16384) // 32768
                    image.paste(Image.frombytes("RGBA", (64, 64), bytes(data)), (tx * 64, ty * 64))
                finally:
                    self.release(req)
        return image


def render_document(document, width, height):
    lib = require_library()
    result = Image.new("RGBA", (width, height), document.background)
    for layer in document.layers:
        if not layer.visible or not layer.opacity or not layer.strokes:
            continue
        surface = NativeSurface(lib, width, height)
        try:
            for stroke in layer.strokes:
                surface.stroke(stroke)
            painted = surface.image()
        finally:
            surface.close()
        if layer.opacity != 1:
            painted.putalpha(painted.getchannel("A").point(lambda a: round(a * layer.opacity)))
        result = Image.alpha_composite(result, painted)
    return result
