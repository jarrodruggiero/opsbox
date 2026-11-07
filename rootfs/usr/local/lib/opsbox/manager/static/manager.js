/* The live half of the manager page: the terminals, keeping the state current
   without a reload, the confirm, and uploading several files at once. Everything
   else on the page is a plain form that works without this file, one file's
   upload included. */
(function () {
  "use strict";

  var tokenMeta = document.querySelector('meta[name="csrf-token"]');
  var csrf = tokenMeta ? tokenMeta.content : "";

  function post(path, fields) {
    var form = new URLSearchParams(fields || {});
    form.set("csrf", csrf);
    return fetch(path, { method: "POST", body: form, headers: { "X-Opsbox-Fetch": "1" } });
  }

  function notice(text, bad) {
    var box = document.getElementById("notice");
    if (!box) { return; }
    box.textContent = text;
    box.classList.toggle("bad", Boolean(bad));
    box.hidden = false;
  }

  /* ── the confirm: a form with data-confirm asks first, in its own words ── */

  var dialog = document.getElementById("confirm");
  var waiting = null;

  function ask(form) {
    waiting = form;
    document.getElementById("confirm-text").textContent = form.dataset.confirm;
    document.getElementById("confirm-ok").textContent = form.querySelector("button").textContent;
    dialog.showModal();
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.getAttribute("method") === "dialog") {
      // The popup's own buttons say which, and it closes itself. Not its close
      // event: Chrome fires that in an animation frame, which may never come.
      var asked = waiting;
      waiting = null;
      if (asked && event.submitter && event.submitter.value === "ok") {
        asked.dataset.confirmed = "1";
        setTimeout(function () { asked.requestSubmit(); }, 0);
      }
      return;
    }
    if (form.dataset.confirm && form.dataset.confirmed !== "1" && dialog) {
      event.preventDefault();
      ask(form);
      return;
    }
    delete form.dataset.confirmed;
    if (form.hasAttribute("data-fetch")) {
      // A button that changes Remote Control leaves the page, and so the
      // terminal and its scrollback, where they are.
      event.preventDefault();
      fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { "X-Opsbox-Fetch": "1" }
      }).then(function (response) {
        if (response.ok) { return null; }
        return response.json().then(function (reply) { notice(reply.error, true); });
      }).catch(function () {}).then(refresh);
    }
  });

  /* ── signing in: pasting the code is the last step ─────────────────────── */

  var code = document.getElementById("code");
  if (code) {
    code.addEventListener("paste", function () {
      // After the paste has landed in the field.
      setTimeout(function () {
        if (code.value.trim().length >= 8) { code.form.requestSubmit(); }
      }, 0);
    });
    // Back from the claude.ai tab, ready for the paste.
    window.addEventListener("focus", function () { code.focus(); });
    code.form.addEventListener("submit", function () {
      var finish = code.form.querySelector("button");
      finish.textContent = "Signing in\u2026";
      finish.disabled = true;
      code.readOnly = true;
    });
  }

  /* ── small things ──────────────────────────────────────────────────────── */

  var faded = document.querySelector("[data-fades]");
  if (faded) { setTimeout(function () { faded.hidden = true; }, 5000); }

  // navigator.clipboard only exists in a secure context, and this page is
  // usually plain HTTP on a LAN address, so fall back to selecting the text.
  function copyText(text) {
    if (window.isSecureContext && navigator.clipboard) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "offscreen";
      document.body.appendChild(area);
      area.select();
      var copied = false;
      try { copied = document.execCommand("copy"); } catch (err) { copied = false; }
      document.body.removeChild(area);
      if (copied) { resolve(); } else { reject(new Error("copy refused")); }
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var label = button.textContent;
      function show(text) {
        button.textContent = text;
        setTimeout(function () { button.textContent = label; }, 1500);
      }
      copyText(button.dataset.copy).then(function () { show("Copied"); }).catch(function () { show("Copy failed"); });
    });
  });

  /* ── uploading: several files or a folder, each file its own request ───── */

  var uploadForm = document.getElementById("upload-form");

  if (uploadForm) {
    // More than this is more likely the wrong folder than a plan.
    var MAX_PICK = 1000;
    var fileInput = document.getElementById("file-input");
    var filePath = document.getElementById("file-path");
    var pathLabel = document.getElementById("path-label");
    var pickedText = document.getElementById("file-picked");
    var uploadResult = document.getElementById("upload-result");
    var uploadButton = uploadForm.querySelector("button.primary");
    var chooseFiles = document.getElementById("choose-files");
    var chooseFolder = document.getElementById("choose-folder");
    var folderInput = document.createElement("input");
    var nothingPicked = pickedText.textContent;
    var pathWord = pathLabel.textContent;
    var pathExample = filePath.placeholder;
    var picked = [];
    var pickedFolder = false;
    var suggested = "";
    // Where Replace's file goes. Kept apart from what's chosen, so that a Replace,
    // refused or cancelled, leaves the next Upload as it was.
    var replaceTo = null;
    var uploading = false;

    // A browser's dialog picks files or a folder, never both, so there are two.
    // No name on this one: what's sent is put together below, a file at a time.
    folderInput.type = "file";
    folderInput.webkitdirectory = true;
    folderInput.hidden = true;
    uploadForm.appendChild(folderInput);
    fileInput.multiple = true;
    fileInput.required = false;
    filePath.required = false;
    fileInput.hidden = true;
    document.getElementById("file-pickers").hidden = false;
    var fileLabel = document.querySelector('label[for="file-input"]');
    fileLabel.htmlFor = "choose-files";
    fileLabel.textContent = "Files";

    // In the form, by the button: the notice at the top of the page is out of sight.
    var say = function (text, bad) {
      uploadResult.textContent = text;
      uploadResult.classList.toggle("bad", Boolean(bad));
      uploadResult.hidden = false;
      uploadResult.scrollIntoView({ block: "nearest" });
    };

    var forget = function () {
      picked = [];
      pickedFolder = false;
      suggested = "";
      filePath.value = "";
      fileInput.value = "";
      folderInput.value = "";
      pickedText.textContent = nothingPicked;
      pathLabel.textContent = pathWord;
      filePath.placeholder = pathExample;
    };

    var choose = function (list, fromFolder) {
      var files = Array.prototype.slice.call(list);
      if (files.length > MAX_PICK) {
        say("That's " + files.length + " files, and the page takes " + MAX_PICK + " at a time.", true);
        return;
      }
      uploadResult.hidden = true;
      picked = files;
      // A browser with no folder picker hands back plain files.
      pickedFolder = fromFolder && Boolean(files[0].webkitRelativePath);
      var one = files.length === 1 && !pickedFolder;
      var count = files.length + (files.length === 1 ? " file" : " files");
      pickedText.textContent = one ? files[0].name
        : pickedFolder ? files[0].webkitRelativePath.split("/")[0] + ": " + count : count;
      // One file's path is the file's own. Otherwise it's the folder they go in.
      pathLabel.textContent = one ? pathWord : "Folder on the box";
      filePath.placeholder = one ? pathExample : "~/.kube/";
      // Typed over, the path is the person's. Otherwise it follows what's chosen.
      if (!filePath.value || filePath.value === suggested) {
        suggested = one ? "~/" + files[0].name : "~/";
        filePath.value = suggested;
      }
      filePath.focus();
    };

    // What to send for what's chosen, or null when it has nowhere to go.
    var jobsForChoice = function () {
      if (picked.length === 1 && !pickedFolder) {
        return [{ file: picked[0], path: filePath.value, folder: false }];
      }
      var folder = filePath.value.trim();
      if (!folder) { return null; }
      if (folder.slice(-1) !== "/") { folder += "/"; }
      return picked.map(function (file) {
        return { file: file, path: folder + (file.webkitRelativePath || file.name), folder: pickedFolder };
      });
    };

    // In place, so that the terminal and its scrollback stay where they are.
    var redrawList = function () {
      fetch("/").then(function (response) {
        return response.ok ? response.text() : null;
      }).then(function (page) {
        if (!page) { return; }
        var list = "#files .rows, #files .empty";
        var fresh = new DOMParser().parseFromString(page, "text/html").querySelector(list);
        var current = document.querySelector(list);
        // Not under an open confirm: the form it answers for would be gone.
        if (fresh && current && !dialog.open) {
          var scrolled = current.scrollTop;
          current.replaceWith(fresh);
          fresh.scrollTop = scrolled;
        }
      }).catch(function () {});
    };

    var busy = function (on) {
      uploading = on;
      uploadButton.disabled = on;
      chooseFiles.disabled = on;
      chooseFolder.disabled = on;
      filePath.readOnly = on;
    };

    // keepChoice: Replace's file, sent without disturbing what's chosen.
    var uploadAll = function (jobs, keepChoice) {
      var label = uploadButton.textContent;
      var limit = Number(uploadForm.dataset.maxFile);
      var refused = [];
      var done = 0;
      var stopped = false;
      busy(true);
      uploadResult.hidden = true;

      var finish = function () {
        busy(false);
        uploadButton.textContent = label;
        if (!refused.length) {
          say(jobs.length === 1 ? "Uploaded." : "Uploaded " + jobs.length + " files.");
        } else {
          // Every reason, for whoever opens the browser's console.
          refused.forEach(function (reason) { console.warn(reason); });
          var more = refused.length > 3 ? " And " + (refused.length - 3) + " more." : "";
          say((done ? "Uploaded " + done + " of " + jobs.length + ". " : "") + refused.slice(0, 3).join(" ") + more, true);
        }
        // With nothing saved the choice stays, so that the path can be corrected.
        if (done) {
          if (!keepChoice) { forget(); }
          redrawList();
        }
      };

      var refuse = function (job, why) {
        refused.push((job.path ? job.path + ": " : "") + why);
      };

      var send = function (job) {
        var body = new FormData();
        body.append("csrf", csrf);
        body.append("path", job.path);
        if (job.folder) { body.append("folder", "1"); }
        body.append("file", job.file);
        return fetch(uploadForm.action, {
          method: "POST",
          body: body,
          headers: { "X-Opsbox-Fetch": "1" }
        }).then(function (response) {
          if (response.ok) {
            done += 1;
            return null;
          }
          // Signed out: every other file would only fail the same way.
          stopped = response.status === 403;
          return response.text().then(function (text) {
            var why = text;
            try { why = JSON.parse(text).error || text; } catch (err) { why = text; }
            refuse(job, why);
          });
        }).catch(function () {
          refuse(job, "Couldn't be sent.");
        });
      };

      var next = function (index) {
        if (index >= jobs.length || stopped) {
          finish();
          return;
        }
        uploadButton.textContent = "Uploading " + (index + 1) + " of " + jobs.length + "…";
        var job = jobs[index];
        // Not sent: it would only be refused, after all of it had gone.
        if (job.file.size > limit) {
          refuse(job, "That file is over the " + limit / 1048576 + " MiB limit.");
          next(index + 1);
          return;
        }
        send(job).then(function () { next(index + 1); });
      };
      next(0);
    };

    var openFiles = function () {
      // A browser may never have said that Replace's dialog was cancelled.
      replaceTo = null;
      fileInput.multiple = true;
      // So that choosing the same file again still counts as a change.
      fileInput.value = "";
      fileInput.click();
    };

    chooseFiles.addEventListener("click", openFiles);
    chooseFolder.addEventListener("click", function () {
      folderInput.value = "";
      folderInput.click();
    });

    // Replace knows where the file goes, so choosing one is the last step. On the
    // document, because uploading redraws the list these buttons are in.
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-replace]");
      if (!button || uploading) { return; }
      replaceTo = button.dataset.replace;
      fileInput.multiple = false;
      fileInput.value = "";
      fileInput.click();
    });

    fileInput.addEventListener("cancel", function () {
      replaceTo = null;
      fileInput.multiple = true;
    });
    fileInput.addEventListener("change", function () {
      if (!fileInput.files.length || uploading) { return; }
      if (replaceTo !== null) {
        var to = replaceTo;
        replaceTo = null;
        fileInput.multiple = true;
        uploadAll([{ file: fileInput.files[0], path: to, folder: false }], true);
        return;
      }
      choose(fileInput.files, false);
    });
    folderInput.addEventListener("change", function () {
      if (uploading) { return; }
      if (folderInput.files.length) {
        choose(folderInput.files, true);
      } else {
        say("That folder has no files.", true);
      }
    });

    uploadForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!picked.length) {
        openFiles();
        return;
      }
      var jobs = jobsForChoice();
      if (jobs) {
        uploadAll(jobs, false);
      } else {
        say("Say where they go, such as ~/.kube/.", true);
      }
    });

    // Leaving now would drop the files not sent yet, with nothing to say so.
    window.addEventListener("beforeunload", function (event) {
      if (uploading) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  var envName = document.getElementById("env-name");
  var envValue = document.getElementById("env-value");
  var envHidden = document.getElementById("env-hidden");
  if (envName && envValue && envHidden) {
    // The server hides these whatever the box says; ticking it shows that early.
    var secretName = /token|secret|passw|passphrase|credential|private|api_?key|_key$|^key$|_pat$|auth$/i;
    envName.addEventListener("input", function () {
      if (secretName.test(envName.value)) { envHidden.checked = true; }
      showHidden();
    });
    envHidden.addEventListener("change", showHidden);
    function showHidden() { envValue.type = envHidden.checked ? "password" : "text"; }
  }

  /* ── the terminals ─────────────────────────────────────────────────────── */

  function b64(text) {
    var raw = atob(text);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { bytes[i] = raw.charCodeAt(i); }
    return bytes;
  }

  function makeTerminal(pane, routes, hooks) {
    var term = new window.Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      scrollback: 5000,
      theme: {
        background: "#0b0f11",
        foreground: "#e6ebec",
        cursor: "#2dd4bf",
        cursorAccent: "#0b0f11",
        selectionBackground: "rgba(45, 212, 191, 0.3)"
      }
    });
    var fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fit) { term.loadAddon(fit); }
    term.open(pane);

    // Ctrl+C stays the terminal's. With text selected, Ctrl+Shift+C copies it.
    // Ctrl+V pastes, as everywhere else on Windows and Linux: left to the browser
    // it becomes the paste event xterm listens for, rather than a typed ^V.
    term.attachCustomKeyEventHandler(function (event) {
      if (event.type === "keydown" && event.ctrlKey && event.shiftKey && event.code === "KeyC" && term.hasSelection()) {
        copyText(term.getSelection()).catch(function () {});
        event.preventDefault();
        return false;
      }
      return !(event.type === "keydown" && event.ctrlKey && !event.altKey && event.code === "KeyV");
    });

    // One request at a time: separate connections could overtake each other,
    // and keystrokes arriving out of order are worse than a little latency.
    var typed = "";
    var sending = false;
    // xterm answers terminal queries it finds in what it's shown, as if typed.
    // In old output replayed after a reload, nobody asked: those answers would
    // land in whatever the process is waiting for now.
    var replaying = false;
    function send() {
      if (sending || !typed) { return; }
      var data = typed.slice(0, 1024);
      typed = typed.slice(data.length);
      sending = true;
      post(routes.input, { data: data }).catch(function () {}).then(function () {
        sending = false;
        send();
      });
    }
    term.onData(function (data) {
      if (replaying || (hooks.onType && hooks.onType(data))) { return; }
      typed += data;
      send();
    });

    var resizing = null;
    term.onResize(function (size) {
      clearTimeout(resizing);
      resizing = setTimeout(function () {
        post(routes.resize, { cols: size.cols, rows: size.rows }).catch(function () {});
      }, 150);
    });

    var source = null;
    return {
      term: term,
      fit: function () { if (fit && !pane.hidden) { fit.fit(); } },
      connected: function () { return source !== null; },
      connect: function () {
        if (source) { return; }
        source = new EventSource(routes.stream);
        source.addEventListener("reset", function (event) {
          term.reset();
          replaying = true;
          term.write(b64(event.data), function () { replaying = false; });
        });
        source.onmessage = function (event) { term.write(b64(event.data)); };
        source.addEventListener("end", function () {
          source.close();
          source = null;
          if (hooks.onEnd) { hooks.onEnd(); }
        });
        // Refused outright rather than dropped, which the browser retries by itself.
        // The next status says whether that's worth another try (see reconnect).
        source.onerror = function () {
          if (source && source.readyState === EventSource.CLOSED) { source = null; }
        };
      },
      disconnect: function () {
        if (source) { source.close(); source = null; }
      }
    };
  }

  var panes = { rc: document.getElementById("pane-rc"), shell: document.getElementById("pane-shell") };
  var tabs = { rc: document.getElementById("tab-rc"), shell: document.getElementById("tab-shell") };
  var terminals = {};
  var active = "rc";
  var shellEnded = false;
  var shellOpening = false;

  function openShell() {
    var shell = terminals.shell;
    shellEnded = false;
    shellOpening = true;
    shell.fit();
    post("/shell/open", { cols: shell.term.cols, rows: shell.term.rows }).then(function (response) {
      shellOpening = false;
      if (response.ok) {
        shell.connect();
      } else {
        response.json().then(function (reply) { shell.term.write("\r\n" + reply.error + "\r\n"); });
      }
    }).catch(function () { shellOpening = false; });
  }

  function shellGone() {
    shellEnded = true;
    terminals.shell.term.write("\r\n\x1b[2mThe shell has ended. Press Enter for a new one.\x1b[0m\r\n");
  }

  // A stream the server refused stays closed, whatever the reason (a proxy's 503
  // during a restart, say), so the status decides: Remote Control's always comes
  // back, and a shell's only if the shell is still there.
  function reconnect(status) {
    var current = terminals[active];
    if (!current || current.connected() || document.hidden) { return; }
    if (active === "rc") {
      current.connect();
    } else if (!shellEnded && !shellOpening) {
      if (status.shell.open) { current.connect(); } else { shellGone(); }
    }
  }

  function terminal(name) {
    if (terminals[name]) { return terminals[name]; }
    if (name === "rc") {
      terminals.rc = makeTerminal(panes.rc, { stream: "/console", input: "/console/input", resize: "/console/resize" }, {});
    } else {
      terminals.shell = makeTerminal(panes.shell, { stream: "/shell/stream", input: "/shell/input", resize: "/shell/resize" }, {
        onEnd: shellGone,
        onType: function (data) {
          if (!shellEnded) { return false; }
          if (data === "\r") { terminals.shell.term.reset(); openShell(); }
          return true;
        }
      });
    }
    return terminals[name];
  }

  function show(name) {
    active = name;
    // Start, Stop and Restart are Remote Control's, not the shell's.
    document.getElementById("rc-actions").hidden = name !== "rc";
    Object.keys(panes).forEach(function (other) {
      panes[other].hidden = other !== name;
      tabs[other].setAttribute("aria-selected", other === name ? "true" : "false");
      // Only the visible terminal holds a stream: a browser allows six
      // connections to a server over plain HTTP, and every tab shares them.
      if (other !== name && terminals[other]) { terminals[other].disconnect(); }
    });
    var isNew = !terminals[name];
    var current = terminal(name);
    current.fit();
    if (name === "shell" && (isNew || !current.connected()) && !shellEnded) {
      openShell();
    } else if (name === "rc") {
      current.connect();
    }
    current.term.focus();
  }

  if (panes.rc && window.Terminal) {
    tabs.rc.addEventListener("click", function () { show("rc"); });
    tabs.shell.addEventListener("click", function () { show("shell"); });
    window.addEventListener("resize", function () {
      if (terminals[active]) { terminals[active].fit(); }
    });
    show("rc");
    // The page opens with its forms in reach, not with the keyboard in a terminal.
    if (document.activeElement) { document.activeElement.blur(); }

    document.addEventListener("visibilitychange", function () {
      // Hidden tabs give their connections back; coming back replays what was missed.
      Object.keys(terminals).forEach(function (name) {
        if (document.hidden) {
          terminals[name].disconnect();
        } else if (name === active && !(name === "shell" && shellEnded)) {
          terminals[name].connect();
        }
      });
      if (!document.hidden) { refresh(); }
    });
  }

  /* ── keeping the state current, without a reload ───────────────────────── */

  function apply(status) {
    var rc = status.rc;
    var badge = document.getElementById("rc-status");
    badge.className = "status state-" + rc.state;
    badge.querySelector(".symbol").textContent = rc.symbol;
    badge.querySelector(".word").textContent = rc.word;
    document.getElementById("rc-detail").textContent = rc.detail;
    document.getElementById("rc-question").hidden = !rc.question;
    document.getElementById("rc-question-text").textContent = rc.question || "";
    // What a click answers: the question on the screen, not whichever comes next.
    document.querySelectorAll('#rc-question input[name="question"]').forEach(function (field) {
      field.value = rc.question || "";
    });
    document.querySelectorAll("[data-show]").forEach(function (form) {
      form.hidden = form.dataset.show.split(" ").indexOf(rc.state) < 0;
    });
    reconnect(status);
    // "Couldn't tell" isn't a change: reloading on one failed check would lose
    // whatever is half typed.
    var signedIn = status.box.loggedIn ? "1" : "0";
    if (!status.box.unknown && signedIn !== document.body.dataset.signedIn && !dialog.open) { location.reload(); }
  }

  // One at a time: a slow answer must not pile up polls until they hold all the
  // browser's connections, and keystrokes queue behind them.
  var polling = false;

  function refresh() {
    if (!document.body.dataset.state || document.hidden || polling) { return; }
    polling = true;
    fetch("/api/status").then(function (response) {
      if (response.status === 401) { location.reload(); return null; }
      return response.ok ? response.json() : null;
    }).then(function (status) {
      if (status) { apply(status); }
    }).catch(function () {}).then(function () { polling = false; });
  }

  if (document.body.dataset.state) { setInterval(refresh, 3000); }
})();
