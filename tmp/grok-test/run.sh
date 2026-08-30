#!/bin/bash
set -u
mkdir -p out

TESTS=(
  "Rick and Morty|animation"
  "The Simpsons|animation"
  "Family Guy|animation"
  "Seinfeld|live-action"
  "The Office|live-action"
  "Breaking Bad|live-action"
  "SpongeBob SquarePants|animation"
)

TEMPLATE='Photorealistic wide-angle group photo of the main cast of the well-known TV show '"'"'%s'"'"' in a signature setting from the show. All main characters visible together, natural expressions, wardrobe and hair matching the show iconic look, cinematic lighting matching the show visual style. Do not include captions, text overlays, watermarks, or on-screen graphics.'

for entry in "${TESTS[@]}"; do
  IFS='|' read -r title kind <<< "$entry"
  slug=$(echo "$title" | tr '[:upper:] ' '[:lower:]_' | tr -d "'")
  echo ""
  echo "[$kind] $title"
  t0=$(date +%s)
  prompt=$(printf "$TEMPLATE" "$title")
  # jq to build JSON safely
  body=$(jq -nc --arg m "grok-imagine-image-2.0" --arg p "$prompt" '{model:$m, prompt:$p, n:1}')
  resp=$(curl -sS --max-time 120 -X POST https://api.x.ai/v1/images/generations \
    -H "Content-Type: application/json" \
    -d "$body")
  dt=$(( $(date +%s) - t0 ))
  url=$(echo "$resp" | jq -r '.data[0].url // empty')
  if [[ -z "$url" ]]; then
    echo "  FAIL in ${dt}s: $(echo "$resp" | head -c 300)"
    continue
  fi
  echo "  ok in ${dt}s: $url"
  curl -sS --max-time 60 "$url" -o "out/${slug}.jpg"
  size=$(stat -c%s "out/${slug}.jpg" 2>/dev/null || echo 0)
  echo "  saved -> out/${slug}.jpg (${size} bytes)"
done

echo ""
echo "Done. Files:"
ls -la out/
