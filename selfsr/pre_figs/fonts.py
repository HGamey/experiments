"""Simplified-Chinese Noto Sans faces for the figures.

Uses $PRE_FIGS_FONT_DIR/NotoSansCJKsc-{Regular,Bold}.otf when set. Otherwise extracts the SC face from the
system Noto CJK collections (/usr/share/fonts/opentype/noto/NotoSansCJK-{Regular,Bold}.ttc, Debian/Ubuntu
package fonts-noto-cjk) into ~/.cache/selfsr-pre-figs with fontTools. The collection's first face is the
Japanese one, whose glyph forms differ for some shared characters, so the SC face is picked by name.
"""
import os

SYSTEM = "/usr/share/fonts/opentype/noto/NotoSansCJK-%s.ttc"


def font_paths():
    d = os.environ.get("PRE_FIGS_FONT_DIR") or os.path.expanduser("~/.cache/selfsr-pre-figs")
    paths = {w: os.path.join(d, "NotoSansCJKsc-%s.otf" % w) for w in ("Regular", "Bold")}
    missing = [w for w, p in paths.items() if not os.path.exists(p)]
    if missing:
        from fontTools.ttLib import TTCollection
        os.makedirs(d, exist_ok=True)
        for w in missing:
            for face in TTCollection(SYSTEM % w).fonts:
                if face["name"].getDebugName(1) == "Noto Sans CJK SC":
                    face.save(paths[w])
                    break
            else:
                raise SystemExit("no Noto Sans CJK SC face in " + SYSTEM % w)
    return paths["Regular"], paths["Bold"]
