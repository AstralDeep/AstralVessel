// Draws AstralProjection chart placeholders inside the sandboxed preview, mirroring Projection's client.js,
// then reports the document height to the parent page so the frame fits its content.
(function () {
  var palette = ["#6366F1", "#8B5CF6", "#06B6D4", "#10B981", "#F59E0B", "#EF4444", "#EC4899", "#3B82F6"];
  function draw() {
    if (typeof Plotly === "undefined") return;
    var els = document.querySelectorAll(".astral-chart");
    for (var i = 0; i < els.length; i++) {
      var el = els[i], spec;
      if (el.dataset.rendered) continue;
      try { spec = JSON.parse(el.dataset.chart || "{}"); } catch (e) { continue; }
      var kind = el.dataset.chartType, traces;
      var layout = {
        autosize: true, height: 300, margin: { l: 44, r: 16, t: 16, b: 48 },
        paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { color: "#9CA3AF" },
        xaxis: { gridcolor: "rgba(255,255,255,0.1)", tickfont: { size: 11 }, automargin: true },
        yaxis: { gridcolor: "rgba(255,255,255,0.1)", tickfont: { size: 11 }, automargin: true }
      };
      if (kind === "bar") traces = [{ x: spec.labels, y: spec.data, type: "bar", marker: { color: "#6366F1" } }];
      else if (kind === "line") traces = [{ x: spec.labels, y: spec.data, type: "scatter", mode: "lines+markers", marker: { color: "#6366F1" }, line: { color: "#6366F1", width: 2 } }];
      else if (kind === "pie") {
        traces = [{ values: spec.data, labels: spec.labels, type: "pie", hole: 0.4, textinfo: "label+percent", marker: { colors: spec.colors && spec.colors.length ? spec.colors : palette } }];
        layout.showlegend = true;
      } else if (kind === "plotly") { traces = spec.data || []; Object.assign(layout, spec.layout || {}); }
      else continue;
      Plotly.newPlot(el, traces, layout, { displayModeBar: false, responsive: true });
      el.dataset.rendered = "1";
    }
  }
  function report() {
    parent.postMessage({ astralPreviewHeight: document.documentElement.scrollHeight }, "*");
  }
  window.addEventListener("load", function () { draw(); report(); setTimeout(report, 400); setTimeout(report, 1500); });
  new ResizeObserver(report).observe(document.body);
})();
