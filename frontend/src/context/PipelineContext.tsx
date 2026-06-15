import { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from 'react';
import type { PacketType, MutatorInfo, MutatorTestResult, LogEntry, LlmConfig } from '../types';
import * as api from '../services/api';
import { connectLogSocket } from '../services/ws';

// ── Step definitions ──────────────────────────────────────────────

export interface PipelineStep {
  id: string;
  title: string;
  description: string;
  path: string;
  stepId: string | null; // API step_id, null for upload
}

export const STEPS: PipelineStep[] = [
  { id: 'upload', title: '上传文件', description: '上传规范文档和测试包', path: '/upload', stepId: null },
  { id: 'extract', title: '类型提取', description: '提取数据包类型', path: '/extract', stepId: 'step_1' },
  { id: 'datamodel', title: 'DataModel生成', description: '生成 DataModel', path: '/datamodel', stepId: 'step_2' },
  { id: 'validate', title: 'DataModel检验&修复', description: 'DataModel 校验与修复', path: '/validate', stepId: 'step_3' },
  { id: 'mutators', title: '变异器生成', description: '生成并校验变异器', path: '/mutators', stepId: 'step_4' },
  { id: 'test', title: '变异器检验&修复', description: '变异器测试与修复', path: '/test', stepId: 'step_5' },
  { id: 'package', title: '打包', description: '打包为动态库', path: '/package', stepId: 'step_final' },
];

const SESSION_KEY = 'pipeline_session_id';

// ── State type ────────────────────────────────────────────────────

export interface PipelineState {
  currentStep: string;
  running: boolean;
  sessionId: string | null;
  protocolName: string;
  specFile: File | null;
  testPackets: File[];
  llmConfig: LlmConfig;
  packetTypes: PacketType[];
  datamodel: string;
  validationPassed: boolean;
  validationError: string | null;
  mutators: MutatorInfo[];
  testResults: MutatorTestResult[];
  packageReady: boolean;
  packagePath: string;
  logs: LogEntry[];
  stepCompleted: Record<string, boolean>;
  getStepStatus: (id: string) => 'pending' | 'active' | 'running' | 'success' | 'error' | 'warning';
  canAccessStep: (id: string) => boolean;
}

// ── Actions type ──────────────────────────────────────────────────

export interface PipelineActions {
  setProtocolName: (v: string) => void;
  setSpecFile: (f: File) => void;
  setTestPackets: (files: File[]) => void;
  setLlmConfig: (c: LlmConfig) => void;
  handleCreateSession: () => Promise<void>;
  handleRunStep: (stepId: string) => Promise<void>;
  handleToggleType: (id: string) => void;
  handleRestoreSelection: () => void;
  handleResumeSession: (sessionId: string) => Promise<void>;
  addLog: (level: LogEntry['level'], message: string) => void;
  clearLogs: () => void;
  resetSession: () => void;
}

// ── Context ───────────────────────────────────────────────────────

const StateContext = createContext<PipelineState | null>(null);
const ActionsContext = createContext<PipelineActions | null>(null);

export function usePipelineState() {
  const ctx = useContext(StateContext);
  if (!ctx) throw new Error('usePipelineState must be used within PipelineProvider');
  return ctx;
}

export function usePipelineActions() {
  const ctx = useContext(ActionsContext);
  if (!ctx) throw new Error('usePipelineActions must be used within PipelineProvider');
  return ctx;
}

// ── Helpers ───────────────────────────────────────────────────────

const DEFAULT_LLM_CONFIG: LlmConfig = {
  api_key: '',
  base_url: '',
  model: '',
  temperature: 0.7,
  embedding_model: '',
  embedding_base_url: '',
  embedding_api_key: '',
};

const now = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
};

function parsePacketTypes(raw: string): PacketType[] {
  const names = raw.split(',').map(s => s.trim()).filter(Boolean);
  return names.map((name, i) => ({
    id: String(i + 1),
    name,
    description: '',
    selected: true,
  }));
}

// ── Provider ──────────────────────────────────────────────────────

export function PipelineProvider({ children }: { children: ReactNode }) {
  // Session
  const [sessionId, setSessionId] = useState<string | null>(() => {
    try { return localStorage.getItem(SESSION_KEY); } catch { return null; }
  });
  const [running, setRunning] = useState(false);
  const [protocolName, setProtocolName] = useState('');
  const [specFile, setSpecFile] = useState<File | null>(null);
  const [testPackets, setTestPackets] = useState<File[]>([]);
  const [llmConfig, setLlmConfig] = useState<LlmConfig>(DEFAULT_LLM_CONFIG);

  // Results
  const [packetTypes, setPacketTypes] = useState<PacketType[]>([]);
  const [datamodel, setDatamodel] = useState('');
  const [validationPassed, setValidationPassed] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [mutators, setMutators] = useState<MutatorInfo[]>([]);
  const [testResults, setTestResults] = useState<MutatorTestResult[]>([]);
  const [packageReady, setPackageReady] = useState(false);
  const [packagePath, setPackagePath] = useState('');

  // Logs
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logIdRef = useRef(0);

  const addLog = useCallback((level: LogEntry['level'], message: string) => {
    logIdRef.current += 1;
    setLogs((prev) => [...prev.slice(-499), { id: logIdRef.current, time: now(), level, message }]);
  }, []);

  const clearLogs = useCallback(() => setLogs([]), []);

  // ── WebSocket log streaming ────────────────────────────────────

  useEffect(() => {
    if (!sessionId) return;
    const cleanup = connectLogSocket(sessionId, (line) => {
      const level: LogEntry['level'] =
        line.includes('[ERROR]') ? 'error' :
        line.includes('[WARN]') ? 'warning' :
        line.includes('[OK]') || line.includes('[PASS]') ? 'success' :
        'info';
      addLog(level, line);
    });
    return cleanup;
  }, [sessionId, addLog]);

  // ── Derived ─────────────────────────────────────────────────────

  const stepCompleted: Record<string, boolean> = {
    upload: sessionId !== null,
    extract: packetTypes.length > 0,
    datamodel: datamodel.length > 0,
    validate: validationPassed,
    mutators: mutators.length > 0 && mutators.every((m) => m.status === 'ready'),
    test: testResults.length > 0 && testResults.every((r) => r.status === 'passed'),
    package: packageReady,
  };

  const currentStep: string = (() => {
    const idx = STEPS.findIndex((s) => !stepCompleted[s.id]);
    return idx === -1 ? STEPS[STEPS.length - 1].id : STEPS[idx].id;
  })();

  const getStepStatus = useCallback(
    (id: string) => {
      if (id === currentStep && running) return 'running';
      if (stepCompleted[id]) return 'success';
      if (id === currentStep) return 'active';
      return 'pending';
    },
    [currentStep, running, stepCompleted],
  );

  const canAccessStep = useCallback(
    (id: string): boolean => {
      const targetIdx = STEPS.findIndex((s) => s.id === id);
      for (let i = 0; i < targetIdx; i++) {
        if (!stepCompleted[STEPS[i].id]) return false;
      }
      return true;
    },
    [stepCompleted],
  );

  // ── Actions ─────────────────────────────────────────────────────

  /** Create a session via the API (upload step). */
  const handleCreateSession = async () => {
    if (!specFile || testPackets.length === 0 || !protocolName.trim()) {
      addLog('warning', '请填写协议名称并上传规范文档和测试数据包。');
      return;
    }

    addLog('info', '正在创建会话并上传文件...');
    setRunning(true);
    try {
      const formData = new FormData();
      formData.append('protocol', protocolName.trim());
      formData.append('fixer', 'false');
      formData.append('rfc_file', specFile);
      for (const f of testPackets) {
        formData.append('seed_files', f);
      }

      // Only include llm_config if any field is non-empty.
      const hasLlmConfig = Object.values(llmConfig).some(v => v !== '' && v !== undefined);
      if (hasLlmConfig) {
        formData.append('llm_config', JSON.stringify({
          api_key: llmConfig.api_key || undefined,
          base_url: llmConfig.base_url || undefined,
          model: llmConfig.model || undefined,
          temperature: llmConfig.temperature,
          embedding_model: llmConfig.embedding_model || undefined,
          embedding_base_url: llmConfig.embedding_base_url || undefined,
          embedding_api_key: llmConfig.embedding_api_key || undefined,
        }));
      }

      const res = await api.createSession(formData);
      setSessionId(res.session_id);
      try { localStorage.setItem(SESSION_KEY, res.session_id); } catch { /* noop */ }
      addLog('success', `会话已创建: ${res.session_id} (协议: ${res.protocol})`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      addLog('error', `创建会话失败: ${msg}`);
    } finally {
      setRunning(false);
    }
  };

  /** Run a step via the API. */
  const handleRunStep = async (stepId: string) => {
    if (!sessionId) {
      addLog('error', '没有活动会话，请先上传文件创建会话。');
      return;
    }

    const stepDef = STEPS.find(s => s.stepId === stepId);
    const title = stepDef?.title ?? stepId;
    addLog('info', `开始执行: ${title}`);

    setRunning(true);
    try {
      const params: api.RunStepParams = {};
      if (stepId === 'step_4') {
        const selected = packetTypes.filter(p => p.selected).map(p => p.name);
        params.selected_types = selected;
      }

      const res = await api.runStep(sessionId, stepId, params);

      if (res.status === 'completed') {
        addLog('success', `${title} 完成`);
        handleStepResult(stepId, res);
      } else {
        addLog('error', `${title} 失败: ${res.error ?? '未知错误'}`);
        handleStepResult(stepId, res);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      addLog('error', `${title} 出错: ${msg}`);

      // Server restarted → session lost. Clear and redirect to upload.
      if (msg.includes('Session not found')) {
        addLog('warning', '会话已失效（服务器可能已重启），请重新上传文件创建会话。');
        resetSession();
      }
    } finally {
      setRunning(false);
    }
  };

  /** Map API response to frontend state. */
  function handleStepResult(stepId: string, res: { status: string; output?: string | null; llm_outputs?: string[] | null; error?: string | null }) {
    switch (stepId) {
      case 'step_1': {
        const raw = res.llm_outputs?.[0] ?? '';
        const types = parsePacketTypes(raw);
        setPacketTypes(types);
        addLog('info', `提取到 ${types.length} 种类型: ${types.map(t => t.name).join(', ')}`);
        break;
      }
      case 'step_2': {
        const content = res.llm_outputs?.[0] ?? (res.llm_outputs?.join('\n\n') ?? '');
        setDatamodel(content);
        break;
      }
      case 'step_3': {
        if (res.status === 'completed') {
          setValidationPassed(true);
          setValidationError(null);
        } else {
          setValidationPassed(false);
          setValidationError(res.error ?? 'Validation failed after retries');
        }
        break;
      }
      case 'step_4': {
        const out = res.llm_outputs?.join('\n') ?? '';
        const types = packetTypes.filter(p => p.selected);
        const infos: MutatorInfo[] = types.map(p => ({
          packetType: p.name,
          status: 'ready' as const,
          syntaxValid: true,
          code: out.includes(p.name) ? `// Generated for ${p.name}\n${out}` : out,
        }));
        setMutators(infos);
        break;
      }
      case 'step_5': {
        const types = packetTypes.filter(p => p.selected);
        const results: MutatorTestResult[] = types.map(p => ({
          packetType: p.name,
          totalTests: 0,
          passed: res.status === 'completed' ? 100 : 0,
          failed: res.status === 'completed' ? 0 : 100,
          parseFailures: 0,
          runtimeErrors: res.status === 'completed' ? 0 : 100,
          status: res.status === 'completed' ? 'passed' as const : 'failed' as const,
          repairAttempts: 0,
          issues: res.error ? [res.error] : [],
        }));
        setTestResults(results);
        break;
      }
      case 'step_final': {
        if (res.status === 'completed') {
          setPackageReady(true);
          setPackagePath(res.output ?? 'Compiled successfully');
        }
        break;
      }
    }
  }

  const handleToggleType = (id: string) => {
    setPacketTypes((prev) =>
      prev.map((p) => (p.id === id ? { ...p, selected: !p.selected } : p)),
    );
  };

  const handleRestoreSelection = () => {
    setPacketTypes((prev) =>
      prev.map((p) => ({ ...p, selected: true })),
    );
  };

  const resetSession = () => {
    setSessionId(null);
    try { localStorage.removeItem(SESSION_KEY); } catch { /* noop */ }
    setPacketTypes([]);
    setDatamodel('');
    setValidationPassed(false);
    setValidationError(null);
    setMutators([]);
    setTestResults([]);
    setPackageReady(false);
    setPackagePath('');
    setRunning(false);
  };

  /** Resume an existing session by loading its state from the API. */
  const handleResumeSession = async (sid: string) => {
    addLog('info', '正在加载会话...');
    setRunning(true);
    try {
      const detail = await api.getSession(sid);
      setSessionId(sid);
      try { localStorage.setItem(SESSION_KEY, sid); } catch { /* noop */ }
      setProtocolName(detail.protocol);

      // Restore step results from session detail.
      if (detail.packet_types && detail.packet_types.length > 0) {
        setPacketTypes(detail.packet_types.map((name, i) => ({
          id: String(i + 1), name, description: '', selected: true,
        })));
      }

      // Derive step completion from API step statuses.
      const s = detail.steps;
      if (s['step_3']?.status === 'completed') setValidationPassed(true);
      if (s['step_4']?.status === 'completed') {
        const types = detail.packet_types || [];
        setMutators(types.map(name => ({
          packetType: name, status: 'ready' as const, syntaxValid: true,
        })));
      }
      if (s['step_5']?.status === 'completed') {
        const types = detail.packet_types || [];
        setTestResults(types.map(name => ({
          packetType: name, totalTests: 100, passed: 100, failed: 0,
          parseFailures: 0, runtimeErrors: 0, status: 'passed' as const,
          repairAttempts: 0, issues: [],
        })));
      }
      if (s['step_final']?.status === 'completed') {
        setPackageReady(true);
        setPackagePath(s['step_final']?.error || '');
      }

      addLog('success', `已恢复会话: ${sid} (${detail.protocol})`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      addLog('error', `加载会话失败: ${msg}`);
    } finally {
      setRunning(false);
    }
  };

  // ── Assemble ────────────────────────────────────────────────────

  const state: PipelineState = {
    currentStep,
    running,
    sessionId,
    protocolName,
    specFile,
    testPackets,
    llmConfig,
    packetTypes,
    datamodel,
    validationPassed,
    validationError,
    mutators,
    testResults,
    packageReady,
    packagePath,
    logs,
    stepCompleted,
    getStepStatus,
    canAccessStep,
  };

  const actions: PipelineActions = {
    setProtocolName,
    setSpecFile,
    setTestPackets,
    setLlmConfig,
    handleCreateSession,
    handleRunStep,
    handleToggleType,
    handleRestoreSelection,
    handleResumeSession,
    addLog,
    clearLogs,
    resetSession,
  };

  return (
    <StateContext.Provider value={state}>
      <ActionsContext.Provider value={actions}>
        {children}
      </ActionsContext.Provider>
    </StateContext.Provider>
  );
}
