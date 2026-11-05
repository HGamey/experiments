// 3DGS 手机基准的页面侧计时 (配合 bench.py 给 antimatter15/splat 打的补丁使用)。
// 查看器每画一帧调用 __gsb.frame(now, vertexCount, total)，每完成一次排序调用 __gsb.sort()。
// 全部高斯加载完并排过序后的第一帧记为加载完成，此时把相机轨迹时间归零，
// 预热 warm 秒后统计 win 秒的 requestAnimationFrame 间隔，结果 POST 给宿主 /result。
(function () {
  const q = new URLSearchParams(location.search);
  const run = q.get("run") || "run";
  const warm = Number(q.get("warm") || 10) * 1000;
  const win = Number(q.get("win") || 60) * 1000;
  const S = { phase: "loading", t0: null, loadedAt: null, intervals: [], sorts: 0, sortsWin: 0,
              last: null, winStart: null, firstFrameAt: null, maxSeen: 0 };
  window.__gsb = S;
  const post = (path, body) => fetch(path, { method: "POST", body: JSON.stringify(body),
                                             headers: { "content-type": "application/json" } }).catch(() => {});
  const glInfo = () => {
    try {
      const c = document.getElementById("canvas");
      const gl = c.getContext("webgl2");
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      return { renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
               vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
               canvas: [c.width, c.height], css: [innerWidth, innerHeight], dpr: devicePixelRatio };
    } catch (e) { return { error: String(e) }; }
  };
  post("/event", { run, ev: "start", ua: navigator.userAgent });
  window.addEventListener("error", (e) => post("/event", { run, ev: "error", msg: String(e.message) }));
  S.sort = () => { S.sorts++; if (S.phase === "measure") S.sortsWin++; };
  S.frame = (now, vc, total) => {
    if (now === undefined) return;
    if (S.firstFrameAt === null && vc > 0) S.firstFrameAt = now;
    S.maxSeen = Math.max(S.maxSeen, vc);
    if (S.phase === "loading") {
      if (total > 0 && vc >= total && S.loadDone) {
        S.phase = "warm"; S.loadedAt = now; S.t0 = Date.now();
        post("/event", { run, ev: "loaded", load_ms: now, first_frame_ms: S.firstFrameAt, total });
      }
    } else if (S.phase === "warm") {
      if (now - S.loadedAt >= warm) {
        S.phase = "measure"; S.winStart = now; S.last = now; S.sortsWin = 0;
        post("/event", { run, ev: "measure_start" });
      }
    } else if (S.phase === "measure") {
      S.intervals.push(now - S.last); S.last = now;
      if (now - S.winStart >= win) {
        S.phase = "done";
        const ents = performance.getEntriesByType("resource").filter((r) => /\.splat/.test(r.name));
        const r = ents.length ? ents[ents.length - 1] : null;
        post("/result", { run, total, load_ms: S.loadedAt, first_frame_ms: S.firstFrameAt,
                          download_ms: r ? r.responseEnd : null, bytes: r ? r.transferSize || r.encodedBodySize : null,
                          win_ms: now - S.winStart, sorts_win: S.sortsWin, intervals: S.intervals.map((x) => Math.round(x * 100) / 100),
                          gl: glInfo(), ua: navigator.userAgent });
      }
    }
  };
  setInterval(() => post("/event", { run, ev: "hb", phase: S.phase, max_seen: S.maxSeen, sorts: S.sorts }), 5000);
})();
