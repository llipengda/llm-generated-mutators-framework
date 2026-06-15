// ── UI display types ────────────────────────────────────────────────

export type StepStatus = 'pending' | 'active' | 'running' | 'success' | 'error' | 'warning';

export interface WorkflowStep {
  id: string;
  title: string;
  description: string;
}

export interface PacketType {
  id: string;
  name: string;
  description: string;
  selected: boolean;
}

export interface MutatorInfo {
  packetType: string;
  status: 'pending' | 'generating' | 'validating' | 'ready' | 'error';
  syntaxValid: boolean;
  code?: string;
}

export interface MutatorTestResult {
  packetType: string;
  totalTests: number;
  passed: number;
  failed: number;
  parseFailures: number;
  runtimeErrors: number;
  status: 'pending' | 'running' | 'passed' | 'failed' | 'repairing';
  repairAttempts: number;
  issues: string[];
  prompt?: string;
}

export interface LogEntry {
  id: number;
  time: string;
  level: 'info' | 'success' | 'warning' | 'error';
  message: string;
}

// ── API types ───────────────────────────────────────────────────────

export interface LlmConfig {
  api_key: string;
  base_url: string;
  model: string;
  temperature: number;
  embedding_model: string;
  embedding_base_url: string;
  embedding_api_key: string;
}

export interface CreateSessionResponse {
  session_id: string;
  protocol: string;
  fixer_enabled: boolean;
  rfc_path: string;
  seed_dir: string;
  available_steps: string[];
  created_at: string;
}

export interface SessionSummary {
  session_id: string;
  protocol: string;
  fixer_enabled: boolean;
  created_at: string;
  completed_steps: number;
  total_steps: number;
  status: 'idle' | 'running' | 'completed' | 'failed';
}

export interface ApiStepStatus {
  step_id: string;
  name: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  available: boolean;
}

export interface SessionDetail {
  session_id: string;
  protocol: string;
  fixer_enabled: boolean;
  created_at: string;
  steps: Record<string, ApiStepStatus>;
  packet_types: string[] | null;
  token_usage: Record<string, unknown> | null;
  rfc_path: string;
  seed_dir: string;
}

export interface RunStepResponse {
  session_id: string;
  step_id: string;
  status: 'completed' | 'failed';
  output: string | null;
  llm_outputs: string[] | null;
  token_usage: Record<string, number> | null;
  error: string | null;
}

// ── WebSocket message ───────────────────────────────────────────────

export interface WsMessage {
  type: 'log' | 'ping';
  line?: string;
}
