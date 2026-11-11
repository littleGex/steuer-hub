// Month cards on the Work days page: arrows and typed numbers save automatically.
(function () {
  "use strict";
  var DELAY = 450; // ms to wait for more taps before saving

  function setTotals(t) {
    if (!t) return;
    ["home", "office", "away"].forEach(function (k) {
      var el = document.querySelector('[data-total="' + k + '"]');
      if (el) el.textContent = t[k];
    });
    var box = document.getElementById("totals");
    var meter = document.querySelector("[data-meter]");
    if (box && meter) {
      var max = Number(box.dataset.maxHome) || 1;
      meter.style.width = Math.min(100, Math.round((t.home / max) * 100)) + "%";
    }
  }

  function setMonth(form, m) {
    if (!m) return;
    ["office", "home"].forEach(function (k) {
      var input = form.elements[k];
      input.value = m[k];
      input.dataset.value = m[k];
    });
    var meta = form.querySelector("[data-meta]");
    meta.textContent = m.weekdays + " weekdays · ";
    var free = document.createElement("span");
    free.setAttribute("data-free", "");
    free.textContent = m.free;
    meta.appendChild(free);
    meta.appendChild(document.createTextNode(" free"));
    var a = document.createElement("span");
    a.setAttribute("data-away", "");
    if (m.away) {
      meta.appendChild(document.createTextNode(" · "));
      a.textContent = m.away;
      meta.appendChild(a);
      meta.appendChild(document.createTextNode(" away"));
    } else {
      a.hidden = true;
      meta.appendChild(a);
    }
  }

  function status(form, text, kind) {
    var el = form.querySelector("[data-status]");
    el.textContent = text || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function save(form) {
    var data = new FormData(form);
    form.classList.add("saving");
    status(form, "Saving…");
    fetch(form.action, {
      method: "POST",
      body: new URLSearchParams(data),
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json().catch(function () {
          return { ok: false, error: "Could not save (HTTP " + r.status + ")." };
        });
      })
      .then(function (body) {
        setMonth(form, body.month);
        setTotals(body.totals);
        if (body.ok) {
          status(form, "Saved", "ok");
          setTimeout(function () {
            if (form.querySelector("[data-status]").textContent === "Saved") status(form, "");
          }, 1500);
        } else {
          status(form, body.error || "Could not save.", "err");
          if (!body.month) revert(form);
        }
      })
      .catch(function () {
        status(form, "Offline? Not saved.", "err");
        revert(form);
      })
      .finally(function () {
        form.classList.remove("saving");
      });
  }

  function revert(form) {
    ["office", "home"].forEach(function (k) {
      var input = form.elements[k];
      input.value = input.dataset.value;
    });
  }

  document.querySelectorAll("form[data-month]").forEach(function (form) {
    var timer = null;
    function later() {
      clearTimeout(timer);
      timer = setTimeout(function () { save(form); }, DELAY);
    }

    form.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-step]");
      if (!btn) return;
      var input = form.elements[btn.dataset.kind];
      var v = Math.max(0, (parseInt(input.value, 10) || 0) + Number(btn.dataset.step));
      var max = Number(input.max) || 31;
      input.value = Math.min(v, max);
      status(form, "");
      later();
    });

    form.addEventListener("input", function (e) {
      if (e.target.type !== "number") return;
      if (e.target.value === "") return; // wait until something is typed
      status(form, "");
      later();
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      clearTimeout(timer);
      save(form);
    });

    form.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.type === "number") {
        e.preventDefault();
        clearTimeout(timer);
        save(form);
      }
    });
  });
})();
