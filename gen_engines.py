#!/usr/bin/env python3
"""Generate per-line Metra engine map icons from the official GTFS line colors.

Outputs engine_<lineslug>_{inbound,outbound}.svg into --out (default cwd) and a
customize-yaml block on stdout (entity_picture per map slot entity, 12 slots per
direction). The inbound template is engine_inbound.svg in this directory (body
fill #003087 is replaced per line; wordmark flips dark on light line colors).
"""
import re
import sys
from pathlib import Path

LINE_COLORS = {  # from GTFS routes.txt (Metra official)
    "BNSF": "#29C233", "HC": "#550E0C", "MD-N": "#CC5500", "MD-W": "#F1AD0E",
    "ME": "#EB5C00", "NCS": "#9785BC", "RI": "#E02400", "SWS": "#0042A8",
    "UP-N": "#008000", "UP-NW": "#FFE600", "UP-W": "#FE8D81",
}

def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

def luminance(hexcolor):
    r, g, b = (int(hexcolor[i:i+2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def mirror(svg):
    svg = svg.replace('<g transform="translate(32,34) scale(1.35) translate(-32,-34)">',
                      '<g transform="translate(32,34) scale(1.35) translate(-32,-34)"><g transform="translate(64,0) scale(-1,1)">', 1)
    svg = svg.rsplit("</g>\n</svg>", 1)
    svg = svg[0] + "</g></g>\n</svg>" + (svg[1] if len(svg) > 1 else "")
    # un-mirror the wordmark so it stays readable
    svg = svg.replace(' text-anchor="middle">Metra</text>',
                      ' text-anchor="middle" transform="translate(54,0) scale(-1,1)">Metra</text>')
    return svg

def main():
    here = Path(__file__).parent
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else Path.cwd()
    template = (here / "engine_inbound.svg").read_text()
    lines_cust = []
    for line, color in LINE_COLORS.items():
        sl = slug(line)
        body = template.replace("#003087", color)
        if luminance(color) > 0.55:  # light line color -> dark wordmark
            body = body.replace('font-style="italic" fill="#ffffff" text-anchor="middle"',
                                'font-style="italic" fill="#1a1a1a" text-anchor="middle"')
        (out / f"engine_{sl}_inbound.svg").write_text(body)
        (out / f"engine_{sl}_outbound.svg").write_text(mirror(body))
        for d in ("inbound", "outbound"):
            for i in range(1, 13):
                lines_cust.append(f"sensor.metra_{sl}_map_{d}_{i}:\n"
                                  f"  entity_picture: /local/metra/engine_{sl}_{d}.svg\n")
    print("".join(lines_cust))

if __name__ == "__main__":
    main()
