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

  function visible(el) {
    return el.getBoundingClientRect().width > 0;
  }

  function fits(found) {
    var doc = document.documentElement;
    var over = doc.scrollWidth - doc.clientWidth;
    if (over > 0) found.push("the document scrolls sideways by " + over + "px");
    /* A rule that sets display beats the hidden attribute unless the stylesheet
       says otherwise: an empty question box would sit above the terminal. */
    document.querySelectorAll("[hidden]").forEach(function (el) {
      if (visible(el)) found.push("shown though hidden: " + name(el));
    });
    document.querySelectorAll("body *").forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > doc.clientWidth + 1 && !contained(el)) {
        found.push("past the edge: " + name(el) + " ends at " + Math.round(r.right));
      }
    });
    /* Something poking out of its panel doesn't reach the window's edge on a
       desktop, so the check above misses it: a field 100% wide plus its own
       padding sits in the panel's padding instead. */
    document.querySelectorAll(".panel").forEach(function (panel) {
      var box = panel.getBoundingClientRect();
      var style = getComputedStyle(panel);
      var inner = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
      panel.querySelectorAll("*").forEach(function (el) {
        var r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > inner + 1 && !contained(el)) {
          found.push("outside its panel: " + name(el) + " ends at " + Math.round(r.right) +
                     ", panel content at " + Math.round(inner));
        }
      });
    });
    /* Text hanging out of its own box doesn't widen the document, so the
       checks above never see it. */
    document.querySelectorAll("button, a.button, .status, .rowname, .rowmeta, .who, .notice, label, .callout p, code.block, .tab")
      .forEach(function (el) {
        if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX === "visible" &&
            getComputedStyle(el).display !== "inline") {
          found.push("text outside its box: " + name(el) +
                     " content " + el.scrollWidth + " box " + el.clientWidth);
        }
      });
  }

  /* "Buttons don't line up" was the owner's first complaint, and it can be
     measured. A group of buttons on one line shares a top edge and a height, and
     every row in a list ends its actions at the same right edge. */
  function linesUp(found) {
    document.querySelectorAll(".actions, .formactions").forEach(function (group) {
      /* Buttons whose heights overlap are on one line; a line that wrapped below
         doesn't overlap, so it's left out of the comparison. */
      var rects = [];
      group.querySelectorAll("button, a.button").forEach(function (button) {
        if (visible(button)) { rects.push(button.getBoundingClientRect()); }
      });
      rects.forEach(function (r, i) {
        rects.slice(0, i).forEach(function (other) {
          var sameLine = r.top < other.bottom && other.top < r.bottom;
          if (sameLine && (Math.abs(r.top - other.top) > 1 || Math.abs(r.height - other.height) > 1)) {
            found.push("buttons out of line in: " + name(group) + " (top " + Math.round(r.top) + "/" +
                       Math.round(other.top) + ", tall " + Math.round(r.height) + "/" + Math.round(other.height) + ")");
          }
        });
      });
    });
    document.querySelectorAll(".rows").forEach(function (list) {
      var right = list.getBoundingClientRect().right;
      list.querySelectorAll(".row > .actions").forEach(function (group) {
        if (visible(group) && Math.abs(group.getBoundingClientRect().right - right) > 1) {
          found.push("row actions don't reach the right edge: " + name(group));
        }
      });
    });
  }

  window.addEventListener("load", function () {
    var found = [];
    fits(found);
    linesUp(found);
    /* The popup has no geometry until it opens: fill it with the longest words
       on the page, and measure that too. */
    var dialog = document.getElementById("confirm");
    if (dialog) {
      var longest = "";
      document.querySelectorAll("[data-confirm]").forEach(function (form) {
        if (form.dataset.confirm.length > longest.length) { longest = form.dataset.confirm; }
      });
      document.getElementById("confirm-text").textContent = longest;
      dialog.showModal();
      var box = dialog.getBoundingClientRect();
      if (box.left < 0 || box.right > document.documentElement.clientWidth + 1) {
        found.push("the confirm runs off the screen: " + Math.round(box.left) + " to " + Math.round(box.right));
      }
      dialog.querySelectorAll("p, button").forEach(function (el) {
        var r = el.getBoundingClientRect();
        if (r.right > box.right + 1 || r.left < box.left - 1) {
          found.push("outside the confirm: " + name(el));
        }
      });
      linesUp(found);
      dialog.close();
    }
    document.title = "VC" + JSON.stringify(found);
  });
})();
