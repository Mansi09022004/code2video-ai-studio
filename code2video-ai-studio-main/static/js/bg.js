/* Subtle drifting-dots background. Pauses when hidden, off for reduced-motion. */
(function () {
  try {
    if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var c = document.createElement("canvas");
    c.setAttribute("aria-hidden", "true");
    c.style.cssText = "position:fixed;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none";
    document.body.appendChild(c);
    var ctx = c.getContext("2d"), w = 0, h = 0, dots = [], raf = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
    function size() {
      w = innerWidth; h = innerHeight;
      c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var n = Math.max(24, Math.min(60, Math.round(w * h / 28000)));
      dots = [];
      for (var i = 0; i < n; i++) dots.push({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22, r: Math.random() * 1.3 + .6, t: Math.random() < .7 ? 0 : 1 });
    }
    function frame() {
      ctx.clearRect(0, 0, w, h);
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i];
        d.x += d.vx; d.y += d.vy;
        if (d.x < -10) d.x = w + 10; else if (d.x > w + 10) d.x = -10;
        if (d.y < -10) d.y = h + 10; else if (d.y > h + 10) d.y = -10;
        for (var j = i + 1; j < dots.length; j++) {
          var e = dots[j], dx = d.x - e.x, dy = d.y - e.y, q = dx * dx + dy * dy;
          if (q < 14400) { ctx.strokeStyle = "rgba(148,163,184," + (0.07 * (1 - q / 14400)).toFixed(3) + ")"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(e.x, e.y); ctx.stroke(); }
        }
        ctx.fillStyle = d.t ? "rgba(167,139,250,.35)" : "rgba(45,212,191,.35)";
        ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, 6.2832); ctx.fill();
      }
      raf = requestAnimationFrame(frame);
    }
    function start() { if (!raf) raf = requestAnimationFrame(frame); }
    function stop() { cancelAnimationFrame(raf); raf = 0; }
    document.addEventListener("visibilitychange", function () { document.hidden ? stop() : start(); });
    var rt; addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(size, 200); });
    size(); start();
  } catch (e) {}
})();
