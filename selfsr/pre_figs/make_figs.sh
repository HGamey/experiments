#!/bin/sh
# Figures for the Genshin report "self-developed SR + interpolation vs native" (RedMagic NX809J).
# Usage: sh selfsr/pre_figs/make_figs.sh <pf-node> <work dir>
#   Downloads the job files listed below with `pf-node out` into <work dir>/pf-node-out (skips files already
#   there), then writes fig1..fig5 PNGs to <work dir>/figs.
# Host needs python3 with matplotlib, numpy, Pillow and fontTools, plus the Noto CJK fonts (see fonts.py).
set -eu
PF=$1
WORK=$2
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$WORK/figs"

FILES="
b0c94110b649 srbaseline_20261030-115528/srip_g7/arms.jsonl
b0c94110b649 srbaseline_20261030-115528/srip_g7/shots/A4.png
b0c94110b649 srbaseline_20261030-115528/srip_g7/shots/B4.png
b6d5969c4f6e srbaseline_20261030-101633/srip_g7/arms.jsonl
622ff5adb4e7 srbaseline_20261030-153619/srip_g7/arms.jsonl
622ff5adb4e7 srbaseline_20261030-153619/srip_g7/report.json
beed9385a1e1 srbaseline_20261101-035225/srip_g7/arms.jsonl
673fdb6b4bb8 srbaseline_20261029-201102/srip_g7/arms.jsonl
31cc1bb276f9 srbaseline_20261030-032023/srip_g7/arms.jsonl
0ba1b349ae36 selfsr_20261101-170854/7_sr_interp/report.json
0eb94025a536 traces/P1_hold.tsv
0eb94025a536 traces/P2_hold.tsv
0eb94025a536 traces/P2_seen.tsv
6c9ee0e74442 traces/P2_seen.tsv
"
for arm in A1 A2 A3 A4 A5 A6 B1 B2 B3 B4 B5 B6; do
  FILES="$FILES
b0c94110b649 srbaseline_20261030-115528/srip_g7/traces/$arm.txt"
done

cd "$WORK"
echo "$FILES" | while read -r job path; do
  [ -n "$job" ] || continue
  [ -f "pf-node-out/$job/$path" ] && continue
  "$PF" out "$job" "$path" >/dev/null || { echo "download failed: $job $path" >&2; exit 1; }
done

ROOT="$WORK/pf-node-out"
for f in fig1_pipeline fig2_picture fig3_smooth fig4_cost fig5_longrun; do
  (cd "$HERE" && python3 "$f.py" "$ROOT" "$WORK/figs/$f.png")
done
ls -l "$WORK/figs"
