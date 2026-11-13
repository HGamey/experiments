#!/usr/bin/env python3
"""fa_view.py <knobs_frameaudit.json> [frame_index] — one frame's passes in submission order, readable."""
import json
import sys

FMT = {9: "R8", 16: "RG8", 37: "RGBA8", 43: "RGBA8_SRGB", 44: "BGRA8", 50: "BGRA8_SRGB", 58: "A2R10G10B10", 64: "A2B10G10R10",
       70: "R16_UNORM", 74: "R16_UINT", 76: "R16F", 77: "RG16_UNORM", 81: "RG16_UINT", 83: "RG16F", 91: "RGBA16_UNORM",
       97: "RGBA16F", 98: "R32_UINT", 100: "R32F", 103: "RG32F", 107: "RGBA32_UINT", 109: "RGBA32F", 122: "B10G11R11F",
       123: "E5B9G9R9", 124: "D16", 125: "X8D24", 126: "D32F", 127: "S8", 128: "D16S8", 129: "D24S8", 130: "D32FS8"}
LOAD = {0: "L", 1: "C", 2: "-", 1000400000: "n"}
STORE = {0: "S", 1: "-", 1000301000: "n"}
LAY = {0: "UND", 1: "GEN", 2: "COL", 3: "DSA", 4: "DSR", 5: "SRO", 6: "TSRC", 7: "TDST", 1000001002: "PRES",
       1000117000: "DROSA", 1000117001: "DASRO", 1000241000: "DA", 1000241001: "DRO", 1000314000: "RO", 1000314001: "ATT"}
ROLE = {0: "col", 1: "ds", 2: "res", 3: "inp", 4: "unused"}


def fmt(f):
    return FMT.get(f, str(f))


def main():
    d = json.load(open(sys.argv[1]))
    fi = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    imgs = {int(k): v for k, v in d.get("images", {}).items()} if isinstance(d.get("images"), dict) else \
        {im.get("id"): im for im in d.get("images", [])}
    sw, sh = d["swapchain"]["w"], d["swapchain"]["h"]
    print(f"swapchain {sw}x{sh} fmt {fmt(d['swapchain']['fmt'])}  frames={len(d['frames'])} diag={d['diag']}")
    fr = d["frames"][fi]
    print(f"frame {fr['frame']} present img {fr['present']}")
    n = 0
    for s in fr["submits"]:
        for cb in s["cbs"]:
            if cb.get("unseen"):
                print(f"  q{s['q']} cb UNSEEN")
                continue
            for e in cb["ev"]:
                n += 1
                if e["k"] == "pass":
                    tag = " <== SWAP-SIZE" if (e["w"], e["h"]) == (sw, sh) else ""
                    atts = []
                    for a in e["att"]:
                        im = imgs.get(a["i"], {})
                        u = im.get("usage")
                        atts.append(f"i{a['i']}:{fmt(a['f'])}{'x'+str(a['s']) if a['s']>1 else ''}:{ROLE.get(a['ro'],a['ro'])}"
                                    f"[{LOAD.get(a['lo'],a['lo'])}{STORE.get(a['st'],a['st'])}"
                                    f"{(' s'+LOAD.get(a['slo'],str(a['slo']))+STORE.get(a['sst'],str(a['sst']))) if a['f'] in (129,130,128) else ''}"
                                    f" {LAY.get(a['il'],a['il'])}>{LAY.get(a['fl'],a['fl'])}{' u'+hex(u) if u is not None else ''}]")
                    rd = ",".join(str(r[0]) for r in e.get("rd", []))
                    wr = ",".join(str(r[0]) for r in e.get("wr", []))
                    print(f"{n:4d} q{s['q']} PASS {e['w']}x{e['h']} dr={e['dr']+e['dri']+e['drn']} sig={e.get('sig','')} "
                          f"{' '.join(atts)} rd[{rd}]{' wr['+wr+']' if wr else ''}{tag}")
                elif e["k"] == "dispatch":
                    rd = ",".join(str(r[0]) for r in e.get("rd", []))
                    wr = ",".join(str(r[0]) for r in e.get("wr", []))
                    print(f"{n:4d} q{s['q']} DISPATCH {e['g']} rd[{rd}] wr[{wr}]")
                else:
                    src = e.get("src", [0])[0] if e.get("src") else ""
                    dst = e.get("dst", [0])[0] if e.get("dst") else ""
                    print(f"{n:4d} q{s['q']} {e['k']} src={src} dst={dst} ex={e.get('ex')} sex={e.get('sex')}")
    print("\nimages referenced (id: w x h fmt usage):")
    for k in sorted(imgs):
        im = imgs[k]
        if im.get("w", 0) >= 256:
            print(f"  i{k}: {im.get('w')}x{im.get('h')} {fmt(im.get('fmt'))} usage={hex(im.get('usage', 0))} "
                  f"mips={im.get('mips')} layers={im.get('layers')} {({kk: vv for kk, vv in im.items() if kk not in ('w','h','fmt','usage','mips','layers','id')})}")


if __name__ == "__main__":
    main()
