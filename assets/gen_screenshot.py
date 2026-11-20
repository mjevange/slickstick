#!/usr/bin/env python3
"""Regenerate assets/screenshot.svg — a faithful terminal mock of the wizard.

Run from the repo root:  python3 assets/gen_screenshot.py
When the wizard menus change, edit the LINES list below to match, re-run, and
commit the regenerated assets/screenshot.svg.
"""
X0, CW, LH = 22.0, 8.4, 20.0
DEF, DIM, CYAN, GREEN = "#abb2bf", "#636d83", "#56b6c2", "#98c379"
ICE = ["#0078ff", "#00aaff", "#00d2ff", "#5aebff", "#aaf8ff"]

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def nbsp(s):
    return s.replace(" ", " ")

# each entry: (y, [(text, fill, weight), ...])
def opt(n, rest):
    return [("  ", DEF, 400), (n, CYAN, 700), (rest, DEF, 400)]

LINES = [
    (52,  [("  ____  _ _      _     ____  _   _      _", ICE[0], 700)]),
    (72,  [(" / ___|| (_) ___| | __/ ___|| |_(_) ___| | __", ICE[1], 700)]),
    (92,  [(" \\___ \\| | |/ __| |/ /\\___ \\| __| |/ __| |/ /", ICE[2], 700)]),
    (112, [("  ___) | | | (__|   <  ___) | |_| | (__|   <", ICE[3], 700)]),
    (132, [(" |____/|_|_|\\___|_|\\_\\|____/ \\__|_|\\___|_|\\_\\", ICE[4], 700)]),
    (152, [("  ", DEF, 400), ("Build bootable macOS installer USBs", DEF, 700),
           (" ", DEF, 400), ("— without a Mac", DIM, 400)]),
    (172, [("  ", DEF, 400), ("github.com/mjevange/slickstick", DIM, 400)]),
    (212, [("  ", DEF, 400), ("✓", GREEN, 400), (" Dependencies OK.", DEF, 400)]),
    (272, [("Target Mac architecture?", DEF, 400)]),
    (292, opt("1", ") Apple Silicon (M1/M2/M3/M4/M5)")),
    (312, opt("2", ") Intel (pre-T2 / T2)")),
    (332, opt("3", ") ↺  Start over")),
    (352, opt("4", ") ✕  Cancel")),
    (372, [("Choose [1-4]: 1", DEF, 400)]),
    (412, [("How should SlickStick get macOS?", DEF, 400)]),
    (432, opt("1", ") Download from Apple via gibMacOS (recommended)")),
    (452, opt("2", ") Clone from an inserted macOS installer stick (real markers, proven)")),
    (472, opt("3", ") Use files I already have (an Install app or a .pkg)")),
    (492, opt("4", ") Download a direct InstallAssistant.pkg URL")),
    (512, opt("5", ") ↺  Start over")),
    (532, opt("6", ") ✕  Cancel")),
]

spans = []
for y, segs in LINES:
    col = 0
    for text, fill, weight in segs:
        x = X0 + col * CW
        spans.append(f'<tspan x="{x:.1f}" y="{y:.1f}" fill="{fill}" '
                     f'font-weight="{weight}">{nbsp(esc(text))}</tspan>')
        col += len(text)

W, H = 648, 570
svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
    f'viewBox="0 0 {W} {H}" font-family="ui-monospace,\'SF Mono\',Menlo,'
    f'Consolas,\'DejaVu Sans Mono\',monospace" font-size="14">\n'
    f'  <rect width="{W}" height="{H}" rx="10" fill="#1b1e24"/>\n'
    f'  <rect width="{W}" height="30" rx="10" fill="#2b2f37"/>\n'
    f'  <rect y="20" width="{W}" height="10" fill="#2b2f37"/>\n'
    f'  <circle cx="18" cy="15" r="6" fill="#ec6a5e"/>\n'
    f'  <circle cx="38" cy="15" r="6" fill="#f4bf4f"/>\n'
    f'  <circle cx="58" cy="15" r="6" fill="#61c554"/>\n'
    f'  <text xml:space="preserve">\n    ' + "".join(spans) + '\n  </text>\n</svg>\n'
)
out = "assets/screenshot.svg"
open(out, "w").write(svg)
print(f"wrote {out} ({len(svg)} bytes, {len(spans)} spans)")
