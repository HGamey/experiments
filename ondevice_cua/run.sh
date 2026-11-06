#!/system/bin/sh
# run.sh <out_prefix> <cmd_script>: run cmd_script in the background, poll its peak memory, print one summary line.
# The pid is written to <out_prefix>.pid so the host can stop exactly this process (never by name).
OUT=$1; CMD=$2
sh "$CMD" > "$OUT.stdout" 2> "$OUT.stderr" &
pid=$!
echo $pid > "$OUT.pid"
hwm=0; anon=0; t0=$(cut -d' ' -f1 /proc/uptime)
while kill -0 $pid 2>/dev/null; do
  for c in $pid $(cat /proc/$pid/task/*/children 2>/dev/null); do
    h=$(grep VmHWM /proc/$c/status 2>/dev/null | tr -s ' ' | cut -d' ' -f2); [ -n "$h" ] && [ "$h" -gt "$hwm" ] && hwm=$h
    a=$(grep RssAnon /proc/$c/status 2>/dev/null | tr -s ' ' | cut -d' ' -f2); [ -n "$a" ] && [ "$a" -gt "$anon" ] && anon=$a
  done
  sleep 0.2
done
wait $pid; rc=$?
t1=$(cut -d' ' -f1 /proc/uptime)
echo "R rc=$rc hwm_kb=$hwm anon_kb=$anon t0=$t0 t1=$t1"
