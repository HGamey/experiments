#!/usr/bin/env python3
"""HONOR smart/high refresh comparison using only adb + Python stdlib.

probe: sample the current foreground game without changing any setting.
full run: read the original UI refresh mode, compare smart then high, and restore.
"""
import argparse
import csv
import json
import re
import statistics
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

MODES = ("智能", "标准", "高")
UI_REMOTE = "/sdcard/honor_refresh_ab.xml"


def adb(serial, *args, timeout=60, check=True):
    result = subprocess.run(["adb", "-s", serial, *args], capture_output=True,
                            text=True, errors="replace", timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"adb {' '.join(args)} failed ({result.returncode}): {result.stderr[-1000:]}")
    return result


def shell(serial, command, timeout=60, check=True):
    return adb(serial, "shell", command, timeout=timeout, check=check).stdout


def wakefulness(serial):
    match = re.search(r"mWakefulness=(\w+)", shell(serial, "dumpsys power"))
    return match.group(1) if match else "unknown"


def dump_ui(serial, target):
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    adb(serial, "shell", f"uiautomator dump {UI_REMOTE}", check=False)
    adb(serial, "pull", UI_REMOTE, str(path), check=True)
    adb(serial, "shell", f"rm -f {UI_REMOTE}", check=False)
    return path.read_text(encoding="utf-8", errors="replace")


def bounds(node):
    values = re.findall(r"\d+", node.attrib.get("bounds", ""))
    if len(values) != 4:
        return None
    x1, y1, x2, y2 = map(int, values)
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def nodes(xml):
    return list(ET.fromstring(xml).iter("node"))


def dialog_cancel(xml):
    for node in nodes(xml):
        if node.attrib.get("resource-id") == "android:id/button2" and node.attrib.get("text") in ("取消", "Cancel"):
            return bounds(node)
    return None


def find_label(xml, label, contains=False):
    for node in nodes(xml):
        text = node.attrib.get("text", "")
        if (label in text if contains else text == label):
            point = bounds(node)
            if point:
                return point
    return None


def find_desc(xml, label):
    for node in nodes(xml):
        if node.attrib.get("content-desc") == label and bounds(node):
            return bounds(node)
    return None


def tap(serial, point):
    adb(serial, "shell", f"input tap {point[0]} {point[1]}")


def dismiss_dialog_if_needed(serial):
    xml_path = Path(serial + "_dialog.xml")
    xml = dump_ui(serial, xml_path)
    point = dialog_cancel(xml)
    if not point:
        return False
    tap(serial, point)
    time.sleep(1.5)
    return True


def nav_log(out, step, started, attempt=None):
    event = {"wall": time.time(), "step": step, "attempt": attempt,
             "duration_s": round(time.monotonic() - started, 3)}
    Path(out).mkdir(parents=True, exist_ok=True)
    with open(Path(out) / "navigation.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def open_refresh_page(serial, out):
    started = time.monotonic()
    adb(serial, "shell", "input keyevent KEYCODE_HOME")
    time.sleep(0.5)
    launch = adb(serial, "shell", "am start -n com.android.settings/.Settings\\$DisplaySettingsActivity --activity-clear-top")
    if "Error" in launch.stdout or "Exception" in launch.stdout:
        raise RuntimeError(f"display settings launch failed: {launch.stdout}")
    nav_log(out, "home_and_explicit_display_settings", started)
    time.sleep(2.5)
    size = shell(serial, "wm size")
    match = re.search(r"(\d+)x(\d+)", size)
    width, height = map(int, match.groups()) if match else (1272, 2800)
    for attempt in range(6):
        xml_path = Path(out) / f"display_page_{attempt}.xml"
        xml = dump_ui(serial, xml_path)
        cancel = dialog_cancel(xml)
        if cancel:
            tap(serial, cancel)
            nav_log(out, "usb_dialog_cancel", started, attempt)
            time.sleep(1.5)
            xml = dump_ui(serial, xml_path)
        has_dark_controls = "com.android.settings:id/light_container" in xml or "com.android.settings:id/dark_container" in xml
        has_display_root_content = any(marker in xml for marker in ("亮度", "字体与显示大小", "字体设置", "显示大小", "屏幕", "休眠"))
        if has_dark_controls and not has_display_root_content:
            back = find_desc(xml, "向上导航")
            if back:
                tap(serial, back)
                nav_log(out, "dark_mode_up", started, attempt)
                time.sleep(1.5)
                xml = dump_ui(serial, xml_path)
        point = find_label(xml, "刷新率", contains=True)
        if point:
            tap(serial, point)
            time.sleep(2)
            nav_log(out, "refresh_page_ready", started, attempt)
            return dump_ui(serial, Path(out) / "refresh_rate_page.xml")
        adb(serial, "shell", f"input swipe {width // 2} {int(height * .82)} {width // 2} {int(height * .28)} 350")
        nav_log(out, "scroll_for_refresh_entry", started, attempt)
        time.sleep(1.2)
    nav_log(out, "refresh_page_failed", started)
    raise RuntimeError("refresh-rate entry not found after dismissing dialogs and scrolling")


def selected_mode(xml):
    labels = [(node.attrib["text"], bounds(node)) for node in nodes(xml)
              if node.attrib.get("text") in MODES and bounds(node)]
    checked = [bounds(node) for node in nodes(xml)
               if node.attrib.get("resource-id") == "android:id/checkbox"
               and node.attrib.get("checked") == "true" and bounds(node)]
    if len(checked) != 1 or not labels:
        raise RuntimeError(f"cannot identify selected refresh mode: checked={len(checked)}, labels={labels}")
    x, y = checked[0]
    label, point = min(labels, key=lambda item: abs(item[1][1] - y) + abs(item[1][0] - x) / 10)
    if abs(point[1] - y) > 150:
        raise RuntimeError("selected radio is not aligned with a known mode label")
    return label


def read_mode(serial, out):
    xml = open_refresh_page(serial, out)
    return selected_mode(xml)


def set_mode(serial, out, mode):
    started = time.monotonic()
    if mode not in MODES:
        raise ValueError(mode)
    xml = open_refresh_page(serial, out)
    before = selected_mode(xml)
    if before != mode:
        point = find_label(xml, mode)
        if not point:
            raise RuntimeError(f"mode label not found: {mode}")
        tap(serial, point)
        time.sleep(1.5)
        xml = dump_ui(serial, Path(out) / "refresh_rate_page.xml")
    after = selected_mode(xml)
    if after != mode:
        raise RuntimeError(f"refresh mode verification failed: wanted {mode}, got {after}")
    Path(out, f"mode_{mode}.xml").write_text(xml, encoding="utf-8")
    return {"before": before, "after": after, "verified": True,
            "navigation_and_verify_seconds": round(time.monotonic() - started, 3)}


def foreground(serial):
    return shell(serial, "dumpsys window | grep -i -E 'mCurrentFocus|mFocusedApp' | head -8", check=False)


def launch_game(serial, package, settle):
    adb(serial, "shell", f"am force-stop {package}")
    adb(serial, "shell", f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=60)
    time.sleep(settle)
    focus = foreground(serial)
    if package not in focus:
        raise RuntimeError(f"game is not foreground after launch: {focus[-500:]}")


def resolve_layer(serial, package, requested):
    text = shell(serial, "dumpsys SurfaceFlinger --list")
    candidates = []
    for line in text.splitlines():
        if package not in line or "Background for" in line:
            continue
        candidates.extend(re.findall(r"SurfaceView\[[^\n]+?\]\(BLAST\)#\d+", line))
    if not candidates:
        raise RuntimeError(f"no current BLAST SurfaceView layer for {package}")
    if requested and requested != "auto":
        base = requested.split("#", 1)[0].replace("(BLAST)", "")
        matching = [candidate for candidate in candidates if base in candidate]
        if matching:
            candidates = matching
    return candidates[-1]


def battery_c(serial):
    match = re.search(r"temperature:\s*(\d+)", shell(serial, "dumpsys battery"))
    return int(match.group(1)) / 10 if match else None


def cooldown(serial, package, target_c, out, max_seconds=900):
    adb(serial, "shell", f"am force-stop {package}")
    start = time.monotonic()
    records = []
    while True:
        temp = battery_c(serial)
        elapsed = time.monotonic() - start
        records.append({"elapsed_s": round(elapsed, 1), "battery_c": temp, "target_c": target_c})
        Path(out, "cooldown.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        if temp is not None and temp <= target_c + 1.0:
            return {"ok": True, "seconds": elapsed, "battery_c": temp, "target_c": target_c}
        if elapsed >= max_seconds:
            return {"ok": False, "seconds": elapsed, "battery_c": temp, "target_c": target_c}
        time.sleep(min(30, max(1, max_seconds - elapsed)))


def measure(serial, package, layer, seconds, raw_path, pan=False):
    pan_command = """
  if [ $((i % 2)) -eq 1 ]; then
    echo "===PAN left $i==="; input swipe 1900 650 1000 650 450
  else
    echo "===PAN right $i==="; input swipe 1000 650 1900 650 450
  fi
""" if pan else ""
    remote = f"""
end=$(( $(date +%s) + {int(seconds)} )); i=0
while [ "$(date +%s)" -lt "$end" ]; do
  i=$((i+1))
  echo "===SAMPLE $i $(date +%s)==="
  dumpsys SurfaceFlinger --timestats -disable -clear >/dev/null 2>&1
  dumpsys SurfaceFlinger --timestats -enable >/dev/null 2>&1
{pan_command}  sleep 5
  echo "===FRAME $i==="
  dumpsys SurfaceFlinger --timestats -dump -maxlayers 24
  dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1
  if [ $((i % 4)) -eq 1 ]; then
    echo "===SF $i==="; dumpsys SurfaceFlinger --displays
    echo "===BATTERY $i==="; dumpsys battery
    echo "===THERMAL $i==="; dumpsys thermalservice
    echo "===FOCUS $i==="; dumpsys window | grep -i -E 'mCurrentFocus|mFocusedApp' | head -8
  fi
done
"""
    with open(raw_path, "w", encoding="utf-8") as handle:
        result = subprocess.run(["adb", "-s", serial, "shell", remote], stdout=handle,
                                stderr=subprocess.PIPE, text=True, errors="replace",
                                timeout=seconds + 180)
    if result.returncode:
        raise RuntimeError(f"measurement adb failed ({result.returncode}): {result.stderr[-1000:]}")
    return raw_path


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int((len(values) - 1) * q)))]


def temp_fields(block, name):
    match = re.search(r"Temperature\{mValue=([\d.-]+), mType=\d+, mName=" + re.escape(name) + r", mStatus=(\d+)\}", block)
    return (float(match.group(1)), int(match.group(2))) if match else (None, None)


def analyze(raw_path, package, csv_path):
    text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    sample_starts = list(re.finditer(r"^===SAMPLE (\d+) (\d+)===\s*$", text, re.M))
    rows, intervals = [], []
    for index, match in enumerate(sample_starts):
        number, wall = int(match.group(1)), int(match.group(2))
        end = sample_starts[index + 1].start() if index + 1 < len(sample_starts) else len(text)
        block = text[match.end():end]
        frame_match = re.search(r"^===FRAME \d+===\s*$(.*?)(?=^===|\Z)", block, re.M | re.S)
        frame_count = 0
        display_on_ms = 0
        if frame_match:
            frame_text = frame_match.group(1)
            display_on = re.search(r"^displayOnTime = (\d+)", frame_text, re.M)
            display_on_ms = int(display_on.group(1)) if display_on else 0
            for layer_block in frame_text.split("layerName = ")[1:]:
                name = layer_block.splitlines()[0]
                if package in name and "(BLAST)" in name:
                    total = re.search(r"^totalFrames = (\d+)", layer_block, re.M)
                    if total:
                        frame_count = max(frame_count, int(total.group(1)))
        fps = frame_count / (display_on_ms / 1000) if display_on_ms > 0 else None
        active = re.search(r"activeFrameRateMode=\{fps=([\d.]+) Hz", block)
        battery = re.search(r"^  temperature: (\d+)", block, re.M)
        thermal = re.search(r"Thermal Status: (\d+)", block)
        hal_block = block.split("Current temperatures from HAL:", 1)[1] if "Current temperatures from HAL:" in block else block
        bat, bat_status = temp_fields(hal_block, "battery")
        skin, skin_status = temp_fields(hal_block, "skin")
        cpu_values = [float(value) for value in re.findall(r"Temperature\{mValue=([\d.-]+), mType=0, mName=CPU\d+, mStatus=\d+\}", hal_block)]
        gpu_values = [float(value) for value in re.findall(r"Temperature\{mValue=([\d.-]+), mType=1, mName=GPU\d+, mStatus=\d+\}", hal_block)]
        focus = "FOCUS" not in block or package in block
        rows.append({
            "sample": number, "wall": wall, "fps": fps, "frame_count": frame_count,
            "active_hz": float(active.group(1)) if active else None,
            "battery_service_c": int(battery.group(1)) / 10 if battery else None,
            "battery_hal_c": bat, "skin_c": skin,
            "cpu_max_c": max(cpu_values) if cpu_values else None,
            "gpu_max_c": max(gpu_values) if gpu_values else None,
            "thermal_status": int(thermal.group(1)) if thermal else None,
            "foreground": focus,
        })
    fields = list(rows[0].keys()) if rows else ["sample", "wall", "fps"]
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    valid = [row for row in rows if row["foreground"] and row["fps"] is not None]
    fps_values = [row["fps"] for row in valid]
    active_values = [row["active_hz"] for row in rows if row["active_hz"] is not None]
    summary = {
        "fps_source": "SurfaceFlinger timestats totalFrames/displayOnTime",
        "frame_interval_source": "not_available_from_timestats",
        "samples": len(rows), "valid_samples": len(valid),
        "valid_ratio": len(valid) / len(rows) if rows else 0,
        "wall_duration_s": rows[-1]["wall"] - rows[0]["wall"] if len(rows) > 1 else None,
        "fps_mean": statistics.mean(fps_values) if fps_values else None,
        "fps_p50": percentile(fps_values, .5), "fps_p05": percentile(fps_values, .05),
        "fps_min": min(fps_values) if fps_values else None,
        "frame_interval_p50_ms": percentile(intervals, .5),
        "frame_interval_p95_ms": percentile(intervals, .95),
        "active_hz_values": sorted(set(active_values)),
        "active_hz_p50": percentile(active_values, .5),
    }
    for field in ("battery_hal_c", "skin_c", "cpu_max_c", "gpu_max_c"):
        values = [row[field] for row in rows if row[field] is not None]
        summary[field] = {
            "mean": statistics.mean(values) if values else None,
            "max": max(values) if values else None,
            "last": values[-1] if values else None,
        }
    statuses = [row["thermal_status"] for row in rows if row["thermal_status"] is not None]
    summary["thermal_status_max"] = max(statuses) if statuses else None
    summary["comparison_valid"] = summary["valid_ratio"] >= .8 and len(valid) >= 30
    return {"summary": summary, "rows": rows}


def restore_screen(serial, original):
    adb(serial, "shell", "input keyevent KEYCODE_HOME", check=False)
    time.sleep(1)
    if original != "Awake":
        adb(serial, "shell", "input keyevent KEYCODE_SLEEP", check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default="AVXB6R5C19000481")
    parser.add_argument("--package", required=True)
    parser.add_argument("--layer", required=True)
    parser.add_argument("--seconds", type=int, default=1200)
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--out", required=True)
    parser.add_argument("--probe", action="store_true", help="sample current foreground only; change nothing")
    parser.add_argument("--pan", action="store_true", help="repeat identical left/right camera swipes in every arm")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.probe:
        layer = resolve_layer(args.serial, args.package, args.layer)
        raw = measure(args.serial, args.package, layer, args.seconds, out / "probe_samples.raw", pan=args.pan)
        analysis = analyze(raw, args.package, out / "probe_samples.csv")
        (out / "probe_analysis.json").write_text(json.dumps(analysis["summary"], indent=2), encoding="utf-8")
        print(json.dumps(analysis["summary"], indent=2))
        return

    original_screen = wakefulness(args.serial)
    original_mode = None
    report = {"device": args.serial, "package": args.package, "layer": args.layer,
              "seconds_per_arm": args.seconds, "original_screen": original_screen,
              "repeatable_camera_pan": args.pan, "order": ["智能", "高"], "arms": {}, "restore": {}}
    try:
        adb(args.serial, "shell", "input keyevent KEYCODE_WAKEUP")
        adb(args.serial, "shell", "wm dismiss-keyguard", check=False)
        adb(args.serial, "shell", "input swipe 636 2200 636 600 300")
        time.sleep(2)
        original_mode = read_mode(args.serial, out / "original_mode")
        report["original_mode"] = original_mode
        (out / "original_mode.json").write_text(json.dumps({"mode": original_mode}), encoding="utf-8")
        initial_temp = battery_c(args.serial)
        report["initial_battery_c"] = initial_temp
        for index, mode in enumerate(("智能", "高")):
            arm = "smart" if mode == "智能" else "high"
            report["arms"][arm] = {"set_mode": set_mode(args.serial, out / f"{arm}_mode", mode)}
            launch_game(args.serial, args.package, args.settle)
            layer = resolve_layer(args.serial, args.package, args.layer)
            report["arms"][arm]["layer"] = layer
            raw = measure(args.serial, args.package, layer, args.seconds, out / f"{arm}_samples.raw", pan=args.pan)
            analysis = analyze(raw, args.package, out / f"{arm}_samples.csv")
            report["arms"][arm]["analysis"] = analysis["summary"]
            (out / f"{arm}_analysis.json").write_text(json.dumps(analysis["summary"], indent=2), encoding="utf-8")
            (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            if index == 0 and initial_temp is not None:
                report["cooldown"] = cooldown(args.serial, args.package, initial_temp, out)
                (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        if original_mode:
            try:
                report["restore"]["mode"] = set_mode(args.serial, out / "restore_mode", original_mode)
            except Exception as error:
                report["restore"]["mode_error"] = repr(error)
        try:
            restore_screen(args.serial, original_screen)
            report["restore"]["screen"] = wakefulness(args.serial)
        except Exception as error:
            report["restore"]["screen_error"] = repr(error)
        adb(args.serial, "shell", f"rm -f {UI_REMOTE}", check=False)
        report["restore"]["original_screen"] = original_screen
        report["restore"]["original_mode"] = original_mode
        (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (out / "restore.json").write_text(json.dumps(report["restore"], indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
