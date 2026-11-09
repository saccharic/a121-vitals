import {
  Component,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import licenseText from "../../LICENSE?raw";
import {
  ApiError,
  type AnalysisStage,
  type Recording,
  request,
  requirePlaybackServer,
} from "./api";
import Playback from "./Playback";

const SOURCE = "https://github.com/saccharic/a121-vitals";
const LICENSE_DOWNLOAD = `data:text/plain;charset=utf-8,${encodeURIComponent(licenseText)}`;
const SAMPLE = "application/x-a121-sample";
const SAMPLE_NAME = "Sample.h5";
const stageNames = {
  reading: "파일 확인",
  signals: "신호 계산",
  breathing: "호흡 분석",
};
const storageKey = "a121-vitals-recording";

function openRecording(job: string, file?: File, signal?: AbortSignal) {
  const parameters = new URLSearchParams({ job });
  if (file) {
    parameters.set("filename", file.name);
    return request<Recording>(`/api/recordings?${parameters}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
      signal,
    });
  }
  return request<Recording>(`/api/examples/sample?${parameters}`, {
    method: "POST",
    signal,
  });
}

function readStored() {
  try {
    return sessionStorage.getItem(storageKey);
  } catch {
    return null;
  }
}

function remember(id: string | null) {
  try {
    if (id) sessionStorage.setItem(storageKey, id);
    else sessionStorage.removeItem(storageKey);
  } catch {
    // The viewer works without storage.
  }
}

function Icon({ type }: { type: "upload" | "sample" | "arrow" }) {
  const paths = {
    upload: "M12 15V4m-4 4 4-4 4 4M4 15v5h16v-5",
    sample: "M6 3h8l4 4v14H6z M14 3v5h4M9 15h1l2-4 2 6 1-2h1",
    arrow: "M5 12h14m-5-5 5 5-5 5",
  };
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d={paths[type]} />
    </svg>
  );
}

class PlaybackBoundary extends Component<{
  children: ReactNode;
  onFailure: () => void;
}> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    this.props.onFailure();
  }

  render() {
    return this.state.failed ? (
      <div className="waiting-panel">재생 오류</div>
    ) : (
      this.props.children
    );
  }
}

export default function App() {
  const [record, setRecord] = useState<Recording | null>(null);
  const [starting, setStarting] = useState(true);
  const [job, setJob] = useState<string | null>(null);
  const [stage, setStage] = useState<AnalysisStage>("reading");
  const [ready, setReady] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const uploading = useRef(false);
  const busy = starting || job !== null;
  const recordName = record?.source === "sample" ? SAMPLE_NAME : record?.name;
  const markReady = useCallback(() => setReady(true), []);
  const viewerError = useCallback((message: string) => setError(message), []);
  const playbackFailed = useCallback(() => {
    setReady(false);
    setError("기록을 표시하지 못했습니다. 기록을 다시 열어 주십시오.");
  }, []);

  useEffect(() => {
    const id = readStored();
    const controller = new AbortController();
    const { signal } = controller;
    async function restore() {
      try {
        await requirePlaybackServer(signal);
        let next: Recording | undefined;
        if (id) {
          try {
            next = await request<Recording>(
              `/api/recordings/${encodeURIComponent(id)}`,
              { signal },
            );
          } catch (error) {
            if (signal.aborted) return;
            if (!(error instanceof ApiError) || error.status !== 404)
              throw error;
            remember(null);
          }
        }
        if (signal.aborted) return;
        if (!next) {
          const sampleJob = crypto.randomUUID();
          setJob(sampleJob);
          next = await openRecording(sampleJob, undefined, signal);
        }
        if (!signal.aborted) {
          setRecord(next);
          remember(next.id);
        }
      } catch (error) {
        if (!signal.aborted) setError((error as Error).message);
      } finally {
        if (!signal.aborted) {
          setStarting(false);
          setJob(null);
        }
      }
    }
    void restore();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!job) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const result = await request<{ stage: AnalysisStage | null }>(
          `/api/analysis-progress/${job}`,
          { signal: controller.signal },
        );
        if (result.stage && !controller.signal.aborted) setStage(result.stage);
      } catch {
        // The upload request reports terminal errors.
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 250);
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [job]);

  async function load(file?: File) {
    if (busy || uploading.current) return;
    if (file && !/\.h(?:5|df5)$/i.test(file.name)) {
      setError("Acconeer A121 .h5 또는 .hdf5 기록을 선택해 주십시오.");
      return;
    }
    if (file && file.size > 32 * 1024 * 1024) {
      setError("파일 크기는 32 MiB 이하여야 합니다.");
      return;
    }
    uploading.current = true;
    setError("");
    try {
      await requirePlaybackServer();
      const id = crypto.randomUUID();
      setJob(id);
      setStage("reading");
      const next = await openRecording(id, file);
      setReady(false);
      setRecord(next);
      remember(next.id);
      if (record && record.source !== "sample")
        void request(`/api/recordings/${record.id}`, {
          method: "DELETE",
        }).catch(() => {});
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "기록을 열 수 없습니다.",
      );
    } finally {
      uploading.current = false;
      setJob(null);
      if (input.current) input.current.value = "";
    }
  }

  function close() {
    if (!record || busy || uploading.current) return;
    setRecord(null);
    setReady(false);
    setError("");
    remember(null);
    if (record.source !== "sample")
      void request(`/api/recordings/${record.id}`, { method: "DELETE" }).catch(
        () => {},
      );
  }

  return (
    <div className="site-layout">
      <main className="wrap">
        <section className="hero" aria-labelledby="title">
          <h1 id="title">
            A121 Vitals <span>XE125</span>
          </h1>
          <div className="showcase">
            {record && !job ? (
              <PlaybackBoundary key={record.id} onFailure={playbackFailed}>
                <Playback
                  record={record}
                  onReady={markReady}
                  onError={viewerError}
                />
              </PlaybackBoundary>
            ) : (
              <div className="waiting-panel hero-wait" role="status">
                {busy ? "재생 준비 중" : "재생 대기"}
              </div>
            )}
          </div>
        </section>

        <section className="content-section" aria-labelledby="data-title">
          <h2 id="data-title">Open a Recording</h2>
          <div className="source-grid">
            <div
              className={
                dragging
                  ? "source-card drop-zone is-dragging"
                  : "source-card drop-zone"
              }
              onDragOver={(event) => {
                event.preventDefault();
                if (!busy) setDragging(true);
              }}
              onDragLeave={(event) => {
                if (
                  !(event.relatedTarget instanceof Node) ||
                  !event.currentTarget.contains(event.relatedTarget)
                )
                  setDragging(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (event.dataTransfer.types.includes(SAMPLE)) void load();
                else if (event.dataTransfer.files[0])
                  void load(event.dataTransfer.files[0]);
              }}
            >
              <div className="source-icon">
                <Icon type="upload" />
              </div>
              <div>
                <h3>.H5 파일</h3>
                <span className="hint">최대 32 MiB</span>
              </div>
              <input
                type="file"
                accept=".h5,.hdf5"
                ref={input}
                hidden
                onChange={(event) => {
                  if (event.target.files?.[0]) void load(event.target.files[0]);
                }}
              />
              <button
                className="button primary"
                disabled={busy}
                onClick={() => input.current?.click()}
              >
                파일 선택
              </button>
            </div>

            <div
              className="source-card sample-card"
              draggable={!busy}
              onDragStart={(event) => {
                event.dataTransfer.setData(SAMPLE, "sample");
                event.dataTransfer.effectAllowed = "copy";
              }}
            >
              <div className="source-icon">
                <Icon type="sample" />
              </div>
              <h3>{SAMPLE_NAME}</h3>
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void load()}
              >
                샘플 열기 <Icon type="arrow" />
              </button>
            </div>
          </div>
        </section>

        {(job || record || error) && (
          <section className="analysis-status" aria-label="분석 상태">
            {job ? (
              <div className="progress-bar" role="status" aria-live="polite">
                <span className="status-dot working" />
                <strong>{stageNames[stage]} 중</strong>
              </div>
            ) : (
              record && (
                <div className="progress-bar" role="status" aria-live="polite">
                  <span
                    className={
                      ready
                        ? "status-dot"
                        : error
                          ? "status-dot failed"
                          : "status-dot working"
                    }
                  />
                  <strong>
                    {ready
                      ? "재생 준비 완료"
                      : error
                        ? "재생 준비 오류"
                        : "재생 준비 중"}
                  </strong>
                  <span className="record-name" title={recordName}>
                    {recordName}
                  </span>
                  <button className="text-link close-record" onClick={close}>
                    닫기
                  </button>
                </div>
              )
            )}
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
          </section>
        )}

        <section className="content-section" aria-labelledby="hardware-title">
          <h2 id="hardware-title">Radar Specs</h2>
          <dl className="spec-grid">
            <div>
              <dt>레이더 센서</dt>
              <dd>Acconeer A121</dd>
            </div>
            <div>
              <dt>대역</dt>
              <dd>60 GHz</dd>
            </div>
            <div>
              <dt>평가 보드 및 모듈</dt>
              <dd>XE125 · XM125</dd>
            </div>
          </dl>
          <a
            className="text-link reference"
            href="https://acconeer.com/products/"
          >
            Acconeer 제품 사양
          </a>
        </section>
      </main>

      <footer className="wrap site-footer">
        <span>A121 Vitals · XE125</span>
        <div className="footer-links">
          <a href={SOURCE}>GitHub</a>
          <a href={LICENSE_DOWNLOAD} download="a121-vitals-LICENSE.txt">
            MIT License
          </a>
        </div>
      </footer>
    </div>
  );
}
