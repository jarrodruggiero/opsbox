/* Loaded as a file (the CSP allows no inline script) after the page's own
   scripts are queued, so it sees every violation they cause. Reports into
   the title for test_visual.py's terminal test. */
(function () {
  "use strict";
  var violations = [];
  document.addEventListener("securitypolicyviolation", function (ev) {
    violations.push(ev.violatedDirective + " " + (ev.blockedURI || "inline"));
  });
  window.addEventListener("load", function () {
    setTimeout(function () {
      var rows = document.querySelector(".xterm-rows");
      document.title = "VC" + JSON.stringify({
        violations: violations,
        rows: rows ? rows.textContent : null,
        font: rows ? getComputedStyle(rows).fontFamily : null
      });
    }, 1500);
  });
})();
