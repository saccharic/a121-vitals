import { useEffect, useRef, useState } from "react";
import Plotly from "plotly.js-dist-min";
import type { Data, Layout } from "plotly.js";

export default function Chart({
  data,
  layout,
  label,
  revision,
}: {
  data: Data[];
  layout: Partial<Layout>;
  label: string;
  revision: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const node = ref.current!;
    const observer = new ResizeObserver(() => {
      if ("data" in node && node.isConnected && node.offsetWidth > 0) {
        // A frame change can unmount the plot before Plotly's queued resize.
        void Promise.resolve(Plotly.Plots.resize(node)).catch(() => {});
      }
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
      Plotly.purge(node);
    };
  }, []);
  useEffect(() => {
    let active = true;
    const node = ref.current!;
    const styles = getComputedStyle(node);
    const panel = styles.getPropertyValue("--panel").trim();
    const ink = styles.getPropertyValue("--ink").trim();
    const secondary = styles.getPropertyValue("--secondary").trim();
    const line = styles.getPropertyValue("--line").trim();
    const axis = {
      gridcolor: styles.getPropertyValue("--grid").trim(),
      zerolinecolor: line,
      tickfont: { size: 11, color: secondary },
      automargin: true,
    };
    void Plotly.react(
      node,
      data,
      {
        autosize: true,
        height: 285,
        paper_bgcolor: panel,
        plot_bgcolor: panel,
        font: { family: "system-ui, sans-serif", color: ink, size: 12 },
        modebar: { bgcolor: panel, color: secondary, activecolor: ink },
        legend: { orientation: "h", x: 1, xanchor: "right", y: 1.12 },
        hovermode: "closest",
        uirevision: revision,
        ...layout,
        margin: { t: 12, r: 20, b: 48, l: 64, ...layout.margin },
        xaxis: { ...axis, ...layout.xaxis },
        yaxis: { ...axis, ...layout.yaxis },
      },
      {
        responsive: true,
        displaylogo: false,
        scrollZoom: false,
        modeBarButtons: [
          [
            "toImage",
            "zoom2d",
            "pan2d",
            "zoomIn2d",
            "zoomOut2d",
            "autoScale2d",
            "resetScale2d",
          ],
        ],
        toImageButtonOptions: {
          filename: "a121-vitals-signal",
          format: "png",
          scale: 2,
        },
      },
    )
      .then(() => {
        if (active) setError(false);
      })
      .catch(() => {
        if (active) setError(true);
      });
    return () => {
      active = false;
    };
  }, [data, layout, revision]);
  return (
    <div className="chart-wrap" role="img" aria-label={label}>
      <div ref={ref} className="plot" />
      {error && <p role="alert">위상 지도를 표시할 수 없습니다.</p>}
    </div>
  );
}
