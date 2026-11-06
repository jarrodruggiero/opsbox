/* The probe. It runs inside the page under test and writes what it found
   into the document title, the one channel headless Chrome hands back through
   --dump-dom. Read by test_visual.py; adapted from Stocktake's. */
(function () {
  "use strict";

  function name(el) {
    var cls = typeof el.className === "string" ? el.className.split(" ")[0] : "";
    var text = (el.textContent || "").trim().slice(0, 30);
    return el.tagName.toLowerCase() + (cls ? "." + cls : "") + (el.id ? "#" + el.id : "") +
      (text ? " \"" + text + "\"" : "");
  }

  /* Crossing the edge is only a fault if nothing between the element and the
     page scrolls or clips it: the terminal clips its own content on purpose. */
  function contained(el) {
    for (var p = el.parentElement; p; p = p.parentElement) {
      var o = getComputedStyle(p).overflowX;
      if (o === "auto" || o === "scroll" || o === "hidden") return true;
    }
    return false;
  }

  /* Inside a shut <details>, apart from its summary, nothing is drawn, and
     Chrome reports sizes for it that mean nothing. The open pass measures it. */
  function hidden(el) {
    var shut = el.closest("details:not([open])");
    return shut !== null && !el.closest("summary");
  }

  function measure(when, found) {
    var doc = document.documentElement;
    var over = doc.scrollWidth - doc.clientWidth;
    if (over > 0) found.push(when + ": the document scrolls sideways by " + over + "px");
    document.querySelectorAll("body *").forEach(function (el) {
      if (hidden(el)) return;
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > doc.clientWidth + 1 && !contained(el)) {
        found.push(when + ": past the edge: " + name(el) + " ends at " + Math.round(r.right));
      }
    });
    /* Something poking out of its card doesn't reach the window's edge on a
       desktop, so the check above misses it: a field 100% wide plus its own
       padding sits in the card's padding instead. */
    document.querySelectorAll(".card").forEach(function (card) {
      var box = card.getBoundingClientRect();
      var style = getComputedStyle(card);
      var inner = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
      card.querySelectorAll("*").forEach(function (el) {
        if (hidden(el)) return;
        var r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > inner + 1 && !contained(el)) {
          found.push(when + ": outside its card: " + name(el) + " ends at " + Math.round(r.right) +
                     ", card content at " + Math.round(inner));
        }
      });
    });

    /* Text hanging out of its own box doesn't widen the document, so the
       checks above never see it. */
    document.querySelectorAll("button, a.button, summary.button, .badge, td, th, .detail, .who, .flash, .error, label")
      .forEach(function (el) {
        if (hidden(el)) return;
        if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX === "visible") {
          found.push(when + ": text outside its box: " + name(el) +
                     " content " + el.scrollWidth + " box " + el.clientWidth);
        }
      });
  }

  window.addEventListener("load", function () {
    var found = [];
    measure("shut", found);
    /* Replace and the confirms are <details>, which have no geometry while
       shut, so a paste box wider than a phone would pass the first look. */
    document.querySelectorAll("details").forEach(function (d) { d.open = true; });
    setTimeout(function () {
      measure("open", found);
      document.title = "VC" + JSON.stringify(found);
    }, 60);
  });
})();
