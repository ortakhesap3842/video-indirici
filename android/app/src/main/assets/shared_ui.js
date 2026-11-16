(function (global) {
  "use strict";

  function formatDuration(sec) {
    if (!sec || sec <= 0) return "";
    var total = Math.floor(sec);
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }

  function isHttpUrl(val) {
    return /^https?:\/\//i.test((val || "").trim());
  }

  /**
   * Renders YouTube search results safely using DOM nodes (no innerHTML).
   * opts: {
   *   rowClass, imgClass, infoClass, actionsClass, btnClass, previewBtnClass,
   *   showPreview, onPreview(item), onDownload(item), onSelect(item)
   * }
   */
  function renderSearchResults(container, items, opts) {
    opts = opts || {};
    container.innerHTML = "";
    var list = Array.isArray(items) ? items : [];
    list.forEach(function (item) {
      var row = document.createElement("div");
      row.className = opts.rowClass || "res-item";

      var img = document.createElement("img");
      img.src = item.thumbnail || "";
      img.alt = item.title || "";
      if (opts.imgClass) img.className = opts.imgClass;

      var info = document.createElement("div");
      info.className = opts.infoClass || "res-info";

      var titleEl = document.createElement(opts.titleTag || "div");
      if (opts.titleClass) titleEl.className = opts.titleClass;
      titleEl.textContent = item.title || "Başlıksız Video";

      var metaEl = document.createElement(opts.metaTag || "div");
      if (opts.metaClass) metaEl.className = opts.metaClass;
      var dur = formatDuration(item.duration);
      metaEl.textContent = (item.uploader || "YouTube") + (dur ? " • " + dur : "");

      info.appendChild(titleEl);
      info.appendChild(metaEl);

      row.appendChild(img);
      row.appendChild(info);

      var actionsWrap = row;
      if (opts.actionsClass) {
        actionsWrap = document.createElement("div");
        actionsWrap.className = opts.actionsClass;
        row.appendChild(actionsWrap);
      }

      if (opts.showPreview && typeof opts.onPreview === "function") {
        var prevBtn = document.createElement("button");
        prevBtn.type = "button";
        prevBtn.className = opts.previewBtnClass || "mini-btn";
        prevBtn.textContent = "Önizle";
        prevBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          opts.onPreview(item);
        });
        actionsWrap.appendChild(prevBtn);
      }

      if (typeof opts.onDownload === "function") {
        var dlBtn = document.createElement("button");
        dlBtn.type = "button";
        dlBtn.className = opts.btnClass || "btn-primary btn-sm";
        if (opts.dlBtnStyle) dlBtn.setAttribute("style", opts.dlBtnStyle);
        dlBtn.textContent = "İndir";
        dlBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          opts.onDownload(item);
        });
        actionsWrap.appendChild(dlBtn);
      }

      if (typeof opts.onSelect === "function") {
        row.addEventListener("click", function () {
          opts.onSelect(item);
        });
      }

      container.appendChild(row);
    });
  }

  global.VideoIndiriciUI = {
    formatDuration: formatDuration,
    isHttpUrl: isHttpUrl,
    renderSearchResults: renderSearchResults,
  };
})(window);
