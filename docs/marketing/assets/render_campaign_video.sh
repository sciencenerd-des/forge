#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/forge-campaign-30s.mp4"
PREVIEW="$ROOT/forge-campaign-preview.gif"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

make_frame() {
  local id="$1" accent="$2" panel="$3" eyebrow="$4" headline="$5" subhead="$6"
  cat > "$WORK/frame-$id.svg" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1920" viewBox="0 0 1080 1920">
  <rect width="1080" height="1920" fill="#F4EBDD"/>
  <rect x="70" y="72" width="940" height="178" rx="44" fill="#111111"/>
  <rect x="82" y="84" width="916" height="154" rx="34" fill="#FFDA57"/>
  <text x="120" y="180" font-family="Courier New, monospace" font-size="56" font-weight="700" fill="#111111">FORGE</text>
  <circle cx="916" cy="161" r="50" fill="#EF583F" stroke="#111111" stroke-width="7"/>
  <rect y="340" width="1080" height="1160" fill="$accent" stroke="#111111" stroke-width="8"/>
  <text x="82" y="430" font-family="Courier New, monospace" font-size="34" font-weight="700" letter-spacing="2" fill="#111111">$eyebrow</text>
  <text x="82" y="580" font-family="Arial, sans-serif" font-size="92" font-weight="900" fill="#111111">$headline</text>
  <rect x="72" y="1510" width="936" height="305" rx="28" fill="$panel" stroke="#111111" stroke-width="8"/>
  <text x="110" y="1605" font-family="Courier New, monospace" font-size="38" font-weight="700" fill="#111111">$subhead</text>
  <text x="82" y="1880" font-family="Courier New, monospace" font-size="30" font-weight="700" fill="#111111">0$id / 06   OPEN-SOURCE RELIABILITY HARNESS</text>
</svg>
SVG
  qlmanage -t -s 1920 -o "$WORK" "$WORK/frame-$id.svg" >/dev/null 2>&1
  mv "$WORK/frame-$id.svg.png" "$WORK/frame-$id.png"
}

make_frame 1 '#315CF4' '#FFDA57' 'THE PROBLEM' '<tspan x="82" dy="0">CODING AGENTS</tspan><tspan x="82" dy="105">CAN ACT.</tspan><tspan x="82" dy="105">BUT CAN THEY</tspan><tspan x="82" dy="105">PROVE IT?</tspan>' '<tspan x="110" dy="0">MODEL OUTPUT IS NOT</tspan><tspan x="110" dy="56">COMPLETION EVIDENCE.</tspan>'
make_frame 2 '#EF583F' '#7BD0A3' 'FAILURE IS NORMAL' '<tspan x="82" dy="0">PLANS FAIL.</tspan><tspan x="82" dy="105">CONTEXT</tspan><tspan x="82" dy="105">DISAPPEARS.</tspan><tspan x="82" dy="105">TESTS GET SKIPPED.</tspan>' '<tspan x="110" dy="0">LONG-RUN WORK NEEDS</tspan><tspan x="110" dy="56">DURABLE STATE.</tspan>'
make_frame 3 '#7BD0A3' '#FFDA57' 'DURABLE BY DESIGN' '<tspan x="82" dy="0">CHECKPOINT.</tspan><tspan x="82" dy="105">RECOVER.</tspan><tspan x="82" dy="105">RESUME.</tspan>' '<tspan x="110" dy="0">POSTGRES OWNS THE STATE.</tspan><tspan x="110" dy="56">NOT THE CHAT HISTORY.</tspan>'
make_frame 4 '#FFDA57' '#315CF4' 'DETERMINISTIC LOOP' '<tspan x="82" dy="0">PLAN.</tspan><tspan x="82" dy="105">AUDIT.</tspan><tspan x="82" dy="105">EXECUTE.</tspan><tspan x="82" dy="105">EVALUATE.</tspan>' '<tspan x="110" dy="0">BOUNDED ACTIONS.</tspan><tspan x="110" dy="56">VISIBLE PROGRESS.</tspan>'
make_frame 5 '#315CF4' '#7BD0A3' 'INDEPENDENT VERIFICATION' '<tspan x="82" dy="0">TESTS PASS?</tspan><tspan x="82" dy="105">SHOW THE</tspan><tspan x="82" dy="105">EVIDENCE.</tspan>' '<tspan x="110" dy="0">COMPLETION COMES FROM</tspan><tspan x="110" dy="56">EXIT CODES, NOT CONFIDENCE.</tspan>'
make_frame 6 '#EF583F' '#FFDA57' 'FORGE / OSS' '<tspan x="82" dy="0">AGENTS ACT.</tspan><tspan x="82" dy="105">FORGE PROVES.</tspan>' '<tspan x="110" dy="0">github.com/sciencenerd-des/forge</tspan><tspan x="110" dy="56">START A DURABLE RUN.</tspan>'

ffmpeg -loglevel error -y \
  -loop 1 -t 5 -i "$WORK/frame-1.png" -loop 1 -t 5 -i "$WORK/frame-2.png" \
  -loop 1 -t 5 -i "$WORK/frame-3.png" -loop 1 -t 5 -i "$WORK/frame-4.png" \
  -loop 1 -t 5 -i "$WORK/frame-5.png" -loop 1 -t 5 -i "$WORK/frame-6.png" \
  -filter_complex "[0:v]fps=30,scale=1080:1920,setsar=1[v0];[1:v]fps=30,scale=1080:1920,setsar=1[v1];[2:v]fps=30,scale=1080:1920,setsar=1[v2];[3:v]fps=30,scale=1080:1920,setsar=1[v3];[4:v]fps=30,scale=1080:1920,setsar=1[v4];[5:v]fps=30,scale=1080:1920,setsar=1[v5];[v0][v1]xfade=transition=slideleft:duration=0.35:offset=4.65[x1];[x1][v2]xfade=transition=slideup:duration=0.35:offset=9.30[x2];[x2][v3]xfade=transition=slideleft:duration=0.35:offset=13.95[x3];[x3][v4]xfade=transition=slideup:duration=0.35:offset=18.60[x4];[x4][v5]xfade=transition=slideleft:duration=0.35:offset=23.25,tpad=stop_mode=clone:stop_duration=1.75,format=yuv420p[v]" \
  -map '[v]' -t 30 -c:v libx264 -preset medium -crf 18 -movflags +faststart "$OUT"

ffmpeg -loglevel error -y -i "$OUT" -vf "fps=8,scale=360:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=96[p];[s1][p]paletteuse=dither=bayer" "$PREVIEW"
printf '%s\n%s\n' "$OUT" "$PREVIEW"
