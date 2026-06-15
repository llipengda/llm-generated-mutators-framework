import type {
  CreateSessionResponse,
  RunStepResponse,
  SessionDetail,
  SessionSummary,
} from '../types';

const BASE = '/api/v1';

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const body = await res.text();
    let detail = body;
    try {
      detail = JSON.parse(body).detail ?? body;
    } catch { /* use raw body */ }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

// ── Sessions ────────────────────────────────────────────────────────

export function createSession(formData: FormData): Promise<CreateSessionResponse> {
  return request<CreateSessionResponse>(`${BASE}/sessions`, {
    method: 'POST',
    body: formData,
  });
}

export function getSession(sessionId: string): Promise<SessionDetail> {
  return request<SessionDetail>(`${BASE}/sessions/${sessionId}`);
}

export function listSessions(): Promise<SessionSummary[]> {
  return request<SessionSummary[]>(`${BASE}/sessions`);
}

export function deleteSession(sessionId: string): Promise<{ detail: string }> {
  return request<{ detail: string }>(`${BASE}/sessions/${sessionId}`, {
    method: 'DELETE',
  });
}

// ── Steps ───────────────────────────────────────────────────────────

export interface RunStepParams {
  selected_types?: string[];
  skip_verification?: boolean;
}

export function runStep(
  sessionId: string,
  stepId: string,
  params?: RunStepParams,
): Promise<RunStepResponse> {
  return request<RunStepResponse>(
    `${BASE}/sessions/${sessionId}/steps/${stepId}/run`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params ?? {}),
    },
  );
}

// ── Logs ────────────────────────────────────────────────────────────

export async function getSessionLogs(
  sessionId: string,
  tail = 100,
): Promise<{ lines: string[]; total_lines: number }> {
  const url = `${BASE}/sessions/${sessionId}/logs?tail=${tail}`;
  const res = await fetch(url);
  if (!res.ok) return { lines: [], total_lines: 0 };
  return res.json();
}
