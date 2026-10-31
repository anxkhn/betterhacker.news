(function () {
  "use strict";
  var root = document.documentElement;
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };

  var themes = ["system", "light", "dark"];
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    else root.removeAttribute("data-theme");
  }
  var theme = store.get("bh:theme");
  if (themes.indexOf(theme) < 0) theme = "system";
  applyTheme(theme);
  if (store.get("bh:compact") === "1") root.setAttribute("data-density", "compact");

  function ready() {
    var $ = function (s, r) { return (r || document).querySelector(s); };
    var items = Array.prototype.slice.call(document.querySelectorAll(".story"));
    var q = $("#q"), count = $("#count"), none = $("#none");
    var savedBtn = $("#saved-only"), savedCount = $("#saved-count");
    var compactBtn = $("#compact"), themeBtn = $("#theme");
    var baseCount = count ? count.textContent : "";
    var saved = {};
    try { saved = JSON.parse(store.get("bh:saved") || "{}") || {}; } catch (e) { saved = {}; }
    var onlySaved = false, current = -1;

    function setTheme() {
      themeBtn.textContent = "Theme: " + theme;
      themeBtn.setAttribute("aria-label", "Theme: " + theme + ". Activate to change.");
    }
    themeBtn.addEventListener("click", function () {
      theme = themes[(themes.indexOf(theme) + 1) % themes.length];
      store.set("bh:theme", theme);
      applyTheme(theme);
      setTheme();
    });
    setTheme();

    function setCompact(on) {
      if (on) root.setAttribute("data-density", "compact"); else root.removeAttribute("data-density");
      compactBtn.setAttribute("aria-pressed", String(on));
      store.set("bh:compact", on ? "1" : "0");
    }
    compactBtn.setAttribute("aria-pressed", String(store.get("bh:compact") === "1"));
    compactBtn.addEventListener("click", function () {
      setCompact(compactBtn.getAttribute("aria-pressed") !== "true");
    });

    function persist() { store.set("bh:saved", JSON.stringify(saved)); }
    function paintSaved() {
      var n = 0;
      items.forEach(function (li) {
        var on = !!saved[li.dataset.id];
        if (on) n++;
        var b = $(".save", li);
        b.setAttribute("aria-pressed", String(on));
        b.textContent = on ? "Saved" : "Save";
      });
      savedCount.textContent = String(n);
    }

    function visible() { return items.filter(function (li) { return !li.hidden; }); }

    function filter() {
      var term = q.value.trim().toLowerCase();
      var words = term ? term.split(/\s+/) : [];
      var shown = 0;
      items.forEach(function (li) {
        var hay = li.dataset.search || "";
        var ok = words.every(function (w) { return hay.indexOf(w) >= 0; });
        if (onlySaved && !saved[li.dataset.id]) ok = false;
        li.hidden = !ok;
        if (ok) shown++;
      });
      var filtering = !!term || onlySaved;
      items.forEach(function (li, i) { li.classList.toggle("lead", i === 0 && !filtering); });
      if (none) none.hidden = shown > 0 || items.length === 0;
      if (count) {
        count.textContent = filtering ? shown + " of " + items.length + " stories match." : baseCount;
      }
      current = -1;
      items.forEach(function (li) { li.classList.remove("current"); });
    }

    function select(i) {
      var v = visible();
      if (!v.length) return;
      i = Math.max(0, Math.min(v.length - 1, i));
      items.forEach(function (li) { li.classList.remove("current"); });
      var li = v[i];
      li.classList.add("current");
      li.focus({ preventScroll: true });
      li.scrollIntoView({ block: "nearest" });
      current = i;
    }

    function toggleSave(li) {
      var id = li.dataset.id;
      if (saved[id]) delete saved[id]; else saved[id] = 1;
      persist();
      paintSaved();
      if (onlySaved) filter();
    }

    q.addEventListener("input", filter);
    savedBtn.addEventListener("click", function () {
      onlySaved = savedBtn.getAttribute("aria-pressed") !== "true";
      savedBtn.setAttribute("aria-pressed", String(onlySaved));
      filter();
    });
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".save");
      if (b) toggleSave(b.closest(".story"));
    });

    document.addEventListener("keydown", function (e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var t = e.target, tag = t && t.tagName;
      if (e.key === "Escape" && t === q) { q.blur(); return; }
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
      var v = visible();
      var li = current >= 0 ? v[current] : null;
      switch (e.key) {
        case "j": e.preventDefault(); select(current + 1); break;
        case "k": e.preventDefault(); select(current < 0 ? 0 : current - 1); break;
        case "/": e.preventDefault(); q.focus(); q.select(); break;
        case "o": case "Enter":
          if (li && (e.key === "o" || t === li)) { e.preventDefault(); window.open($(".link", li).href, "_blank", "noopener"); }
          break;
        case "c":
          if (li) { e.preventDefault(); window.open($(".disc", li).href, "_blank", "noopener"); }
          break;
        case "s": if (li) { e.preventDefault(); toggleSave(li); } break;
      }
    });

    items.forEach(function (li, i) {
      li.addEventListener("focusin", function () {
        var v = visible(), idx = v.indexOf(li);
        if (idx >= 0) {
          current = idx;
          items.forEach(function (x) { x.classList.remove("current"); });
          li.classList.add("current");
        }
      });
    });

    var rtf = typeof Intl !== "undefined" && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat("en", { numeric: "auto" }) : null;
    function ago(d) {
      var s = Math.round((d.getTime() - Date.now()) / 1000), a = Math.abs(s);
      var units = [["day", 86400], ["hour", 3600], ["minute", 60]];
      for (var i = 0; i < units.length; i++) {
        if (a >= units[i][1]) {
          var n = Math.round(s / units[i][1]);
          return rtf ? rtf.format(n, units[i][0]) : Math.abs(n) + " " + units[i][0] + "s ago";
        }
      }
      return "just now";
    }
    Array.prototype.forEach.call(document.querySelectorAll("time[data-time]"), function (el) {
      var raw = el.getAttribute("data-time");
      var d = /^\d+(\.\d+)?$/.test(raw) ? new Date(Number(raw) * 1000) : new Date(raw);
      if (isNaN(d.getTime())) return;
      el.textContent = ago(d);
      el.setAttribute("datetime", d.toISOString());
      el.title = d.toLocaleString();
    });

    var main = $("#main"), notice = $("#notice");
    var generated = main && main.getAttribute("data-generated");
    function poll() {
      if (document.hidden || !generated || !notice) return;
      fetch("/api/status", { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          var g = j && (j.generated_at || (j.data && j.data.generated_at));
          if (g && g !== generated) notice.hidden = false;
        })
        .catch(function () {});
    }
    setInterval(poll, 300000);

    paintSaved();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
  else ready();
})();
