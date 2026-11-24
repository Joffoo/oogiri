import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

# IPPON GP 经典高反差配色（一主一辅，克制纯净）
COLOR_YELLOW = (255, 217, 0)       # #FFD900 IPPON 明黄
COLOR_YELLOW_DARK = (244, 202, 0)  # #F4CA00 极细微斜纹底纹
COLOR_BLACK = (18, 18, 18)         # #121212 炭黑主墨
COLOR_WHITE = (255, 255, 255)      # #FFFFFF 题板白
COLOR_MODEL_TEXT = (128, 128, 128) # 回答区右下角模型署名灰

FONT_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/Dengb.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

FONT_REG_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

RATIOS = {
    "3:4": (1080, 1440),
    "16:9": (1600, 900),
    "1:1": (1200, 1200),
}

NO_START_CHARS = set("，。！？、；：”’）】》.!?,;:)]}")


def load_font(size: int, regular: bool = False) -> ImageFont.FreeTypeFont:
    candidates = FONT_REG_CANDIDATES if regular else FONT_BOLD_CANDIDATES
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按像素宽度自动换行，兼顾中文连续标点避头尾。"""
    lines: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            test = current + ch
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] <= max_width or not current:
                current = test
            elif ch in NO_START_CHARS:
                if bbox[2] - bbox[0] <= max_width + font.size * 0.65:
                    current = test
                else:
                    split_idx = len(current) - 1
                    while split_idx > 0 and current[split_idx] in NO_START_CHARS:
                        split_idx -= 1
                    if split_idx > 0:
                        lines.append(current[:split_idx])
                        current = current[split_idx:] + ch
                    else:
                        lines.append(current)
                        current = ch
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
    line_spacing_ratio: float = 1.35,
) -> tuple[ImageFont.FreeTypeFont, list[str], int, int]:
    """自动寻找能完整放进目标矩形的最大字号。"""
    size = start_size
    while size >= min_size:
        font = load_font(size)
        lines = wrap_text(draw, text, font, max_width)
        line_h = int(size * line_spacing_ratio)
        total_h = len(lines) * line_h - int(size * (line_spacing_ratio - 1.0))
        max_line_w = max((draw.textbbox((0, 0), ln, font=font)[2] for ln in lines), default=0)
        if total_h <= max_height and max_line_w <= max_width:
            return font, lines, line_h, total_h
        size -= 2
    font = load_font(min_size)
    lines = wrap_text(draw, text, font, max_width)
    line_h = int(min_size * line_spacing_ratio)
    total_h = len(lines) * line_h
    return font, lines, line_h, total_h


def draw_striped_background(img: Image.Image) -> None:
    """绘制 IPPON 风格明黄斜纹底纹。"""
    w, h = img.size
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, h], fill=COLOR_YELLOW)
    stripe_w = max(24, w // 36)
    step = stripe_w * 2
    for offset in range(-h, w + h, step):
        pts = [
            (offset, 0),
            (offset + stripe_w, 0),
            (offset + stripe_w - h, h),
            (offset - h, h),
        ]
        draw.polygon(pts, fill=COLOR_YELLOW_DARK)


def draw_board_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int],
    border: int = 5,
    shadow_offset: int = 8,
    radius: int = 8,
) -> None:
    """绘制克制硬框题板（微圆角、干净硬偏移投影）。"""
    x1, y1, x2, y2 = box
    if shadow_offset > 0:
        draw.rounded_rectangle(
            [x1 + shadow_offset, y1 + shadow_offset, x2 + shadow_offset, y2 + shadow_offset],
            radius=radius,
            fill=COLOR_BLACK,
        )
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill, outline=COLOR_BLACK, width=border)


# ==============================================================================
# 零依赖轻量 SVG 简笔画渲染器（支持 line / rect / circle / ellipse / polyline / polygon / path）
# ==============================================================================

def _parse_color(val: str | None, default: tuple[int, int, int] | None) -> tuple[int, int, int] | None:
    if not val:
        return default
    v = val.strip().lower()
    if v in ("none", "transparent"):
        return None
    named = {
        "black": (18, 18, 18),
        "white": (255, 255, 255),
        "red": (228, 30, 43),
        "yellow": (255, 217, 0),
        "gray": (128, 128, 128),
        "grey": (128, 128, 128),
    }
    if v in named:
        return named[v]
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return default


def _parse_points(points_str: str) -> list[tuple[float, float]]:
    nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", points_str)]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _parse_svg_path(d: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """将 SVG path d 字符串解析为多段折线 [(points, closed), ...]。"""
    tokens = re.findall(r"[A-Za-z]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", d)
    subpaths: list[tuple[list[tuple[float, float]], bool]] = []
    pts: list[tuple[float, float]] = []
    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0
    i = 0
    cmd = ""

    def next_f() -> float:
        nonlocal i
        val = float(tokens[i])
        i += 1
        return val

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        if cmd in ("M", "m"):
            if pts:
                subpaths.append((pts, False))
                pts = []
            x, y = next_f(), next_f()
            if cmd == "m":
                x, y = cx + x, cy + y
            cx, cy = x, y
            sx, sy = x, y
            pts.append((cx, cy))
            cmd = "L" if cmd == "M" else "l"
        elif cmd in ("L", "l"):
            x, y = next_f(), next_f()
            if cmd == "l":
                x, y = cx + x, cy + y
            cx, cy = x, y
            pts.append((cx, cy))
        elif cmd in ("H", "h"):
            x = next_f()
            cx = x if cmd == "H" else cx + x
            pts.append((cx, cy))
        elif cmd in ("V", "v"):
            y = next_f()
            cy = y if cmd == "V" else cy + y
            pts.append((cx, cy))
        elif cmd in ("Q", "q"):
            x1, y1, x2, y2 = next_f(), next_f(), next_f(), next_f()
            if cmd == "q":
                x1, y1, x2, y2 = cx + x1, cy + y1, cx + x2, cy + y2
            for step in range(1, 13):
                t = step / 12.0
                px = (1 - t) ** 2 * cx + 2 * (1 - t) * t * x1 + t ** 2 * x2
                py = (1 - t) ** 2 * cy + 2 * (1 - t) * t * y1 + t ** 2 * y2
                pts.append((px, py))
            cx, cy = x2, y2
        elif cmd in ("C", "c"):
            x1, y1, x2, y2, x3, y3 = next_f(), next_f(), next_f(), next_f(), next_f(), next_f()
            if cmd == "c":
                x1, y1, x2, y2, x3, y3 = cx + x1, cy + y1, cx + x2, cy + y2, cx + x3, cy + y3
            for step in range(1, 17):
                t = step / 16.0
                px = (1 - t) ** 3 * cx + 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t ** 2 * x2 + t ** 3 * x3
                py = (1 - t) ** 3 * cy + 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t ** 2 * y2 + t ** 3 * y3
                pts.append((px, py))
            cx, cy = x3, y3
        elif cmd in ("Z", "z"):
            if pts:
                pts.append((sx, sy))
                subpaths.append((pts, True))
                pts = []
            cx, cy = sx, sy
            cmd = ""
        else:
            i += 1

    if pts:
        subpaths.append((pts, False))
    return subpaths


def render_raster_sketch(base_img: Image.Image, img_path: str, box: tuple[int, int, int, int]) -> None:
    """将纯白底黑色线条位图去白底后等比居中贴入目标矩形。"""
    sketch = Image.open(img_path).convert("RGBA")
    # 将近白色背景转为透明，保留黑色/深色线条
    pixels = sketch.getdata()
    new_pixels = []
    for r, g, b, a in pixels:
        lum = (r * 299 + g * 587 + b * 114) / 1000.0
        if lum > 225:
            new_pixels.append((255, 255, 255, 0))
        else:
            # 压纯黑线条，保持清晰度
            alpha = int(min(255, max(0, (235 - lum) * 1.4)))
            new_pixels.append((18, 18, 18, alpha))
    sketch.putdata(new_pixels)

    # 裁剪透明边缘，让主体居中更紧凑
    bbox = sketch.getbbox()
    if bbox:
        sketch = sketch.crop(bbox)

    bx1, by1, bx2, by2 = box
    bw, bh = max(1, bx2 - bx1), max(1, by2 - by1)
    sw, sh = sketch.size
    scale = min(bw / sw, bh / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    resized = sketch.resize((nw, nh), Image.Resampling.LANCZOS)
    ox = bx1 + (bw - nw) // 2
    oy = by1 + (bh - nh) // 2
    base_img.paste(resized, (ox, oy), resized)


def render_svg_sketch(draw: ImageDraw.ImageDraw, sketch_input: str, box: tuple[int, int, int, int]) -> None:
    """将极简 SVG 字符串或 .svg 文件等比居中绘制到目标矩形内。"""
    svg_text = sketch_input.strip()
    if os.path.exists(svg_text):
        with open(svg_text, "r", encoding="utf-8") as f:
            svg_text = f.read()

    root = ET.fromstring(svg_text)
    vb = root.attrib.get("viewBox", "0 0 200 200")
    vb_nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", vb)]
    vx, vy, vw, vh = (vb_nums + [0, 0, 200, 200])[:4]
    if len(vb_nums) >= 4:
        vx, vy, vw, vh = vb_nums[:4]

    bx1, by1, bx2, by2 = box
    bw, bh = bx2 - bx1, by2 - by1
    s = min(bw / max(vw, 1.0), bh / max(vh, 1.0))
    ox = bx1 + (bw - vw * s) / 2.0 - vx * s
    oy = by1 + (bh - vh * s) / 2.0 - vy * s

    def tx(x: float, y: float) -> tuple[float, float]:
        return (ox + x * s, oy + y * s)

    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag == "svg" or tag == "g":
            continue
        stroke = _parse_color(elem.attrib.get("stroke"), COLOR_BLACK)
        fill = _parse_color(elem.attrib.get("fill"), None)
        sw = max(2, int(round(float(elem.attrib.get("stroke-width", "5")) * s)))

        if tag == "line":
            p1 = tx(float(elem.attrib.get("x1", 0)), float(elem.attrib.get("y1", 0)))
            p2 = tx(float(elem.attrib.get("x2", 0)), float(elem.attrib.get("y2", 0)))
            if stroke:
                draw.line([p1, p2], fill=stroke, width=sw)
        elif tag == "circle":
            cx, cy = float(elem.attrib.get("cx", 0)), float(elem.attrib.get("cy", 0))
            r = float(elem.attrib.get("r", 0))
            p1, p2 = tx(cx - r, cy - r), tx(cx + r, cy + r)
            draw.ellipse([p1, p2], fill=fill, outline=stroke, width=sw if stroke else 0)
        elif tag == "ellipse":
            cx, cy = float(elem.attrib.get("cx", 0)), float(elem.attrib.get("cy", 0))
            rx, ry = float(elem.attrib.get("rx", 0)), float(elem.attrib.get("ry", 0))
            p1, p2 = tx(cx - rx, cy - ry), tx(cx + rx, cy + ry)
            draw.ellipse([p1, p2], fill=fill, outline=stroke, width=sw if stroke else 0)
        elif tag == "rect":
            x, y = float(elem.attrib.get("x", 0)), float(elem.attrib.get("y", 0))
            rw, rh = float(elem.attrib.get("width", 0)), float(elem.attrib.get("height", 0))
            rx = int(float(elem.attrib.get("rx", 0)) * s)
            p1, p2 = tx(x, y), tx(x + rw, y + rh)
            if rx > 0:
                draw.rounded_rectangle([p1, p2], radius=rx, fill=fill, outline=stroke, width=sw if stroke else 0)
            else:
                draw.rectangle([p1, p2], fill=fill, outline=stroke, width=sw if stroke else 0)
        elif tag in ("polyline", "polygon"):
            raw_pts = _parse_points(elem.attrib.get("points", ""))
            mapped = [tx(px, py) for px, py in raw_pts]
            if len(mapped) >= 2:
                if tag == "polygon":
                    draw.polygon(mapped, fill=fill, outline=stroke)
                    if stroke:
                        draw.line(mapped + [mapped[0]], fill=stroke, width=sw, joint="curve")
                elif stroke:
                    draw.line(mapped, fill=stroke, width=sw, joint="curve")
        elif tag == "path":
            d = elem.attrib.get("d", "")
            for raw_pts, closed in _parse_svg_path(d):
                mapped = [tx(px, py) for px, py in raw_pts]
                if len(mapped) >= 2:
                    if closed and fill:
                        draw.polygon(mapped, fill=fill)
                    if stroke:
                        draw.line(mapped, fill=stroke, width=sw, joint="curve")


def render_card(
    question: str,
    answer: str,
    output_path: str,
    model: str = "",
    sketch: str = "",
    ratio: str = "3:4",
    question_only: bool = False,
) -> str:
    if ratio not in RATIOS:
        raise ValueError(f"Unsupported ratio '{ratio}'. Choose from: {', '.join(RATIOS)}")

    w, h = RATIOS[ratio]
    scale = min(w / 1080.0, h / 1440.0) if ratio != "16:9" else (h / 900.0) * 0.85

    img = Image.new("RGBA", (w, h), (*COLOR_YELLOW, 255))
    draw_striped_background(img)
    draw = ImageDraw.Draw(img)

    margin_x = int(w * 0.065)
    border_w = max(4, int(5 * scale))
    shadow_off = max(6, int(8 * scale))
    radius = max(6, int(8 * scale))

    # 1. 顶部极简栏：左上角仅写 BOKEMON
    header_h = max(60, int(74 * scale))
    draw.rectangle([0, 0, w, header_h], fill=COLOR_BLACK)
    brand_font = load_font(max(22, int(30 * scale)))
    brand_text = "BOKEMON"
    b_box = draw.textbbox((0, 0), brand_text, font=brand_font)
    draw.text(
        (margin_x - b_box[0], (header_h - (b_box[3] - b_box[1])) // 2 - b_box[1]),
        brand_text,
        font=brand_font,
        fill=COLOR_YELLOW,
    )

    content_top = header_h + int(42 * scale)
    content_bottom = h - int(44 * scale)
    avail_h = content_bottom - content_top

    if ratio == "16:9":
        gap_x = int(w * 0.035)
        q_x1 = margin_x
        q_x2 = margin_x + int((w - margin_x * 2 - gap_x) * 0.43)
        a_x1 = q_x2 + gap_x
        a_x2 = w - margin_x
        q_y1 = a_y1 = content_top
        q_y2 = a_y2 = content_bottom
    else:
        gap_y = int(34 * scale)
        q_h = int((avail_h - gap_y) * 0.34)
        q_x1 = a_x1 = margin_x
        q_x2 = a_x2 = w - margin_x
        q_y1 = content_top
        q_y2 = q_y1 + q_h
        a_y1 = q_y2 + gap_y
        a_y2 = content_bottom

    # 2. 题目区（黑底白字，左对齐）
    draw_board_box(draw, (q_x1, q_y1, q_x2, q_y2), fill=COLOR_BLACK, border=border_w, shadow_offset=shadow_off, radius=radius)

    q_pad_x = int(46 * scale)
    q_pad_y = int(34 * scale)
    q_font, q_lines, q_lh, q_total_h = fit_text(
        draw,
        question,
        max_width=(q_x2 - q_x1) - q_pad_x * 2,
        max_height=(q_y2 - q_y1) - q_pad_y * 2,
        start_size=max(28, int(42 * scale)),
        min_size=max(20, int(24 * scale)),
        line_spacing_ratio=1.38,
    )
    q_start_y = q_y1 + q_pad_y + (((q_y2 - q_y1) - q_pad_y * 2) - q_total_h) // 2
    for idx, line in enumerate(q_lines):
        lb = draw.textbbox((0, 0), line, font=q_font)
        lx = q_x1 + q_pad_x - lb[0]
        ly = q_start_y + idx * q_lh - lb[1]
        draw.text((lx, ly), line, font=q_font, fill=COLOR_WHITE)

    # 3. 回答白板区（纯白题板 + 硬投影）
    draw_board_box(draw, (a_x1, a_y1, a_x2, a_y2), fill=COLOR_WHITE, border=border_w, shadow_offset=shadow_off, radius=radius)

    if question_only:
        qmark_font = load_font(max(120, int(210 * scale)))
        qm = "?"
        qmb = draw.textbbox((0, 0), qm, font=qmark_font)
        qmw, qmh = qmb[2] - qmb[0], qmb[3] - qmb[1]
        draw.text(
            (a_x1 + ((a_x2 - a_x1) - qmw) // 2 - qmb[0], a_y1 + ((a_y2 - a_y1) - qmh) // 2 - qmb[1]),
            qm,
            font=qmark_font,
            fill=COLOR_BLACK,
        )
    else:
        a_pad_x = int(54 * scale)
        a_pad_top = int(46 * scale)
        a_pad_bot = int(72 * scale) if model else int(46 * scale)
        inner_h = (a_y2 - a_y1) - a_pad_top - a_pad_bot

        if sketch:
            # 上文（左对齐）+ 下图（居中简笔画配图），互不挤压
            text_max_h = int(inner_h * 0.42)
            a_font, a_lines, a_lh, a_total_h = fit_text(
                draw,
                answer,
                max_width=(a_x2 - a_x1) - a_pad_x * 2,
                max_height=text_max_h,
                start_size=max(34, int(56 * scale)),
                min_size=max(24, int(28 * scale)),
                line_spacing_ratio=1.34,
            )
            a_start_y = a_y1 + a_pad_top
            for idx, line in enumerate(a_lines):
                lb = draw.textbbox((0, 0), line, font=a_font)
                lx = a_x1 + a_pad_x - lb[0]
                ly = a_start_y + idx * a_lh - lb[1]
                draw.text((lx, ly), line, font=a_font, fill=COLOR_BLACK)

            sketch_top = a_start_y + a_total_h + int(28 * scale)
            sketch_bot = a_y2 - a_pad_bot - int(10 * scale)
            sketch_box = (a_x1 + a_pad_x, sketch_top, a_x2 - a_pad_x, sketch_bot)
            if os.path.exists(sketch) and os.path.splitext(sketch)[1].lower() in (".png", ".jpg", ".jpeg", ".webp"):
                render_raster_sketch(img, sketch, sketch_box)
                draw = ImageDraw.Draw(img)
            else:
                render_svg_sketch(draw, sketch, sketch_box)
        else:
            a_font, a_lines, a_lh, a_total_h = fit_text(
                draw,
                answer,
                max_width=(a_x2 - a_x1) - a_pad_x * 2,
                max_height=inner_h,
                start_size=max(40, int(68 * scale)),
                min_size=max(26, int(32 * scale)),
                line_spacing_ratio=1.36,
            )
            a_start_y = a_y1 + a_pad_top + (inner_h - a_total_h) // 2
            for idx, line in enumerate(a_lines):
                lb = draw.textbbox((0, 0), line, font=a_font)
                lx = a_x1 + a_pad_x - lb[0]
                ly = a_start_y + idx * a_lh - lb[1]
                draw.text((lx, ly), line, font=a_font, fill=COLOR_BLACK)

        if model:
            m_font = load_font(max(16, int(20 * scale)), regular=True)
            mb = draw.textbbox((0, 0), model, font=m_font)
            mx = a_x2 - (mb[2] - mb[0]) - int(28 * scale) - mb[0]
            my = a_y2 - (mb[3] - mb[1]) - int(22 * scale) - mb[1]
            draw.text((mx, my), model, font=m_font, fill=COLOR_MODEL_TEXT)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ext = os.path.splitext(output_path)[1].lower()
    rgb = img.convert("RGB")
    if ext in (".jpg", ".jpeg"):
        rgb.save(output_path, "JPEG", quality=95)
    else:
        rgb.save(output_path)
    return output_path


def run_single(args: argparse.Namespace) -> None:
    if not args.q or not args.a:
        print("Error: --q and --a are required in single mode.", file=sys.stderr)
        sys.exit(1)
    if args.split:
        base, ext = os.path.splitext(args.output)
        ext = ext or ".png"
        q_out = f"{base}_q{ext}"
        a_out = f"{base}_a{ext}"
        render_card(args.q, args.a, q_out, model=args.model, sketch=args.sketch, ratio=args.ratio, question_only=True)
        render_card(args.q, args.a, a_out, model=args.model, sketch=args.sketch, ratio=args.ratio, question_only=False)
        print(f"Rendered split cards:\n  Q: {q_out}\n  A: {a_out}")
    else:
        out = render_card(args.q, args.a, args.output, model=args.model, sketch=args.sketch, ratio=args.ratio)
        print(f"Rendered card: {out}")


def run_batch(args: argparse.Namespace) -> None:
    with open(args.batch, "r", encoding="utf-8") as f:
        items = json.load(f)
    os.makedirs(args.out_dir, exist_ok=True)
    for idx, item in enumerate(items, 1):
        q = item["q"]
        a = item["a"]
        model = item.get("model", args.model)
        sketch = item.get("sketch", "")
        filename = item.get("filename", f"bokemon_{idx:02d}.png")
        out_path = os.path.join(args.out_dir, filename)
        if args.split:
            base, ext = os.path.splitext(out_path)
            ext = ext or ".png"
            render_card(q, a, f"{base}_q{ext}", model=model, sketch=sketch, ratio=args.ratio, question_only=True)
            render_card(q, a, f"{base}_a{ext}", model=model, sketch=sketch, ratio=args.ratio, question_only=False)
        else:
            render_card(q, a, out_path, model=model, sketch=sketch, ratio=args.ratio)
    print(f"Batch rendered {len(items)} card(s) into {args.out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render BOKEMON IPPON-style Oogiri share cards.")
    parser.add_argument("--q", help="Question text")
    parser.add_argument("--a", help="Oogiri answer punchline")
    parser.add_argument("--model", default="", help="Optional model name shown at bottom-right of answer card")
    parser.add_argument("--sketch", default="", help="Optional inline SVG or .svg file path for hand-drawn sketch")
    parser.add_argument("--ratio", default="3:4", choices=list(RATIOS.keys()), help="Canvas aspect ratio")
    parser.add_argument("--split", action="store_true", help="Output two cards (_q and _a) to prevent spoilers")
    parser.add_argument("-o", "--output", default="00-Inbox/bokemon_card.png", help="Output file path")
    parser.add_argument("--batch", help="Path to JSON array of {q, a, model?, sketch?, filename?}")
    parser.add_argument("--out-dir", default="00-Inbox/bokemon_cards", help="Output directory for batch mode")

    parsed = parser.parse_args()
    if parsed.batch:
        run_batch(parsed)
    else:
        run_single(parsed)
