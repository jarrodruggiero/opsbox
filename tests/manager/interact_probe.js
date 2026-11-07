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

  // A file dialog can't be driven, so what it would hand back is set directly.
  function pick(input, files) {
    Object.defineProperty(input, "files", { value: files, configurable: true, writable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function inFolder(name, text, relative) {
    var file = new File([text], name);
    Object.defineProperty(file, "webkitRelativePath", { value: relative });
    return file;
  }

  function idle() {
    var button = document.querySelector("#upload-form button.primary");
    return !button.disabled;
  }

  function uploadPage() {
    var fileInput = byId("file-input");
    var folderInput = document.querySelector("#upload-form input[webkitdirectory]");
    var noticeText = function () { return byId("notice").textContent; };
    var submit = function () { document.querySelector("#upload-form button.primary").click(); };
    return [
      { run: function () {
        report.heading = document.querySelector("#files h2").textContent;
        report.nativeHidden = fileInput.hidden;
        report.buttons = Array.prototype.map.call(byId("file-pickers").querySelectorAll("button"), function (b) { return b.textContent; });
        report.nothing = byId("file-picked").textContent;
        report.fileLabel = document.querySelector('label[for="choose-files"]').textContent;
        var tops = Array.prototype.map.call(byId("file-pickers").querySelectorAll("button"), function (b) { return b.getBoundingClientRect().top; });
        report.buttonsLineUp = tops[0] === tops[1];
        report.listScrolls = getComputedStyle(document.querySelector("#files .rows")).overflowY;
        report.pickersShown = !byId("file-pickers").hidden;
        // Upload with nothing chosen opens the dialog, rather than sending nothing.
        var opened = function (input, act) {
          var clicked = false;
          var watch = function () { clicked = true; };
          input.addEventListener("click", watch);
          act();
          input.removeEventListener("click", watch);
          return clicked;
        };
        report.emptyOpensDialog = opened(fileInput, submit);
        report.folderPickerClicked = opened(folderInput, function () { byId("choose-folder").click(); });
        report.filesPickerClicked = opened(fileInput, function () { byId("choose-files").click(); });
      } },
      // One file.
      { run: function () {
        pick(fileInput, [new File(["one"], "a.txt")]);
        report.onePicked = byId("file-picked").textContent;
        report.onePath = byId("file-path").value;
        report.oneLabel = byId("path-label").textContent;
        byId("file-path").value = "~/renamed.txt";
        submit();
      } },
      { when: idle, run: function () { report.oneNotice = noticeText(); } },
      // The path follows what's chosen, until it's typed over.
      { run: function () {
        pick(fileInput, [new File(["a"], "a.txt")]);
        pick(fileInput, [new File(["a"], "a.txt"), new File(["b"], "b.txt")]);
        report.followsChoice = byId("file-path").value;
        byId("file-path").value = "~/mine/";
        pick(fileInput, [new File(["a"], "a.txt")]);
        report.keepsTyped = byId("file-path").value;
      } },
      // Two files, and a folder given without its slash.
      { run: function () {
        document.querySelector("#files .rows").dataset.stale = "1";
        report.consoleEl = document.querySelector("#pane-rc .xterm-rows");
        byId("file-path").value = "";
        pick(fileInput, [new File(["b"], "b.txt"), new File(["c"], "c.txt")]);
        report.manyPicked = byId("file-picked").textContent;
        report.manyPath = byId("file-path").value;
        report.manyLabel = byId("path-label").textContent;
        byId("file-path").value = "~/.kube";
        report.progress = [];
        var button = document.querySelector("#upload-form button.primary");
        new MutationObserver(function () { report.progress.push(button.textContent); }).observe(button, { childList: true });
        submit();
        // Replace waits while an upload runs: it would change what's being sent.
        var replaced = false;
        var watch = function () { replaced = true; };
        fileInput.addEventListener("click", watch);
        document.querySelector("[data-replace]").click();
        fileInput.removeEventListener("click", watch);
        report.replacedMidUpload = replaced;
      } },
      { when: function () { return idle() && noticeText().indexOf("2 files") >= 0; }, pause: 500, run: function () {
        report.manyNotice = noticeText();
        report.manyPathAfter = byId("file-path").value;
        report.manyPickedAfter = byId("file-picked").textContent;
        report.uploadLabelAfter = document.querySelector("#upload-form button.primary").textContent;
        report.consoleKept = report.consoleEl === document.querySelector("#pane-rc .xterm-rows");
        delete report.consoleEl;
      } },
      { when: function () { return !document.querySelector("#files .rows").dataset.stale; }, run: function () {
        report.listRefreshed = !document.querySelector("#files .rows").dataset.stale;
      } },
      // A whole folder, with an empty file in it.
      { run: function () {
        pick(folderInput, [inFolder("one.txt", "1", "box/one.txt"), inFolder("empty", "", "box/sub/empty")]);
        report.folderPicked = byId("file-picked").textContent;
        byId("file-path").value = "~/backup/";
        submit();
      } },
      { when: idle },
      // One refused, one fine.
      { run: function () {
        pick(folderInput, [inFolder("x", "x", "refused/x"), inFolder("fine", "y", "fine")]);
        byId("file-path").value = "~/";
        submit();
      } },
      { when: idle, run: function () {
        report.refusedNotice = noticeText();
        report.refusedBad = byId("notice").classList.contains("bad");
      } },
      // Too big, and never sent.
      { run: function () {
        pick(fileInput, [new File([new Uint8Array(1048577)], "huge")]);
        byId("file-path").value = "~/huge";
        submit();
      } },
      { when: idle, run: function () {
        report.hugeNotice = noticeText();
        report.hugeKept = byId("file-picked").textContent;
      } },
      // Refused many times over: three reasons, and how many more.
      { run: function () {
        pick(folderInput, [1, 2, 3, 4, 5].map(function (n) { return inFolder(String(n), "x", "refused/" + n); }));
        byId("file-path").value = "~/";
        submit();
      } },
      { when: idle, run: function () { report.manyRefusedNotice = noticeText(); } },
      // The connection drops.
      { run: function () {
        pick(folderInput, [inFolder("x", "x", "dropped/x")]);
        byId("file-path").value = "~/";
        report.oneInFolder = byId("file-picked").textContent;
        submit();
      } },
      { when: idle, run: function () { report.droppedNotice = noticeText(); } },
      // Too many.
      { run: function () {
        var lots = [];
        for (var i = 0; i < 1001; i++) { lots.push(new File([""], "f" + i)); }
        pick(fileInput, lots);
        report.tooManyNotice = noticeText();
      } },
      // Signed out partway.
      { run: function () {
        pick(folderInput, [inFolder("1", "1", "signedout/1"), inFolder("2", "2", "signedout/2")]);
        byId("file-path").value = "~/";
        submit();
      } },
      { when: idle, run: function () { report.signedOutNotice = noticeText(); } },
      // Replace.
      { run: function () {
        fileInput.addEventListener("click", function () { report.replaceMultiple = fileInput.multiple; }, { once: true });
        document.querySelector("[data-replace]").click();
        pick(fileInput, [new File(["new"], "whatever")]);
        report.multipleAfterReplace = fileInput.multiple;
      }, pause: 500 },
      { when: idle }
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
      var drive = document.querySelector("script[data-drive]").dataset.drive;
      steps(drive === "uploads" ? uploadPage() : document.querySelector("#code") ? signingInPage() : signedInPage());
    }, 200);
  });
})();
