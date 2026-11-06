"use strict";
(function () {
  // b64 decodes a base64 string to a Uint8Array using atob.
  function b64(s) {
    var bin = atob(s);
    var arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return arr;
  }

  // Terminal
  var termEl = document.getElementById("terminal");
  if (termEl && window.Terminal) {
    var term = new Terminal({
      disableStdin: true,
      convertEol: false,
      fontSize: 13,
      scrollback: 5000,
      cursorBlink: false,
    });
    term.open(termEl);

    if (window.FitAddon) {
      var fit = new FitAddon.FitAddon();
      term.loadAddon(fit);
      fit.fit();
      window.addEventListener("resize", function () { fit.fit(); });
    }

    var es = new EventSource("/console");
    es.addEventListener("reset", function (ev) {
      term.reset();
      term.write(b64(ev.data));
    });
    es.onmessage = function (ev) {
      term.write(b64(ev.data));
    };
  }

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

  // Copy buttons
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var orig = btn.textContent;
      copyText(btn.dataset.copy).then(function () {
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = orig; }, 1500);
      }).catch(function () {
        btn.textContent = "Copy failed";
        setTimeout(function () { btn.textContent = orig; }, 1500);
      });
    });
  });

  // Refresh
  if (document.body.dataset.state) {
    setInterval(function () {
      fetch("/api/status")
        .then(function (res) { return res.json(); })
        .then(function (data) {
          var stateMatch = data.rc && data.rc.state === document.body.dataset.state;
          var loginActive = String(data.login && data.login.active ? 1 : 0);
          var loginMatch = loginActive === document.body.dataset.login;
          if (!stateMatch || !loginMatch) {
            // Skip reload if any <details> is open or active element is form control
            var openDetails = document.querySelector("details[open]");
            var active = document.activeElement;
            var isFormControl =
              active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT");
            if (!openDetails && !isFormControl) {
              location.reload();
            }
          }
        })
        .catch(function () {});
    }, 5000);
  }
})();
