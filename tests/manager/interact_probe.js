/* Drives the real page in a real browser, with the real scripts under the real
   Content-Security-Policy, and reports what it saw into the title. The server in
   test_visual.py records what reached it. Loaded as a file, because the CSP
   allows no inline script. */
(function () {
  "use strict";
  var report = { violations: [] };
  document.addEventListener("securitypolicyviolation", function (ev) {
    report.violations.push(ev.violatedDirective + " " + (ev.blockedURI || "inline"));
  });

  function finish() { document.title = "VC" + JSON.stringify(report); }
  function byId(id) { return document.getElementById(id); }

  function typeInto(pane, text) {
    var area = document.querySelector(pane + " .xterm-helper-textarea");
    area.focus();
    area.value = text;
    area.dispatchEvent(new InputEvent("input", { data: text, inputType: "insertText", bubbles: true }));
  }

  // What a browser does when a tab goes to the background and comes back.
  function setHidden(hidden) {
    Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  }

  /* Run the steps in order, each once its condition holds or after a while. */
  function steps(list) {
    var waited = 0;
    (function next() {
      if (!list.length) { finish(); return; }
      var step = list[0];
      if (step.when && !step.when() && waited < 7000) {
        waited += 100;
        setTimeout(next, 100);
        return;
      }
      waited = 0;
      list.shift();
      if (step.run) { step.run(); }
      setTimeout(next, step.pause || 200);
    })();
  }

  function signedInPage() {
    return [
      { when: function () { return document.querySelector("#pane-rc .xterm-rows"); }, run: function () {
        var rows = document.querySelector("#pane-rc .xterm-rows");
        var style = getComputedStyle(rows);
        report.font = style.fontFamily;
        report.color = style.color;
      }, pause: 800 },
      { run: function () {
        // A danger button opens the popup and sends nothing, and Cancel sends nothing.
        document.querySelector('form[action="/ssh/remove"] button').click();
        var dialog = byId("confirm");
        report.dialogOpen = dialog.open;
        report.dialogText = byId("confirm-text").textContent;
        report.dialogOk = byId("confirm-ok").textContent;
        dialog.querySelector('button[value="cancel"]').click();
        report.closedByCancel = !dialog.open;
      } },
      { run: function () { typeInto("#pane-rc", "y"); } },
      // The status poll brings the question without a reload.
      { when: function () { return !byId("rc-question").hidden; }, run: function () {
        report.question = byId("rc-question-text").textContent;
        report.word = document.querySelector("#rc-status .word").textContent;
        report.stateClass = byId("rc-status").className;
      } },
      { run: function () { document.querySelector('form[action="/rc/answer"] button').click(); } },
      { run: function () { byId("tab-shell").click(); }, pause: 800 },
      { when: function () { return document.querySelector("#pane-shell .xterm-helper-textarea"); }, run: function () {
        report.rcHidden = byId("pane-rc").hidden;
        report.shellHidden = byId("pane-shell").hidden;
        report.actionsHidden = byId("rc-actions").hidden;
        report.shellSelected = byId("tab-shell").getAttribute("aria-selected");
      } },
      { run: function () { typeInto("#pane-shell", "z"); } },
      // Away and back: the stream closes and reopens, and this time the server says the shell has ended.
      { run: function () { setHidden(true); }, pause: 300 },
      { run: function () { setHidden(false); }, pause: 800 },
      // Ended: a key goes nowhere, and Enter opens a new shell, which takes typing.
      { run: function () { typeInto("#pane-shell", "q"); } },
      { run: function () { typeInto("#pane-shell", "\r"); }, pause: 800 },
      { run: function () { typeInto("#pane-shell", "w"); } },
      // Back to Remote Control, whose stream the server refuses once: the next status brings it back.
      { run: function () { byId("tab-rc").click(); }, pause: 3500 },
      { run: function () {
        // Confirmed this time: the popup's own button lets the form go.
        var form = document.querySelector('form[action="/env/remove"]');
        form.querySelector("button").click();
        byId("confirm-ok").click();
        report.stillHere = true;
      } }
    ];
  }

  function signingInPage() {
    return [
      { run: function () {
        var code = byId("code");
        code.value = "pasted-code-123";
        code.dispatchEvent(new Event("paste", { bubbles: true }));
        report.pasted = true;
      } },
      { run: function () {
        var finish = byId("code-form").querySelector("button");
        report.finishText = finish.textContent;
        report.finishDisabled = finish.disabled;
      } }
    ];
  }

  window.addEventListener("load", function () {
    setTimeout(function () {
      steps(document.querySelector("#code") ? signingInPage() : signedInPage());
    }, 200);
  });
})();
