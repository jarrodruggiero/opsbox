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

  // Whether doing act clicks input, the way opening its dialog does.
  function opened(input, act) {
    var clicked = false;
    var watch = function () { clicked = true; };
    input.addEventListener("click", watch);
    act();
    input.removeEventListener("click", watch);
    return clicked;
  }

  function leavingIsWarned() {
    var leaving = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(leaving);
    return leaving.defaultPrevented;
  }

  function uploadPage() {
    var fileInput = byId("file-input");
    var folderInput = document.querySelector("#upload-form input[webkitdirectory]");
    var result = function () { var box = byId("upload-result"); return box.hidden ? "" : box.textContent; };
    var chosen = function () { return byId("file-picked").textContent; };
    var path = function (value) { if (value !== undefined) { byId("file-path").value = value; } return byId("file-path").value; };
    var submit = function () { document.querySelector("#upload-form button.primary").click(); };
    var replace = function () { document.querySelector("[data-replace]").click(); };
    var markList = function () { document.querySelector("#files .rows").dataset.stale = "1"; };
    var redrawn = function () { return !document.querySelector("#files .rows").dataset.stale; };
    return [
      { run: function () {
        report.heading = document.querySelector("#files h2").textContent;
        report.nativeHidden = fileInput.hidden;
        report.buttons = Array.prototype.map.call(byId("file-pickers").querySelectorAll("button"), function (b) { return b.textContent; });
        report.nothing = chosen();
        report.fileLabel = document.querySelector('label[for="choose-files"]').textContent;
        var tops = Array.prototype.map.call(byId("file-pickers").querySelectorAll("button"), function (b) { return b.getBoundingClientRect().top; });
        report.buttonsLineUp = tops[0] === tops[1];
        report.listScrolls = getComputedStyle(document.querySelector("#files .rows")).overflowY;
        report.pickersShown = !byId("file-pickers").hidden;
        report.resultHidden = byId("upload-result").hidden;
        // Upload with nothing chosen opens the dialog, rather than sending nothing.
        report.emptyOpensDialog = opened(fileInput, submit);
        report.folderPickerClicked = opened(folderInput, function () { byId("choose-folder").click(); });
        report.filesPickerClicked = opened(fileInput, function () { byId("choose-files").click(); });
      } },
      // One file, from the top of the page, as after a page load.
      { run: function () {
        pick(fileInput, [new File(["one"], "a.txt")]);
        report.onePicked = chosen();
        report.onePath = path();
        report.oneLabel = byId("path-label").textContent;
        path("~/renamed.txt");
        window.scrollTo(0, 0);
        submit();
      } },
      { when: idle, run: function () {
        report.oneResult = result();
        var box = byId("upload-result").getBoundingClientRect();
        // A pixel to spare: edges land on fractions.
        report.resultInView = box.top >= 0 && box.bottom <= window.innerHeight + 1;
      } },
      // The path follows what's chosen, until it's typed over.
      { run: function () {
        pick(fileInput, [new File(["a"], "a.txt")]);
        report.resultAfterChoice = result();
        pick(fileInput, [new File(["a"], "a.txt"), new File(["b"], "b.txt")]);
        report.followsChoice = path();
        path("~/mine/");
        pick(fileInput, [new File(["a"], "a.txt")]);
        report.keepsTyped = path();
      } },
      // Two files, and a folder given without its slash.
      { run: function () {
        markList();
        report.consoleEl = document.querySelector("#pane-rc .xterm-rows");
        path("");
        pick(fileInput, [new File(["b"], "b.txt"), new File(["c"], "c.txt")]);
        report.manyPicked = chosen();
        report.manyPath = path();
        report.manyLabel = byId("path-label").textContent;
        path("~/.kube");
        report.progress = [];
        var button = document.querySelector("#upload-form button.primary");
        new MutationObserver(function () { report.progress.push(button.textContent); }).observe(button, { childList: true });
        submit();
        // While it runs, nothing can change what's being sent, and leaving warns.
        report.busyPickers = byId("choose-files").disabled && byId("choose-folder").disabled;
        report.busyPath = byId("file-path").readOnly;
        report.leavingWarned = leavingIsWarned();
        report.replacedMidUpload = opened(fileInput, replace);
      } },
      { when: function () { return idle() && result().indexOf("2 files") >= 0; }, pause: 500, run: function () {
        report.manyResult = result();
        report.manyPathAfter = path();
        report.manyPickedAfter = chosen();
        report.uploadLabelAfter = document.querySelector("#upload-form button.primary").textContent;
        report.consoleKept = report.consoleEl === document.querySelector("#pane-rc .xterm-rows");
        delete report.consoleEl;
        report.idleAgain = !byId("choose-files").disabled && !byId("choose-folder").disabled && !byId("file-path").readOnly;
        report.leavingAfter = leavingIsWarned();
      } },
      { when: redrawn, run: function () { report.listRefreshed = redrawn(); } },
      // A whole folder, with an empty file in it.
      { run: function () {
        pick(folderInput, [inFolder("one.txt", "1", "box/one.txt"), inFolder("empty", "", "box/sub/empty")]);
        report.folderPicked = chosen();
        path("~/backup/");
        submit();
      } },
      { when: idle },
      // One refused, one fine: what was saved is listed, and the choice is cleared.
      { run: function () {
        markList();
        pick(folderInput, [inFolder("x", "x", "refused/x"), inFolder("fine", "y", "fine")]);
        path("~/");
        submit();
      } },
      { when: idle, run: function () {
        report.refusedResult = result();
        report.refusedBad = byId("upload-result").classList.contains("bad");
        report.partialPicked = chosen();
        report.partialPath = path();
      } },
      { when: redrawn, run: function () { report.partialRedrawn = redrawn(); } },
      // With a Remove's confirm open, the list stays: the form it answers for would go.
      { run: function () {
        markList();
        pick(fileInput, [new File(["u"], "under-confirm")]);
        path("~/under-confirm");
        submit();
        document.querySelector('#files form[action="/files/remove"] button').click();
        report.confirmOpen = byId("confirm").open;
      } },
      { when: idle, pause: 800, run: function () {
        report.keptUnderConfirm = !redrawn();
        byId("confirm").querySelector('button[value="cancel"]').click();
      } },
      // Too big is never sent; exactly the limit is.
      { run: function () {
        pick(fileInput, [new File([new Uint8Array(1048577)], "huge")]);
        path("~/huge");
        submit();
      } },
      { when: idle, run: function () {
        report.hugeResult = result();
        report.hugeKept = chosen();
        pick(fileInput, [new File([new Uint8Array(1048576)], "exact")]);
        path("~/exact");
        submit();
      } },
      { when: idle },
      // Refused many times over: three reasons, and how many more.
      { run: function () {
        pick(folderInput, [1, 2, 3, 4, 5].map(function (n) { return inFolder(String(n), "x", "refused/" + n); }));
        path("~/");
        submit();
      } },
      { when: idle, run: function () { report.manyRefusedResult = result(); } },
      // The connection drops.
      { run: function () {
        pick(folderInput, [inFolder("x", "x", "dropped/x")]);
        path("~/");
        report.oneInFolder = chosen();
        submit();
      } },
      { when: idle, run: function () { report.droppedResult = result(); } },
      // Too many: refused, and what was chosen before stays.
      { run: function () {
        pick(fileInput, [new File(["k"], "keep.txt")]);
        path("~/kept.txt");
        var lots = [];
        for (var i = 0; i < 1001; i++) { lots.push(new File([""], "f" + i)); }
        pick(fileInput, lots);
        report.tooManyResult = result();
        report.tooManyKeptPicked = chosen();
        report.tooManyKeptPath = path();
      } },
      // Nowhere to go.
      { run: function () {
        pick(fileInput, [new File(["a"], "blank-a"), new File(["b"], "blank-b")]);
        path("");
        submit();
        report.blankResult = result();
        pick(fileInput, [new File(["x"], "lone")]);
        path("");
        submit();
      } },
      { when: idle, run: function () { report.blankOneResult = result(); } },
      // Signed out partway.
      { run: function () {
        pick(folderInput, [inFolder("1", "1", "signedout/1"), inFolder("2", "2", "signedout/2")]);
        path("~/");
        submit();
      } },
      { when: idle, run: function () { report.signedOutResult = result(); } },
      // Replace picks one file, to its row's path, and leaves what's chosen alone.
      { run: function () {
        pick(fileInput, [new File(["d1"], "d1"), new File(["d2"], "d2")]);
        path("~/dest/");
        var seeMultiple = function () { report.replaceMultiple = fileInput.multiple; fileInput.removeEventListener("click", seeMultiple); };
        fileInput.addEventListener("click", seeMultiple);
        replace();
        pick(fileInput, [new File(["new"], "whatever")]);
        report.multipleAfterReplace = fileInput.multiple;
      } },
      { when: idle, run: function () {
        report.pendingPicked = chosen();
        report.pendingPath = path();
        submit();
      } },
      { when: idle },
      // A refused Replace leaves nothing for the next Upload to send.
      { run: function () {
        replace();
        pick(fileInput, [new File(["refuse-me"], "empty")]);
      } },
      { when: idle, run: function () {
        report.refusedReplaceResult = result();
        report.refusedReplacePath = path();
        report.refusedReplacePicked = chosen();
        report.refusedReplaceOpensDialog = opened(fileInput, submit);
      } },
      // Replace, then cancel: what was chosen and typed is still there.
      { run: function () {
        pick(fileInput, [new File(["e1"], "e1"), new File(["e2"], "e2")]);
        path("~/cancelled/");
        replace();
        fileInput.dispatchEvent(new Event("cancel"));
        report.cancelMultiple = fileInput.multiple;
        report.cancelPath = path();
        report.cancelPicked = chosen();
        pick(fileInput, [new File(["f"], "after-cancel")]);
        report.afterCancelPicked = chosen();
      } },
      // Replace with no cancel event, then Choose files: a plain choice, not a Replace.
      { run: function () {
        replace();
        byId("choose-files").click();
        pick(fileInput, [new File(["g"], "no-cancel")]);
        report.noCancelPicked = chosen();
      }, pause: 400 },
      // A browser with no folder picker hands back plain files.
      { run: function () {
        pick(folderInput, [new File(["p"], "plain.txt")]);
        report.plainFromFolder = chosen();
        report.plainFromFolderLabel = byId("path-label").textContent;
        pick(folderInput, []);
        report.emptyFolderResult = result();
      } }
    ];
  }

  // From an empty list: the first file's row appears.
  function firstUploadPage() {
    return [
      { run: function () {
        report.emptyBefore = Boolean(document.querySelector("#files .empty"));
        pick(byId("file-input"), [new File(["k"], "config")]);
        byId("file-path").value = "~/.kube/config";
        document.querySelector("#upload-form button.primary").click();
      } },
      { when: function () { return document.querySelector("#files .rows"); }, run: function () {
        report.emptyAfter = Boolean(document.querySelector("#files .empty"));
        report.firstRow = document.querySelector("#files .rows .rowname").textContent;
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
      var drive = document.querySelector("script[data-drive]").dataset.drive;
      var drives = { uploads: uploadPage, "first-upload": firstUploadPage };
      steps(drives[drive] ? drives[drive]() : document.querySelector("#code") ? signingInPage() : signedInPage());
    }, 200);
  });
})();
