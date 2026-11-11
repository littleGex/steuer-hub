/* Steuer Hub: progress, pipeline animation and small celebrations.
   Plain JS, no dependencies. Everything degrades to the static page without it. */
(function () {
  "use strict";

  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var STATIONS = ["dropoff", "reading", "review", "filed"];

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function store(key, val) {
    try {
      if (val === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, val);
    } catch (e) { /* private mode or blocked storage: carry on without it */ }
    return null;
  }
  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  /* ---------- confetti and toast ---------- */
  function confetti(count) {
    if (reduced) return;
    var box = document.createElement("div");
    box.className = "confetti";
    box.setAttribute("aria-hidden", "true");
    var colors = ["#0a84ff", "#30d158", "#ff9f0a", "#ff375f", "#bf5af2", "#64d2ff"];
    for (var i = 0; i < (count || 70); i++) {
      var p = document.createElement("i");
      p.style.left = Math.random() * 100 + "%";
      p.style.background = colors[i % colors.length];
      p.style.setProperty("--x", (Math.random() * 160 - 80) + "px");
      p.style.setProperty("--r", (Math.random() * 900 - 450) + "deg");
      p.style.setProperty("--d", (1.4 + Math.random() * 1.4) + "s");
      p.style.animationDelay = Math.random() * 0.35 + "s";
      box.appendChild(p);
    }
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 3600);
  }

  function toast(text) {
    var n = document.querySelectorAll(".toast").length;
    var t = document.createElement("div");
    t.className = "toast";
    t.setAttribute("role", "status");
    t.style.setProperty("--n", n);
    t.textContent = text;
    document.body.appendChild(t);
    requestAnimationFrame(function () { requestAnimationFrame(function () { t.classList.add("show"); }); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 400);
    }, 4200);
  }

  /* ---------- achievements: announce only what is new since last visit ---------- */
  function checkEarned(payload) {
    if (!payload || !payload.earned) return;
    var key = "steuer.earned." + payload.year;
    var seen = store(key);
    var now = payload.earned.map(function (a) { return a.key; });
    if (seen === null) { store(key, JSON.stringify(now)); return; }  // first visit: set the baseline silently
    var old;
    try { old = JSON.parse(seen); } catch (e) { old = []; }
    var fresh = payload.earned.filter(function (a) { return old.indexOf(a.key) === -1; });
    if (!fresh.length) return;
    store(key, JSON.stringify(now));
    fresh.forEach(function (a, i) {
      setTimeout(function () { toast(a.icon + " Unlocked: " + a.title); }, i * 700);
    });
    confetti(fresh.length > 1 ? 110 : 70);
  }

  /* ---------- live partial refresh of [data-live] blocks ---------- */
  var refreshing = false;
  function refreshLive() {
    if (refreshing || !document.querySelector("[data-live]")) return Promise.resolve();
    refreshing = true;
    return fetch(location.href, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html) return;
        var doc = new DOMParser().parseFromString(html, "text/html");
        document.querySelectorAll("[data-live]").forEach(function (el) {
          if (!el.id) return;
          var fresh = doc.getElementById(el.id);
          if (fresh && fresh.innerHTML !== el.innerHTML) el.innerHTML = fresh.innerHTML;
        });
      })
      .catch(function () { /* next poll will try again */ })
      .then(function () { refreshing = false; });
  }

  /* ---------- the pipeline ---------- */
  function initPipeline() {
    var root = document.getElementById("pipeline");
    if (!root) return;
    var year = root.dataset.year;
    var lane = root.querySelector(".lane");
    var worker = root.querySelector(".worker");
    var msg = document.getElementById("pipeline-msg");
    var stEls = {};
    STATIONS.forEach(function (k) { stEls[k] = root.querySelector('.station[data-station="' + k + '"]'); });

    var initial = readJSON("pipeline-data");
    if (!initial) return;

    var maxKey = "steuer.maxdoc." + year;
    var storedMax = parseInt(store(maxKey), 10);
    var maxId = function (docs) { return docs.reduce(function (m, d) { return Math.max(m, d.id); }, 0); };

    var shown = {};
    var latest = initial;
    var known = {};           // doc id -> station, as last seen
    var curX = 0, idleTimer = null, queue = Promise.resolve(), lastSig = null, inflight = false;

    function setMsg(t) { if (msg) msg.textContent = t; }

    function render(dropKey) {
      STATIONS.forEach(function (k) {
        var el = stEls[k];
        if (!el) return;
        el.querySelector(".count").textContent = shown[k];
        var box = el.querySelector(".papers");
        box.textContent = "";
        var n = Math.min(Math.max(shown[k], 0), 5);
        for (var i = 0; i < n; i++) {
          var p = document.createElement("span");
          p.className = "paper" + (k === "filed" ? " ok" : k === "review" && latest.open_items ? " warn" : "");
          if (dropKey === k && i === n - 1) p.classList.add("drop-in");
          box.appendChild(p);
        }
        el.classList.toggle("busy", k === "reading" && shown.reading > 0 && !latest.__idleReading);
        el.classList.toggle("has-warn", k === "review" && latest.open_items > 0);
      });
    }

    function stationX(k) {
      var s = stEls[k].getBoundingClientRect(), l = lane.getBoundingClientRect();
      return s.left - l.left + s.width / 2 - worker.offsetWidth / 2;
    }

    function wake() {
      worker.classList.remove("sleeping");
      clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        if (!worker.classList.contains("offline")) { worker.classList.add("sleeping"); setMsg("The worker is resting."); }
      }, 8000);
    }

    function place(x) { curX = x; worker.style.transform = "translateX(" + x + "px)"; }

    function walkTo(x) {
      if (reduced || Math.abs(x - curX) < 2 || !worker.animate) { place(x); return Promise.resolve(); }
      worker.classList.toggle("left", x < curX);
      worker.classList.add("walking");
      var from = curX;
      var a = worker.animate(
        [{ transform: "translateX(" + from + "px)" }, { transform: "translateX(" + x + "px)" }],
        { duration: Math.min(1400, 350 + Math.abs(x - from) * 2.2), easing: "ease-in-out", fill: "forwards" }
      );
      return a.finished.catch(function () {}).then(function () {
        place(x);
        a.cancel();
        worker.classList.remove("walking");
      });
    }

    function stampFiled() {
      var s = stEls.filed && stEls.filed.querySelector(".stamp-hit");
      if (!s) return;
      s.classList.remove("show");
      void s.offsetWidth;
      s.classList.add("show");
      confetti(40);
      setTimeout(function () { s.classList.remove("show"); }, 1100);
    }

    function runJob(job) {
      var from = job.from, to = job.to;
      var fi = from === null ? 0 : STATIONS.indexOf(from);
      var ti = STATIONS.indexOf(to);
      wake();
      setMsg((job.label || "A document") + (from === null ? " arrived." : " moves on."));
      var chain = walkTo(stationX(STATIONS[fi]));
      if (from === null) {
        chain = chain.then(function () { shown.dropoff++; render("dropoff"); return sleep(450); });
      }
      if (ti === fi) return chain.then(function () { if (from !== null) { shown[from]--; shown[to]++; render(to); } });
      chain = chain.then(function () {
        shown[STATIONS[fi]]--; render();
        worker.classList.add("carrying");
      });
      for (var i = fi + 1; i <= ti; i++) {
        (function (idx) {
          chain = chain.then(function () {
            return walkTo(stationX(STATIONS[idx])).then(function () {
              if (STATIONS[idx] === "reading" && idx < ti) { return sleep(700); }
            });
          });
        })(i);
      }
      return chain.then(function () {
        worker.classList.remove("carrying");
        shown[to]++; render(to);
        if (to === "filed") stampFiled();
        else if (to === "reading") return sleep(500);
      });
    }

    function enqueue(jobs) {
      jobs.forEach(function (job) { queue = queue.then(function () { return runJob(job); }).catch(function () {}); });
      queue = queue.then(function () {
        STATIONS.forEach(function (k) { shown[k] = latest.stations[k]; });
        render();
        if (!jobs.length) return;
        setMsg("All caught up.");
        wake();
      });
    }

    function diff(docs, isInitial) {
      var jobs = [];
      docs.forEach(function (d) {
        var prev = known[d.id];
        if (prev === undefined) {
          if (!isInitial || (isFinite(storedMax) && d.id > storedMax)) jobs.push({ from: null, to: d.station, label: d.label });
        } else if (prev !== d.station) {
          jobs.push({ from: prev, to: d.station, label: d.label });
        }
        known[d.id] = d.station;
      });
      return jobs.slice(0, 4);
    }

    function applyEarned(p) { checkEarned({ year: p.year, earned: p.earned }); }

    /* initial state: documents that appeared since this browser last looked animate in */
    var jobs0 = [];
    var base = {};
    STATIONS.forEach(function (k) { base[k] = initial.stations[k]; });
    if (isFinite(storedMax)) {
      initial.docs.forEach(function (d) { if (d.id > storedMax) base[d.station]--; });
      jobs0 = diff(initial.docs, true);
      var skipped = initial.docs.filter(function (d) { return d.id > storedMax; }).length - jobs0.length;
      if (skipped > 0) { jobs0.forEach(function () {}); }
    } else {
      initial.docs.forEach(function (d) { known[d.id] = d.station; });
    }
    store(maxKey, String(Math.max(storedMax || 0, maxId(initial.docs))));
    // anything beyond the animated jobs is simply shown as already there
    var animated = {};
    jobs0.forEach(function (j) { animated[j.to] = (animated[j.to] || 0) + 1; });
    STATIONS.forEach(function (k) { shown[k] = base[k]; });
    initial.docs.forEach(function (d) {
      if (isFinite(storedMax) && d.id > storedMax && !jobs0.some(function (j) { return j.label === d.label && j.to === d.station; })) shown[d.station]++;
    });
    lastSig = initial.sig;
    render();
    place(stationX("dropoff"));
    if (initial.worker !== "ok") worker.classList.add("offline");
    wake();
    if (jobs0.length) enqueue(jobs0);

    window.addEventListener("resize", function () { place(stationX(STATIONS[0]) > 0 ? curX : 0); });

    function poll() {
      if (inflight || document.visibilityState !== "visible") return;
      inflight = true;
      fetch("/api/status?year=" + encodeURIComponent(year), { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("status " + r.status); return r.json(); })
        .then(function (data) {
          worker.classList.remove("offline");
          if (data.worker !== "ok" || !data.nas_ok) {
            worker.classList.add("offline");
            setMsg(data.nas_ok ? "The background worker is not running." : "The NAS is not reachable.");
          }
          latest = data;
          if (data.sig !== lastSig) {
            lastSig = data.sig;
            enqueue(diff(data.docs, false));
            store(maxKey, String(Math.max(parseInt(store(maxKey), 10) || 0, maxId(data.docs))));
            refreshLive();
            applyEarned(data);
          }
        })
        .catch(function () { worker.classList.add("offline"); setMsg("Cannot reach the server."); })
        .then(function () { inflight = false; });
    }

    setInterval(poll, 4000);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") poll(); });
  }

  /* ---------- page wiring ---------- */
  function init() {
    checkEarned(readJSON("earned-data"));
    if (document.body.dataset.celebrate === "signoff") { confetti(120); }
    document.querySelectorAll('form[action="/upload"]').forEach(function (f) {
      f.addEventListener("submit", function () {
        var b = f.querySelector("button.primary");
        if (b) setTimeout(function () { b.disabled = true; b.textContent = "Uploading…"; }, 0);
      });
    });
    initPipeline();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
