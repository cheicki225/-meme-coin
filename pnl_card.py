"""
════════════════════════════════════════════════════════════════
PNL CARD — Génère une image de résultat de trade (comme F Project)
════════════════════════════════════════════════════════════════
Reproduit l'info affichée par les cartes PNL de F Project (%, montants
achetés/vendus/détenus, profit en SOL et en USD, couleur verte/rouge
selon le résultat) — mais avec notre propre design, pas les visuels/la
mascotte de F Project (qui sont leur identité de marque).

Utilise Pillow. Cherche une police système parmi plusieurs chemins
courants (Linux et Windows) ; si aucune n'est trouvée, retombe sur la
police bitmap par défaut de Pillow (moins jolie mais ne plante jamais).
"""

import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("pnl_card")

CARD_WIDTH = 720
CARD_HEIGHT = 480

COLOR_WIN = (34, 197, 94)      # vert
COLOR_LOSS = (239, 68, 68)     # rouge
COLOR_BG = (13, 17, 23)        # fond sombre
COLOR_TEXT_MUTED = (156, 163, 175)
COLOR_TEXT = (255, 255, 255)

# ── Mascottes (assets fournis par l'utilisateur) ──────────────────
# Utilisées uniquement pour les GAINS, réparties par ampleur du résultat —
# aucune des 3 images fournies ne correspondant à une "perte", les pertes
# gardent le dessin vectoriel (flèche cassée).
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
MASCOT_EPIC = os.path.join(ASSETS_DIR, "mascot_epic.png")        # gros gains (>=200%)
MASCOT_CELEBRATE = os.path.join(ASSETS_DIR, "mascot_celebrate.png")  # gains moyens (100-200%)
MASCOT_CHILL = os.path.join(ASSETS_DIR, "mascot_chill.png")      # petits gains (<100%)

FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux
    "C:\\Windows\\Fonts\\arialbd.ttf",                         # Windows
    "C:\\Windows\\Fonts\\segoeuib.ttf",                        # Windows (Segoe UI Bold)
]
FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
]


def _load_font(candidates: list, size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    log.warning("Aucune police système trouvée parmi les chemins connus — police par défaut Pillow utilisée (moins lisible).")
    return ImageFont.load_default()


def _dim(color: tuple, factor: float = 0.35) -> tuple:
    """Assombrit une couleur pour un effet watermark discret."""
    r, g, b = color
    bg_r, bg_g, bg_b = COLOR_BG
    return (
        int(bg_r + (r - bg_r) * factor),
        int(bg_g + (g - bg_g) * factor),
        int(bg_b + (b - bg_b) * factor),
    )


def _draw_crosshair(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int, color: tuple, width: int = 3):
    """Dessine un viseur/crosshair — thème 'sniper bot', dessin vectoriel original."""
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=color, width=width)
    draw.ellipse([cx - radius // 2, cy - radius // 2, cx + radius // 2, cy + radius // 2], outline=color, width=width)
    gap = radius // 4
    # Lignes horizontales (avec un espace au centre)
    draw.line([cx - radius - 15, cy, cx - gap, cy], fill=color, width=width)
    draw.line([cx + gap, cy, cx + radius + 15, cy], fill=color, width=width)
    # Lignes verticales (avec un espace au centre)
    draw.line([cx, cy - radius - 15, cx, cy - gap], fill=color, width=width)
    draw.line([cx, cy + gap, cx, cy + radius + 15], fill=color, width=width)
    # Point central
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)


def _load_mascot_circle(path: str, diameter: int, border_color: tuple) -> Image.Image:
    """
    Charge une image mascotte, la recadre en carré (centré), la redimensionne,
    et l'applique dans un masque circulaire avec une bordure colorée — pour
    un rendu propre quel que soit le format/ratio de l'image source.
    Retourne une image RGBA prête à être collée (paste) sur la carte.
    """
    mascot = Image.open(path).convert("RGBA")

    # Recadrage carré centré
    w, h = mascot.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    mascot = mascot.crop((left, top, left + side, top + side))
    mascot = mascot.resize((diameter, diameter), Image.LANCZOS)

    # Masque circulaire
    mask = Image.new("L", (diameter, diameter), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([0, 0, diameter, diameter], fill=255)

    result = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    result.paste(mascot, (0, 0), mask=mask)

    # Bordure circulaire colorée
    border_layer = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(border_layer)
    border_draw.ellipse([2, 2, diameter - 2, diameter - 2], outline=border_color, width=4)
    result = Image.alpha_composite(result, border_layer)

    return result


def _pick_mascot_path(result_pct: float) -> str:
    if result_pct >= 200:
        return MASCOT_EPIC
    elif result_pct >= 100:
        return MASCOT_CELEBRATE
    else:
        return MASCOT_CHILL


def _draw_rocket(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: tuple):
    """Petite fusée stylisée (gain) — formes géométriques simples, dessin original."""
    # Corps (triangle pointant vers le haut-droite, façon "trajectoire ascendante")
    body = [(cx, cy - size), (cx - size * 0.4, cy + size * 0.5), (cx + size * 0.4, cy + size * 0.5)]
    draw.polygon(body, fill=color)
    # Hublot
    draw.ellipse([cx - size * 0.15, cy - size * 0.3, cx + size * 0.15, cy], fill=COLOR_BG)
    # Ailerons
    draw.polygon([(cx - size * 0.4, cy + size * 0.5), (cx - size * 0.7, cy + size * 0.9), (cx - size * 0.1, cy + size * 0.6)], fill=color)
    draw.polygon([(cx + size * 0.4, cy + size * 0.5), (cx + size * 0.7, cy + size * 0.9), (cx + size * 0.1, cy + size * 0.6)], fill=color)
    # Traînée
    for i, alpha_size in enumerate([0.35, 0.22, 0.12]):
        trail_y = cy + size * 0.5 + (i + 1) * size * 0.35
        draw.ellipse([cx - size * alpha_size, trail_y, cx + size * alpha_size, trail_y + size * 0.25], fill=color)


def _draw_broken_arrow(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: tuple, width: int = 6):
    """Flèche descendante cassée (perte) — dessin vectoriel original."""
    draw.line([cx - size, cy - size * 0.6, cx - size * 0.2, cy], fill=color, width=width)
    draw.line([cx + size * 0.2, cy + size * 0.15, cx + size, cy + size * 0.7], fill=color, width=width)
    # Pointe de flèche
    tip = [(cx + size, cy + size * 0.7), (cx + size * 0.55, cy + size * 0.55), (cx + size * 0.85, cy + size * 0.35)]
    draw.polygon(tip, fill=color)
    # "Cassure" au milieu (petit zigzag)
    draw.line([cx - size * 0.2, cy, cx, cy - size * 0.15, cx + size * 0.2, cy + size * 0.15], fill=color, width=width // 2)


def build_pnl_card(token_label: str, result_pct: float, bought_sol: float, sold_sol: float,
                    holding_sol: float, pnl_sol: float, pnl_usd: float, reason: str) -> bytes:
    """
    Construit une carte PNL et retourne les bytes PNG prêts à envoyer.

    token_label: nom/ticker/adresse tronquée du token
    result_pct: résultat en % (positif ou négatif)
    bought_sol/sold_sol/holding_sol: montants en SOL
    pnl_sol/pnl_usd: profit ou perte net
    reason: raison de clôture (TP, SL, MANUAL_SELL_ALL, etc.)
    """
    is_win = pnl_usd >= 0
    accent = COLOR_WIN if is_win else COLOR_LOSS

    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), COLOR_BG)
    draw = ImageDraw.Draw(img)

    font_huge = _load_font(FONT_CANDIDATES_BOLD, 72)
    font_large = _load_font(FONT_CANDIDATES_BOLD, 32)
    font_medium = _load_font(FONT_CANDIDATES_REGULAR, 26)
    font_small = _load_font(FONT_CANDIDATES_REGULAR, 20)

    # Bande d'accent en haut
    draw.rectangle([0, 0, CARD_WIDTH, 8], fill=accent)

    # Crosshair discret en watermark (thème "sniper bot"), coin haut-droit
    _draw_crosshair(draw, CARD_WIDTH - 90, 90, radius=45, color=_dim(accent), width=2)

    # Mascotte (gains uniquement, choisie selon l'ampleur du résultat) ou
    # flèche cassée vectorielle (pertes — aucune image fournie ne convient)
    if is_win:
        try:
            mascot_path = _pick_mascot_path(result_pct)
            mascot_img = _load_mascot_circle(mascot_path, diameter=130, border_color=accent)
            img = img.convert("RGBA")
            img.paste(mascot_img, (CARD_WIDTH - 160, 25), mask=mascot_img)
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)  # redraw handle après conversion
        except Exception as e:
            log.warning(f"Mascotte introuvable/erreur ({e}) — repli sur la fusée vectorielle.")
            _draw_rocket(draw, CARD_WIDTH - 90, 170, size=38, color=accent)
    else:
        _draw_broken_arrow(draw, CARD_WIDTH - 150, 150, size=45, color=accent)

    # Nom du token
    draw.text((40, 40), token_label, font=font_large, fill=COLOR_TEXT)

    # Résultat en % — gros et coloré
    pct_text = f"{result_pct:+.1f}%"
    draw.text((40, 90), pct_text, font=font_huge, fill=accent)

    # Détails bought/sold/holding
    y = 220
    for label, value in [("BOUGHT", bought_sol), ("SOLD", sold_sol), ("HOLDING", holding_sol)]:
        draw.text((40, y), label, font=font_small, fill=COLOR_TEXT_MUTED)
        draw.text((250, y), f"{value:.4f} SOL", font=font_medium, fill=COLOR_TEXT)
        y += 40

    # Bandeau de résultat final (SOL + USD)
    band_y = 370
    draw.rectangle([0, band_y, CARD_WIDTH, band_y + 70], fill=accent)
    result_line = f"{pnl_sol:+.3f} SOL      {pnl_usd:+.2f}$"
    draw.text((40, band_y + 18), result_line, font=font_large, fill=(10, 10, 10))

    # Raison de clôture, discrète en bas
    draw.text((40, CARD_HEIGHT - 35), f"Raison : {reason}", font=font_small, fill=COLOR_TEXT_MUTED)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
