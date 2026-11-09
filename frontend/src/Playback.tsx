import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  type Recording,
  type Stream,
  type Timeline,
  type Window,
  query,
  request,
} from "./api";
import SignalPlot, { type Trace } from "./SignalPlot";

const DetailChart = lazy(() => import("./Chart"));
const BREATHING =
  "https://docs.acconeer.com/en/latest/applications/a121/breathing.html";
const colors = {
  i: "#a6c9ff",
  q: "#f6b994",
  phase: "#c8deab",
  amplitude: "#aec6ff",
};
const stateLabels: Record<string, string> = {
  estimated: "추정값 있음",
  no_presence: "사람이 감지되지 않음",
  motion: "움직임 감지",
  finding_distance: "대상 거리 확인 중",
  warming_up: "분석 구간 확보 중",
  invalid_signal: "기록 품질 또는 프레임 간격 불량",
};
const metricLabels = { iq: "I/Q", phase: "위상", amplitude: "진폭" };
type Metric = keyof typeof metricLabels;
const metrics: Metric[] = ["amplitude", "iq", "phase"];
const phaseTicks = [
  [-Math.PI, "−π"],
  [-Math.PI / 2, "−π/2"],
  [0, "0"],
  [Math.PI / 2, "π/2"],
  [Math.PI, "π"],
] as const;

function number(value: number | null | undefined, decimals = 2) {
  return value == null ? "—" : value.toFixed(decimals);
}

function amplitudeAxis(peak: number) {
  const target = Math.max(peak, 1) / 5;
  const magnitude = 10 ** Math.floor(Math.log10(target));
  const step =
    magnitude * ([1, 2, 2.5, 5, 10].find((n) => n >= target / magnitude) ?? 10);
  const count = Math.ceil(Math.max(peak, 1) / step);
  return {
    domain: [0, count * step] as [number, number],
    ticks: Array.from({ length: count + 1 }, (_, i) => [i * step] as const),
  };
}

function closest(values: number[], target: number) {
  let left = 0;
  let right = values.length - 1;
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    if (values[mid] < target) left = mid + 1;
    else right = mid;
  }
  return left > 0 && target - values[left - 1] < values[left] - target
    ? left - 1
    : left;
}

export default function Playback({
  record,
  onReady,
  onError,
}: {
  record: Recording;
  onReady: () => void;
  onError: (message: string) => void;
}) {
  const streams = useMemo(
    () =>
      record.summary.sessions.flatMap((session) =>
        session.streams
          .filter((stream) => stream.num_frames > 0)
          .map((stream) => ({
            session: session.session_index,
            stream,
          })),
      ),
    [record],
  );
  const [streamIndex, setStreamIndex] = useState(0);
  const { session, stream } = streams[streamIndex];
  return (
    <>
      {streams.length > 1 && (
        <label className="stream-select">
          기록 스트림
          <select
            value={streamIndex}
            onChange={(event) => setStreamIndex(Number(event.target.value))}
          >
            {streams.map(({ session, stream }, index) => (
              <option key={index} value={index}>
                세션 {session} · 그룹 {stream.group_index} · 센서{" "}
                {stream.sensor_id}
              </option>
            ))}
          </select>
        </label>
      )}
      <Player
        key={streamIndex}
        recordId={record.id}
        session={session}
        stream={stream}
        libraryVersion={record.summary.recording_library_version}
        onReady={onReady}
        onError={onError}
      />
    </>
  );
}

function Player({
  recordId,
  session,
  stream,
  libraryVersion,
  onReady,
  onError,
}: {
  recordId: string;
  session: number;
  stream: Stream;
  libraryVersion: string;
  onReady: () => void;
  onError: (message: string) => void;
}) {
  const base = `/api/recordings/${recordId}`;
  const streamQuery = useMemo(
    () => ({ session, group: stream.group_index, sensor: stream.sensor_id }),
    [session, stream],
  );
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [overview, setOverview] = useState<Window | null>(null);
  const [loadError, setLoadError] = useState("");
  const [subsweep, setSubsweep] = useState(0);
  const [sweep, setSweep] = useState(0);
  const distances = stream.subsweeps[subsweep].distance_m;
  const [point, setPoint] = useState(() => Math.floor(distances.length / 2));
  const [frameIndex, setFrameIndex] = useState(0);
  const [playbackStart, setPlaybackStart] = useState<number | null>(null);
  const playing = playbackStart !== null;
  const [activated, setActivated] = useState(false);
  const [metric, setMetric] = useState<Metric>("amplitude");
  const [showDetail, setShowDetail] = useState(false);
  const [retry, setRetry] = useState(0);
  const options = useMemo(
    () => ({ ...streamQuery, subsweep, sweep_index: sweep }),
    [streamQuery, subsweep, sweep],
  );
  const overviewPath = `${base}/window?${query({ ...options, stop: stream.num_frames, max_frames: 512 })}`;
  const expected = useMemo(() => new Map<number, Window>(), [options, retry]);
  const [, refresh] = useState(0);
  const batchSize = Math.max(
    1,
    Math.min(64, Math.floor(32768 / distances.length)),
  );
  const batch = Math.floor(frameIndex / batchSize) * batchSize;
  const window = expected.get(batch);
  const row = frameIndex - batch;
  const reliableTime = stream.non_increasing_intervals === 0;

  useEffect(() => {
    const controller = new AbortController();
    void request<Timeline>(`${base}/timeline?${query(streamQuery)}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setTimeline(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLoadError(error.message);
      });
    return () => controller.abort();
  }, [base, streamQuery, retry]);

  useEffect(() => {
    const controller = new AbortController();
    void request<Window>(overviewPath, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setOverview(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLoadError(error.message);
      });
    return () => controller.abort();
  }, [overviewPath, retry]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = globalThis.setTimeout(
      async () => {
        for (const start of [batch, batch + batchSize]) {
          if (start >= stream.num_frames || expected.has(start)) continue;
          const stop = Math.min(stream.num_frames, start + batchSize);
          try {
            const value = await request<Window>(
              `${base}/window?${query({
                ...options,
                start,
                stop,
                max_frames: batchSize,
              })}`,
              { signal: controller.signal },
            );
            if (controller.signal.aborted) return;
            expected.set(start, value);
            if (expected.size > 6) {
              const oldest = [...expected.keys()].find(
                (key) => key !== batch && key !== start,
              );
              if (oldest !== undefined) expected.delete(oldest);
            }
            if (start === batch) refresh((count) => count + 1);
          } catch (error) {
            if (!controller.signal.aborted && start === batch) {
              setPlaybackStart(null);
              setLoadError((error as Error).message);
            }
            return;
          }
        }
      },
      expected.has(batch) ? 0 : 28,
    );
    return () => {
      controller.abort();
      globalThis.clearTimeout(timer);
    };
  }, [base, batch, batchSize, expected, options, stream.num_frames]);

  const dataReady = !!timeline && !!window && !!overview;
  useEffect(() => {
    if (dataReady) {
      setActivated(true);
      onReady();
    }
  }, [dataReady, onReady]);
  useEffect(() => {
    if (loadError) onError(loadError);
  }, [loadError, onError]);

  useEffect(() => {
    if (playbackStart === null || !timeline) return;
    let animation: number;
    const startWall = performance.now();
    const times = timeline.elapsed_s;
    const last = times[times.length - 1];
    const startTime = times[playbackStart];
    function tick(now: number) {
      const target = Math.min(last, startTime + (now - startWall) / 1000);
      setFrameIndex(closest(times, target));
      if (target < last) animation = requestAnimationFrame(tick);
      else setPlaybackStart(null);
    }
    animation = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animation);
  }, [playbackStart, timeline]);

  const times = timeline?.elapsed_s ?? [];
  const lastFrame = stream.num_frames - 1;
  const duration = times[lastFrame] ?? 0;
  const now = reliableTime ? (times[frameIndex] ?? 0) : frameIndex;
  const timelineEnd = reliableTime ? duration : lastFrame;
  const rate = timeline?.breathing.rates[frameIndex];
  const breathState = timeline?.breathing.states[frameIndex];
  const breathReason = timeline?.breathing.reason;
  const sourceAxis = reliableTime ? "기록 시간 (초)" : "프레임 번호";
  const commonDomain: [number, number] = [0, timelineEnd];
  const distanceDomain: [number, number] = [
    distances[0],
    distances[distances.length - 1],
  ];
  const distanceAxis: [number, number] = [
    Math.floor(distanceDomain[0] * 10) / 10,
    Math.ceil(distanceDomain[1] * 10) / 10,
  ];
  const profileAmplitudeAxis = amplitudeAxis(timeline?.max_amplitude ?? 0);
  const level = Math.max(
    1,
    Math.ceil((timeline?.max_amplitude ?? 0) / 100) * 100,
  );
  const profileTraces: Record<Metric, Trace[]> = window
    ? {
        iq: [
          { color: colors.i, values: window.i[row] },
          { color: colors.q, values: window.q[row] },
        ],
        phase: [
          { color: colors.phase, values: window.phase[row], mode: "markers" },
        ],
        amplitude: [
          { color: colors.amplitude, values: window.mean_amplitude[row] },
        ],
      }
    : { iq: [], phase: [], amplitude: [] };
  const axisRanges: Record<Metric, [number, number]> = {
    iq: [-level, level],
    phase: [-Math.PI, Math.PI],
    amplitude: profileAmplitudeAxis.domain,
  };
  const overviewAxis = useMemo(
    () =>
      overview
        ? reliableTime
          ? overview.elapsed_s
          : overview.frame_indices
        : [],
    [overview, reliableTime],
  );
  const overviewTraces = useMemo<Trace[]>(() => {
    if (!overview) return [];
    if (metric === "iq")
      return [
        { color: colors.i, values: overview.i.map((frame) => frame[point]) },
        { color: colors.q, values: overview.q.map((frame) => frame[point]) },
      ];
    return [
      {
        color: colors[metric],
        values: (metric === "amplitude"
          ? overview.mean_amplitude
          : overview.phase
        ).map((frame) => frame[point]),
        mode: metric === "phase" ? "markers" : "line",
      },
    ];
  }, [overview, metric, point]);

  const timeAmplitudeAxis = useMemo(() => {
    const peak =
      overview?.mean_amplitude.reduce(
        (max, frame) => Math.max(max, frame[point]),
        0,
      ) ?? 0;
    return amplitudeAxis(peak);
  }, [overview, point]);

  const breathingHistory = useMemo(() => {
    const x: number[] = [];
    const values: (number | null)[] = [];
    let peak = 20;
    if (timeline && !timeline.breathing.reason) {
      const { rates } = timeline.breathing;
      const step = Math.max(1, Math.ceil(rates.length / 1800));
      for (let start = 0; start < rates.length; start += step) {
        const end = Math.min(start + step, rates.length);
        let firstValid = -1;
        let hasGap = false;
        for (let index = start; index < end; index++) {
          const rate = rates[index];
          if (rate === null) hasGap = true;
          else {
            if (firstValid === -1) firstValid = index;
            peak = Math.max(peak, rate);
          }
        }
        // Break the line when a display bucket contains an unestimated frame.
        if (hasGap && values.length && values[values.length - 1] !== null) {
          x.push(timeline.elapsed_s[start]);
          values.push(null);
        }
        if (firstValid !== -1) {
          x.push(timeline.elapsed_s[firstValid]);
          values.push(rates[firstValid]);
          if (hasGap) {
            x.push(timeline.elapsed_s[end - 1]);
            values.push(null);
          }
        }
      }
    }
    const traces: Trace[] = [
      { color: colors.amplitude, values, mode: "line+markers" },
    ];
    return {
      x,
      traces,
      peak: Math.ceil(peak / 10) * 10,
      hasEstimate: values.some((value) => value !== null),
    };
  }, [timeline]);

  function seek(value: number) {
    setPlaybackStart(null);
    setFrameIndex(reliableTime ? closest(times, value) : Math.round(value));
  }

  function togglePlaying() {
    if (playing) setPlaybackStart(null);
    else {
      const start = frameIndex === lastFrame ? 0 : frameIndex;
      setFrameIndex(start);
      setPlaybackStart(start);
    }
  }

  function changeSelection(nextSubsweep: number, nextSweep: number) {
    setPlaybackStart(null);
    setOverview(null);
    setLoadError("");
    if (loadError) onError("");
    setSubsweep(nextSubsweep);
    setSweep(nextSweep);
    if (nextSubsweep !== subsweep)
      setPoint(
        Math.floor(stream.subsweeps[nextSubsweep].distance_m.length / 2),
      );
  }

  if (!timeline || !overview || (!window && !activated)) {
    return (
      <div className="waiting-panel" role="status">
        <div>
          <strong>{loadError ? "재생 준비 오류" : "재생 준비 중"}</strong>
          {loadError && (
            <p>
              <button
                className="text-link"
                onClick={() => {
                  setLoadError("");
                  onError("");
                  setRetry((value) => value + 1);
                }}
              >
                다시 불러오기
              </button>
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="viewer">
      <div className="player-controls">
        <button
          className="button primary play-button"
          aria-label={playing ? "일시정지" : "재생"}
          disabled={!reliableTime || lastFrame < 1 || (!window && !playing)}
          onClick={togglePlaying}
        >
          <span aria-hidden="true">{playing ? "Ⅱ" : "▶"}</span>{" "}
          {playing ? "일시정지" : "재생"}
        </button>
        <div className="timeline-control">
          <input
            type="range"
            aria-label={sourceAxis}
            min={0}
            max={timelineEnd || 1}
            step={reliableTime ? "any" : 1}
            value={now}
            disabled={lastFrame < 1}
            onChange={(event) => seek(Number(event.target.value))}
          />
          <div className="timeline-labels">
            <span>{reliableTime ? "0.00초" : "프레임 0"}</span>
            <span>
              {reliableTime ? `${number(duration)}초` : `프레임 ${lastFrame}`}
            </span>
          </div>
        </div>
        <div className="playback-time">
          <strong>
            {reliableTime ? `${number(now)}초` : `#${frameIndex}`}
          </strong>
        </div>
      </div>

      {!reliableTime && (
        <p className="quality-note">
          프레임 시간이 역행하거나 겹칩니다. 시간 막대로 프레임을 직접 확인할 수
          있습니다.
        </p>
      )}
      {loadError && (
        <p className="quality-note">
          재생 구간을 불러오지 못했습니다.{" "}
          <button
            className="text-link"
            onClick={() => {
              setLoadError("");
              onError("");
              setRetry((value) => value + 1);
            }}
          >
            다시 불러오기
          </button>
        </p>
      )}

      <div className="plot-heading">
        <div>
          <h3>거리별 신호</h3>
          <p>
            스윕 {sweep} · {number(distanceDomain[0], 3)}–
            {number(distanceDomain[1], 3)} m
          </p>
        </div>
        {!window && <span className="hint">구간 불러오는 중</span>}
      </div>
      <div className="profiles">
        {metrics.map((type) => (
          <div className="plot-card" key={type}>
            <div className="plot-title">
              <h4>{type === "iq" ? "Sparse IQ" : metricLabels[type]}</h4>
              <span>
                {type === "iq"
                  ? "원본 정수"
                  : type === "phase"
                    ? "rad"
                    : "스윕 평균 · counts"}
              </span>
            </div>
            <SignalPlot
              label={`거리별 ${metricLabels[type]}`}
              x={distances}
              traces={profileTraces[type]}
              xDomain={distanceAxis}
              yDomain={axisRanges[type]}
              yTicks={
                type === "phase"
                  ? phaseTicks
                  : type === "amplitude"
                    ? profileAmplitudeAxis.ticks
                    : undefined
              }
              distanceAxis
              cursor={distances[point]}
              onSelect={(value) => setPoint(closest(distances, value))}
            />
            <div className="plot-footer">
              {type === "iq" && (
                <span className="legend">
                  <i style={{ background: colors.i }} /> I{" "}
                  <i style={{ background: colors.q }} /> Q
                </span>
              )}
              <span>거리 (m)</span>
            </div>
          </div>
        ))}
      </div>

      <div className="plot-card focus-plot">
        <div className="plot-title">
          <div>
            <h4>선택 거리의 시계열</h4>
            <div className="distance-control">
              <input
                type="range"
                aria-label="거리 지점"
                aria-valuetext={`${number(distances[point], 3)} m`}
                min={0}
                max={distances.length - 1}
                value={point}
                disabled={distances.length < 2}
                onChange={(event) => setPoint(Number(event.target.value))}
              />
              <span>
                {number(distances[point], 3)} m
                {metric === "amplitude" && " · 스윕 평균"}
              </span>
            </div>
          </div>
          <div className="metric-switch" role="group" aria-label="시계열 항목">
            {metrics.map((type) => (
              <button
                key={type}
                aria-pressed={metric === type}
                onClick={() => setMetric(type)}
              >
                {metricLabels[type]}
              </button>
            ))}
          </div>
        </div>
        <SignalPlot
          label="선택 거리의 시간 변화"
          height={266}
          x={overviewAxis}
          traces={overviewTraces}
          xDomain={commonDomain}
          yDomain={
            metric === "amplitude"
              ? timeAmplitudeAxis.domain
              : axisRanges[metric]
          }
          yTicks={
            metric === "phase"
              ? phaseTicks
              : metric === "amplitude"
                ? timeAmplitudeAxis.ticks
                : undefined
          }
          cursor={now}
          onSelect={seek}
        />
        <div className="plot-footer">
          <span>{sourceAxis}</span>
        </div>
      </div>

      {!breathReason && (
        <div className="plot-card breathing-plot">
          <div className="plot-title">
            <div>
              <h4>호흡수 추정</h4>
              <a className="text-link" href={BREATHING}>
                Acconeer Breathing · {timeline.exploration_tool_version}
              </a>
            </div>
            <span className="rate">
              {rate == null ? "—" : number(rate, 1)} <small>회/분</small>
            </span>
          </div>
          {breathingHistory.hasEstimate ? (
            <SignalPlot
              label="기록 시간에 따른 호흡수 추정"
              x={breathingHistory.x}
              traces={breathingHistory.traces}
              xDomain={commonDomain}
              yDomain={[0, breathingHistory.peak]}
              cursor={now}
              onSelect={seek}
            />
          ) : (
            <div className="breath-empty">추정 구간 없음</div>
          )}
          <div className="plot-footer">
            <span>
              {breathState ? stateLabels[breathState] : "추정값 없음"}
            </span>
            <span>
              {timeline.breathing.window_s
                ? `분석 구간 ${number(timeline.breathing.window_s, 0)}초`
                : "회/분"}
            </span>
          </div>
        </div>
      )}

      <details
        className="inspection"
        onToggle={(event) => setShowDetail(event.currentTarget.open)}
      >
        <summary>기록 설정 및 원본 데이터</summary>
        {showDetail && (
          <div className="inspection-body">
            <p className="hint">
              {stream.num_frames.toLocaleString()} 프레임 ·{" "}
              {stream.frame_shape[0]} 스윕/프레임 · Exploration Tool{" "}
              {libraryVersion}
            </p>
            <div className="inspection-options">
              <label>
                서브스윕
                <select
                  value={subsweep}
                  onChange={(event) =>
                    changeSelection(Number(event.target.value), sweep)
                  }
                >
                  {stream.subsweeps.map((sub) => (
                    <option key={sub.index} value={sub.index}>
                      {sub.index}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                스윕
                <select
                  value={sweep}
                  onChange={(event) =>
                    changeSelection(subsweep, Number(event.target.value))
                  }
                >
                  {Array.from({ length: stream.frame_shape[0] }, (_, index) => (
                    <option key={index} value={index}>
                      {index}
                    </option>
                  ))}
                </select>
              </label>
              <a
                className="button secondary"
                href={`${base}/frame.csv?${query({ ...options, frame_index: frameIndex })}`}
                download
              >
                현재 프레임 CSV
              </a>
            </div>
            <p className="hint">
              포화 {stream.flag_counts.data_saturated ?? 0} · 지연{" "}
              {stream.flag_counts.frame_delayed ?? 0} · 보정 필요{" "}
              {stream.flag_counts.calibration_needed ?? 0} 프레임
            </p>
            {!!stream.warnings.length && (
              <ul className="hint">
                {stream.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            )}
            {window && (
              <div className="data-table">
                <table>
                  <caption>
                    프레임 {frameIndex} · 스윕 {sweep}
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">거리 (m)</th>
                      <th scope="col">I</th>
                      <th scope="col">Q</th>
                      <th scope="col">위상 (rad)</th>
                      <th scope="col">진폭</th>
                    </tr>
                  </thead>
                  <tbody>
                    {distances.map((distance, index) => (
                      <tr key={index}>
                        <td>{number(distance, 4)}</td>
                        <td>{window.i[row][index]}</td>
                        <td>{window.q[row][index]}</td>
                        <td>{number(window.phase[row][index], 4)}</td>
                        <td>{number(window.amplitude[row][index], 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <h4>거리–시간 위상 지도</h4>
            <Suspense
              fallback={<p className="hint">위상 지도를 불러오는 중입니다.</p>}
            >
              <PhaseMap
                overview={overview}
                times={overviewAxis}
                axisLabel={sourceAxis}
              />
            </Suspense>
          </div>
        )}
      </details>
    </div>
  );
}

function PhaseMap({
  overview,
  times,
  axisLabel,
}: {
  overview: Window;
  times: number[];
  axisLabel: string;
}) {
  const data = useMemo(
    () => [
      {
        type: "heatmap" as const,
        x: times,
        y: overview.distance_m,
        z: overview.distance_m.map((_, index) =>
          overview.phase.map((row) => row[index]),
        ),
        colorscale: "RdBu" as const,
        zmin: -Math.PI,
        zmax: Math.PI,
        colorbar: { thickness: 9, title: { text: "rad" } },
      },
    ],
    [overview, times],
  );
  const layout = useMemo(
    () => ({
      xaxis: { title: { text: axisLabel } },
      yaxis: { title: { text: "거리 (m)" } },
    }),
    [axisLabel],
  );
  return (
    <DetailChart
      data={data}
      layout={layout}
      label="거리와 시간에 따른 위상 지도"
      revision="phase-map"
    />
  );
}
