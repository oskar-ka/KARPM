// The only dynamic behaviour on the site: keep the status panel current without
// reloading the page. Not worth a framework.
(function () {
  var panel = document.querySelector("[data-poll]");
  if (!panel) return;
  var url = panel.getAttribute("data-poll");
  var every = parseInt(panel.getAttribute("data-every") || "10", 10) * 1000;

  function refresh() {
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) panel.innerHTML = html; })
      .catch(function () { /* the daemon or the network is down; try again later */ });
  }
  setInterval(refresh, every);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
})();
