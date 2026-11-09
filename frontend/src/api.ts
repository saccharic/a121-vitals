export type Flags = {
  data_saturated: boolean;
  frame_delayed: boolean;
  calibration_needed: boolean;
};
export type Subsweep = {
  index: number;
  data_offset: number;
  distance_m: number[];
};
export type Stream = {
  group_index: number;
  sensor_id: number;
  frame_shape: [number, number];
  num_frames: number;
  configured_frame_rate_hz: number | null;
  configured_sweep_rate_hz: number | null;
  duration_s: number | null;
  non_increasing_intervals: number;
  metadata: Record<string, unknown>;
  subsweeps: Subsweep[];
  warnings: string[];
  flag_counts: Record<keyof Flags, number>;
};
export type Recording = {
  id: string;
  name: string;
  source: "upload" | "synthetic" | "sample";
  idle_timeout_s: number;
  summary: {
    recording_uuid: string;
    recorded_at: string;
    recording_library_version: string;
    num_sessions: number;
    server_info: Record<string, unknown>;
    sessions: {
      session_index: number;
      session_config: Record<string, unknown>;
      streams: Stream[];
    }[];
  };
};
export type Frame = {
  frame_index: number;
  sweep_index: number;
  tick: number;
  tick_time_s: number;
  elapsed_s: number;
  temperature_c: number;
  flags: Flags;
  distance_m: number[];
  i: number[];
  q: number[];
  phase: number[];
  amplitude: number[];
};
export type Window = {
  start: number;
  stop: number;
  stride: number;
  frame_indices: number[];
  sweep_index: number;
  distance_m: number[];
  tick_time_s: number[];
  elapsed_s: number[];
  flags: Record<keyof Flags, boolean[]>;
  i: number[][];
  q: number[][];
  phase: number[][];
  amplitude: number[][];
  mean_amplitude: number[][];
};

export type Timeline = {
  elapsed_s: number[];
  exploration_tool_version: string;
  max_amplitude: number;
  breathing: {
    reason:
      "missing_settings" | "unsupported_settings" | "invalid_timing" | null;
    rates: (number | null)[];
    states: string[];
    window_s: number | null;
  };
};

export type AnalysisStage = "reading" | "signals" | "breathing";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new Error(
      "로컬 서버에 연결할 수 없습니다. 터미널에서 실행 상태를 확인하십시오.",
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      response.status,
      typeof body?.detail === "string"
        ? body.detail
        : `요청을 처리하지 못했습니다 (${response.status}). 다시 시도하십시오.`,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export async function requirePlaybackServer(signal?: AbortSignal) {
  const health = await request<{ playback_api?: number } | null>(
    "/api/health",
    { signal },
  );
  if (health?.playback_api !== 3) {
    throw new Error(
      "이전 버전의 서버가 실행 중입니다. 기존 서버를 종료한 뒤 bash run-local.sh로 다시 실행하십시오.",
    );
  }
}

export function query(params: Record<string, number>): string {
  return new URLSearchParams(
    Object.entries(params).map(([key, value]) => [key, String(value)]),
  ).toString();
}
