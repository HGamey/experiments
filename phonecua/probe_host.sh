#!/bin/bash
# Read-only host probe: tools available on the execution machine and whether the LLM relay is reachable.
# Prints no secrets: only whether a relay env file exists and the HTTP status of an unauthenticated request.
echo "host_python=$(command -v python3) $(python3 --version 2>&1)"
echo "adb=$(command -v adb)"
for f in "$HOME/.config/yao-relay.env" "$HOME/.config/phonecua/relay.env"; do
  [ -f "$f" ] && echo "relay_env_present=$f keys=$(sed -n 's/=.*//p' "$f" | tr '\n' ',')"
done
for u in https://relay.liyao.space/v1/models https://api.openai.com/v1/models; do
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code} %{time_total}' -A curl/8 "$u")
  echo "reach $u -> $code"
done
python3 -c "import PIL; print('PIL', PIL.__version__)" 2>&1 | tail -1
env | grep -E '^PF_|^PHONEFARM|^DEVLOCK' | sed 's/TOKEN=.*/TOKEN=<redacted>/'
