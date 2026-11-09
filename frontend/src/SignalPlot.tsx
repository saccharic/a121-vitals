import { useEffect, useMemo, useRef, useState } from "react";

export type Trace = {
  color: string;
  values: (number | null)[];
  mode?: "line" | "markers" | "line+markers";
};

function number(value: number) {
  return Number(value.toPrecision(4)).toString();
}

export default function SignalPlot({
  label,
  x,
  traces,
  xDomain,
  yDomain,
  distanceAxis = false,
  yTicks,
  height = 178,
  cursor,
  onSelect,
}: {
  label: string;
  x: number[];
  traces: Trace[];
  xDomain: [number, number];
  yDomain: [number, number];
  distanceAxis?: boolean;
  yTicks?: ReadonlyArray<readonly [number, string?]>;
  height?: number;
  cursor?: number;
  onSelect?: (value: number) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(350);
  const left = 43;
  const right = width - 14;
  const top = 10;
  const bottom = height - 25;
  const [xMin, xMax] = xDomain;
  const [yMin, yMax] = yDomain;
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;

  useEffect(() => {
    const node = svg.current!;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(130, Math.round(entry.contentRect.width)));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const paths = useMemo(() => {
    return traces.map(({ values, color, mode = "line" }) => {
      let connected = false;
      const points: [number, number][] = [];
      const commands: string[] = [];
      values.forEach((value, index) => {
        if (value === null) {
          connected = false;
          return;
        }
        const px = left + ((x[index] - xMin) / xSpan) * (right - left);
        const py = bottom - ((value - yMin) / ySpan) * (bottom - top);
        if (mode !== "line" || values.length === 1) points.push([px, py]);
        if (mode !== "markers") {
          commands.push(
            (connected ? "L" : "M") + px.toFixed(1) + " " + py.toFixed(1),
          );
        }
        connected = true;
      });
      const radius = mode === "markers" && values.length <= 64 ? 2.7 : 1.65;
      return { color, path: commands.join(" "), points, radius };
    });
  }, [traces, x, xMin, xSpan, yMin, ySpan, right, bottom]);

  const xTicks = distanceAxis
    ? distanceTicks(xMin, xMax, right - left)
    : (xMax > xMin ? [0, 0.25, 0.5, 0.75, 1] : [0]).map(
        (fraction) => xMin + fraction * (xMax - xMin),
      );
  const yAxis: ReadonlyArray<readonly [number, string?]> =
    yTicks ??
    (yMin < 0 && yMax > 0 ? [0, 0.25, 0.5, 0.75, 1] : [0, 1 / 3, 2 / 3, 1]).map(
      (fraction) => [yMin + fraction * (yMax - yMin)] as const,
    );

  const cursorX =
    cursor === undefined
      ? null
      : left + ((cursor - xMin) / xSpan) * (right - left);

  return (
    <svg
      className={onSelect ? "signal-plot selectable" : "signal-plot"}
      ref={svg}
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label}
      onClick={
        onSelect
          ? (event) => {
              const bounds = event.currentTarget.getBoundingClientRect();
              const px = ((event.clientX - bounds.left) * width) / bounds.width;
              const fraction = Math.max(
                0,
                Math.min(1, (px - left) / (right - left)),
              );
              onSelect(xMin + fraction * xSpan);
            }
          : undefined
      }
    >
      {yAxis.map(([value, label]) => {
        const y = bottom - ((value - yMin) / ySpan) * (bottom - top);
        return (
          <g key={value}>
            <line className="plot-grid" x1={left} x2={right} y1={y} y2={y} />
            <text x={left - 6} y={y + 3.5} textAnchor="end">
              {label ?? number(value)}
            </text>
          </g>
        );
      })}
      {xTicks.map((value) => {
        const px = left + ((value - xMin) / xSpan) * (right - left);
        return (
          <g key={value}>
            <line className="plot-grid" x1={px} x2={px} y1={top} y2={bottom} />
            <text x={px} y={height - 7} textAnchor="middle">
              {distanceAxis ? value.toFixed(1) : number(value)}
            </text>
          </g>
        );
      })}
      {paths.map(({ path, points, color, radius }) => (
        <g key={color}>
          {path && (
            <path d={path} fill="none" stroke={color} strokeWidth={1.5} />
          )}
          {points.map(([px, py], index) => (
            <circle key={index} cx={px} cy={py} r={radius} fill={color} />
          ))}
        </g>
      ))}
      {cursorX !== null && (
        <line
          className="plot-cursor"
          x1={cursorX}
          x2={cursorX}
          y1={top}
          y2={bottom}
        />
      )}
    </svg>
  );
}

function distanceTicks(min: number, max: number, width: number) {
  const first = Math.round(min * 10);
  const last = Math.round(max * 10);
  const steps = Math.max(1, Math.floor(width / 26));
  const stride = Math.max(1, Math.ceil((last - first) / steps));
  const start = Math.ceil(first / stride) * stride;
  return Array.from(
    { length: Math.max(0, Math.floor((last - start) / stride) + 1) },
    (_, index) => (start + index * stride) / 10,
  );
}
