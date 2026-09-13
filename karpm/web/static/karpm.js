// Two small things, neither worth a framework: keep the status panel current,
// and add or remove a search block without a round trip.
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


// Add and remove search blocks. Each block's inputs are named "<key>-<n>"; the
// server reads whatever numbers arrive and renumbers on save, so a gap left by
// a removed block is harmless and nothing has to be re-indexed here.
(function () {
  var list = document.getElementById("searches");
  var add = document.getElementById("add-search");
  var template = document.getElementById("search-template");
  if (!list || !add || !template) return;

  var next = list.querySelectorAll("section.search").length;

  add.addEventListener("click", function () {
    var html = template.innerHTML.replace(/INDEX/g, String(next++));
    var holder = document.createElement("div");
    holder.innerHTML = html;
    var block = holder.firstElementChild;
    list.appendChild(block);
    // Scroll it clear of the save bar pinned to the bottom of the window.
    block.scrollIntoView({ block: "center", behavior: "smooth" });
    var first = block.querySelector("input:not([type=checkbox])");
    if (first) first.focus({ preventScroll: true });
  });

  // Removing drops the block, so its inputs are simply not submitted. Nothing
  // is deleted until the form is saved, and the listings it already found keep
  // their rows in the database either way.
  list.addEventListener("click", function (event) {
    var button = event.target.closest("[data-remove]");
    if (!button) return;
    var block = button.closest("section.search");
    if (!block) return;
    if (!confirm("Remove this search? Its listings and their history stay in the database.")) return;
    block.remove();
  });
})();
