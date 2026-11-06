/* Loaded as a file (the CSP allows no inline script) after the page's own
   scripts are queued, so it sees every violation they cause. Reports into
   the title for test_visual.py's terminal test.

   It reads xterm's styling, not its painted text: xterm paints in animation
   frames, which headless Chrome runs unreliably under virtual time, while
   its <style> elements (what the first CSP blocked) apply at once. */
(function () {
  "use strict";
  var violations = [];
  document.addEventListener("securitypolicyviolation", function (ev) {
    violations.push(ev.violatedDirective + " " + (ev.blockedURI || "inline"));
  });
  var waited = 0;
  function report() {
    var rows = document.querySelector(".xterm-rows");
    if (!rows && waited < 6000) {
      waited += 200;
      setTimeout(report, 200);
      return;
    }
    var style = rows ? getComputedStyle(rows) : null;
    document.title = "VC" + JSON.stringify({
      violations: violations,
      rows: !!rows,
      font: style ? style.fontFamily : null,
      color: style ? style.color : null
    });
  }
  window.addEventListener("load", function () { setTimeout(report, 200); });
})();
