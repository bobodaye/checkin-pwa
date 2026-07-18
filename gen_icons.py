"""Generate crisp PWA icons for the check-in app."""
from PIL import Image, ImageDraw, ImageFilter
import math
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(ROOT, "icons")

BG = "#0b1220"
ACCENT = "#3b82f6"
FACE = "#f8fafc"
HAND = "#0f172a"
CHECK = "#ffffff"
SHADOW = "#000000"


def hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4)) + (255,)


def blend_rgba(bg, fg, alpha):
    """blend fg onto bg with alpha 0..1"""
    return tuple(int(bg[i] * (1 - alpha) + fg[i] * alpha) for i in range(4))


def make_icon(size, maskable=False):
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background
    d.rectangle([0, 0, s, s], fill=BG)

    center = s // 2
    # For maskable, keep all content within the center ~66% safe zone
    padding = s // 4 if maskable else s // 10
    outer_r = (s - padding * 2) // 2
    ring_w = s // 18
    inner_r = outer_r - ring_w - s // 60

    # Soft drop shadow for the ring/face
    shadow_offset = s // 80
    shadow_r = outer_r + s // 120
    for i in range(20, 0, -1):
        alpha = int(12 * (1 - i / 20))
        shadow_color = (*hex_to_rgb(SHADOW)[:3], alpha)
        d.ellipse(
            [center - shadow_r + shadow_offset, center - shadow_r + shadow_offset,
             center + shadow_r + shadow_offset, center + shadow_r + shadow_offset],
            fill=shadow_color,
        )

    # Blue ring (progress ring style with a gap for checkmark)
    d.ellipse(
        [center - outer_r, center - outer_r, center + outer_r, center + outer_r],
        fill=ACCENT,
    )

    # Inner face
    d.ellipse(
        [center - inner_r, center - inner_r, center + inner_r, center + inner_r],
        fill=FACE,
    )

    # Clock hands (hour and minute) - dark navy
    hand_w = max(2, s // 60)
    hour_len = inner_r * 0.5
    minute_len = inner_r * 0.75
    # Hour hand at ~10:10
    hour_angle = math.radians(300)  # 10 o'clock-ish
    minute_angle = math.radians(54)  # ~10 past

    def draw_hand(angle, length, width, color):
        x = center + math.cos(angle) * length
        y = center - math.sin(angle) * length
        # Round cap via small circle at tip
        d.line([(center, center), (x, y)], fill=color, width=width)
        d.ellipse([x - width // 2, y - width // 2, x + width // 2, y + width // 2], fill=color)

    draw_hand(hour_angle, hour_len, hand_w * 2, HAND)
    draw_hand(minute_angle, minute_len, hand_w, HAND)

    # Center dot
    dot_r = s // 40
    d.ellipse([center - dot_r, center - dot_r, center + dot_r, center + dot_r], fill=HAND)

    # Checkmark at bottom-right (white, integrated with the ring)
    check_center_x = center + outer_r * 0.55
    check_center_y = center + outer_r * 0.55
    check_r = s // 13
    d.ellipse(
        [check_center_x - check_r, check_center_y - check_r,
         check_center_x + check_r, check_center_y + check_r],
        fill=CHECK,
    )
    # Checkmark stroke
    chk_w = max(2, s // 45)
    # V shape: left-down, right-up
    p1 = (check_center_x - check_r * 0.35, check_center_y - check_r * 0.05)
    p2 = (check_center_x - check_r * 0.05, check_center_y + check_r * 0.35)
    p3 = (check_center_x + check_r * 0.45, check_center_y - check_r * 0.25)
    d.line([p1, p2], fill=ACCENT, width=chk_w)
    d.line([p2, p3], fill=ACCENT, width=chk_w)
    # Round caps for checkmark
    for p in (p1, p2, p3):
        d.ellipse([p[0] - chk_w // 2, p[1] - chk_w // 2, p[0] + chk_w // 2, p[1] + chk_w // 2], fill=ACCENT)

    # Downscale with high quality antialiasing
    img = img.resize((size, size), Image.LANCZOS)
    return img


def main():
    os.makedirs(ICONS_DIR, exist_ok=True)

    sizes = {
        "icon-512.png": 512,
        "icon-192.png": 192,
        "icon-180.png": 180,
    }
    for filename, size in sizes.items():
        img = make_icon(size)
        img.save(os.path.join(ICONS_DIR, filename), "PNG")
        print(f"Saved {filename} ({size}x{size})")

    # Maskable variants (content within safe zone for adaptive icons)
    for filename, size in (("icon-512-maskable.png", 512), ("icon-192-maskable.png", 192)):
        img = make_icon(size, maskable=True)
        img.save(os.path.join(ICONS_DIR, filename), "PNG")
        print(f"Saved {filename} ({size}x{size})")

    # Favicon ICO (16, 32, 48, 64) — PIL creates these sizes by scaling the source
    favicon_64 = make_icon(64)
    favicon_64.save(
        os.path.join(ICONS_DIR, "favicon.ico"),
        format="ICO",
        sizes=[(64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print("Saved favicon.ico")


if __name__ == "__main__":
    main()
