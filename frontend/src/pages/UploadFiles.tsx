import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';
import { listSessions } from '../services/api';
import type { SessionSummary } from '../types';

export default function UploadFiles() {
  const { protocolName, specFile, testPackets, llmConfig, running, stepCompleted } = usePipelineState();
  const { setProtocolName, setSpecFile, setTestPackets, setLlmConfig, handleCreateSession, handleResumeSession } = usePipelineActions();
  const navigate = useNavigate();
  const [showLlm, setShowLlm] = useState(false);

  // Existing sessions
  const [existingSessions, setExistingSessions] = useState<SessionSummary[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(true);

  const loadSessions = async () => {
    setLoadingSessions(true);
    try {
      const list = await listSessions();
      setExistingSessions(list);
    } catch { /* ignore */ }
    finally { setLoadingSessions(false); }
  };

  useEffect(() => { loadSessions(); }, []);

  const onResume = async (sid: string) => {
    await handleResumeSession(sid);
  };

  const stepIdx = STEPS.findIndex((s) => s.id === 'upload');
  const next = STEPS[stepIdx + 1];

  const onCreate = async () => {
    await handleCreateSession();
  };

  const updateLlm = (patch: Partial<typeof llmConfig>) => {
    setLlmConfig({ ...llmConfig, ...patch });
  };

  return (
    <div className="relative">
      <h2 className="text-[16px] font-semibold text-title m-0 mb-2">上传文件</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        填写协议名称，并上传协议规范文档（TXT/PDF）和用于校验的测试数据包。
      </p>

      <div className="mb-5">
        <label className="block text-xs text-muted font-medium mb-1" htmlFor="protocol-name">
          协议名称
        </label>
        <p className="text-xs text-muted mb-2 leading-relaxed">
          用于标识当前协议项目，将用于模型命名与打包输出路径。
        </p>
        <input
          id="protocol-name"
          className="h-7 w-full border border-border rounded-sm px-2 text-xs bg-white
            focus:outline-none focus:border-primary placeholder:text-muted"
          type="text"
          value={protocolName}
          onChange={(e) => setProtocolName(e.target.value)}
          placeholder="例如：MQTT、HTTP/1.1、Modbus TCP"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div className="p-6 bg-panel-secondary border-2 border-dashed border-border rounded-sm text-center hover:border-primary transition-colors">
          <div className="text-2xl mb-2">📄</div>
          <h3 className="text-[14px] font-semibold text-body m-0 mb-1">规范文档</h3>
          <p className="text-xs text-muted m-0 mb-4">支持 .txt .pdf</p>
          <label className="inline-block h-7 px-3 text-xs rounded-sm border border-border bg-white text-body cursor-pointer hover:bg-gray-50 leading-7">
            选择文件
            <input
              type="file"
              accept=".txt,.pdf"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) setSpecFile(f);
              }}
            />
          </label>
          {specFile && (
            <div className="mt-4 flex justify-between items-center px-2 py-1.5 bg-white rounded-sm text-xs text-left">
              <span>{specFile.name}</span>
              <span className="text-muted">{(specFile.size / 1024).toFixed(1)} KB</span>
            </div>
          )}
        </div>

        <div className="p-6 bg-panel-secondary border-2 border-dashed border-border rounded-sm text-center hover:border-primary transition-colors">
          <div className="text-2xl mb-2">📦</div>
          <h3 className="text-[14px] font-semibold text-body m-0 mb-1">测试数据包</h3>
          <p className="text-xs text-muted m-0 mb-4">支持任意格式（可多选）</p>
          <label className="inline-block h-7 px-3 text-xs rounded-sm border border-border bg-white text-body cursor-pointer hover:bg-gray-50 leading-7">
            选择文件
            <input
              type="file"
              accept="*"
              multiple
              hidden
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                if (files.length) setTestPackets(files);
              }}
            />
          </label>
          {testPackets.length > 0 && (
            <ul className="list-none p-0 m-0 mt-4 text-left">
              {testPackets.map((f, i) => (
                <li key={i} className="flex justify-between px-2 py-1 text-xs border-b border-border-light last:border-b-0">
                  <span>{f.name}</span>
                  <span className="text-muted">{(f.size / 1024).toFixed(1)} KB</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* LLM Config */}
      <div className="mt-5 border border-border rounded-sm">
        <button
          className="w-full h-8 px-3 text-xs font-medium text-body bg-slate-50 hover:bg-gray-100 flex items-center gap-2 cursor-pointer"
          onClick={() => setShowLlm(!showLlm)}
        >
          <span>{showLlm ? '▾' : '▸'}</span>
          LLM 配置（可选，留空使用服务器默认值）
        </button>
        {showLlm && (
          <div className="p-4 grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[11px] text-muted mb-0.5">API Key</label>
              <input
                type="password"
                className="h-7 w-full border border-border rounded-sm px-2 text-xs"
                value={llmConfig.api_key}
                onChange={e => updateLlm({ api_key: e.target.value })}
                placeholder="sk-..."
              />
            </div>
            <div>
              <label className="block text-[11px] text-muted mb-0.5">Base URL</label>
              <input
                type="text"
                className="h-7 w-full border border-border rounded-sm px-2 text-xs"
                value={llmConfig.base_url}
                onChange={e => updateLlm({ base_url: e.target.value })}
                placeholder="https://api.openai.com/v1"
              />
            </div>
            <div>
              <label className="block text-[11px] text-muted mb-0.5">Model</label>
              <input
                type="text"
                className="h-7 w-full border border-border rounded-sm px-2 text-xs"
                value={llmConfig.model}
                onChange={e => updateLlm({ model: e.target.value })}
                placeholder="gpt-5.4"
              />
            </div>
            <div>
              <label className="block text-[11px] text-muted mb-0.5">
                Temperature: <span className="text-body font-medium">{llmConfig.temperature}</span>
              </label>
              <input
                type="range"
                min="0"
                max="2"
                step="0.1"
                className="w-full h-3"
                value={llmConfig.temperature}
                onChange={e => updateLlm({ temperature: parseFloat(e.target.value) })}
              />
            </div>
            <div className="col-span-2">
              <label className="block text-[11px] text-muted mb-0.5">Embedding API Key（可选，默认同 API Key）</label>
              <input
                type="password"
                className="h-7 w-full border border-border rounded-sm px-2 text-xs"
                value={llmConfig.embedding_api_key}
                onChange={e => updateLlm({ embedding_api_key: e.target.value })}
                placeholder="留空则使用上方 API Key"
              />
            </div>
            <div>
              <label className="block text-[11px] text-muted mb-0.5">Embedding Base URL</label>
              <input
                type="text"
                className="h-7 w-full border border-border rounded-sm px-2 text-xs"
                value={llmConfig.embedding_base_url}
                onChange={e => updateLlm({ embedding_base_url: e.target.value })}
                placeholder="留空则使用上方 Base URL"
              />
            </div>
            <div>
              <label className="block text-[11px] text-muted mb-0.5">Embedding Model</label>
              <input
                type="text"
                className="h-7 w-full border border-border rounded-sm px-2 text-xs"
                value={llmConfig.embedding_model}
                onChange={e => updateLlm({ embedding_model: e.target.value })}
                placeholder="text-embedding-ada-002"
              />
            </div>
          </div>
        )}
      </div>

      {/* Existing sessions */}
      {!loadingSessions && existingSessions.length > 0 && (
        <div className="mt-6 border border-border rounded-sm">
          <div className="h-8 px-3 flex items-center bg-slate-50 border-b border-border text-xs font-medium text-muted">
            继续已有会话（{existingSessions.length}）
          </div>
          <div className="divide-y divide-border">
            {existingSessions.map((s) => (
              <div key={s.session_id} className="flex items-center justify-between px-3 py-2 hover:bg-gray-50">
                <div>
                  <span className="text-xs font-medium text-body">{s.protocol}</span>
                  <span className="ml-2 text-[11px] text-muted">
                    {s.completed_steps}/{s.total_steps} 步完成
                  </span>
                  <span className={`ml-2 px-1.5 py-0.5 rounded-sm text-[10px] ${
                    s.status === 'completed' ? 'bg-green-100 text-success' :
                    s.status === 'failed' ? 'bg-red-100 text-error' :
                    s.status === 'running' ? 'bg-blue-100 text-primary' :
                    'bg-gray-100 text-muted'
                  }`}>
                    {s.status === 'completed' ? '已完成' :
                     s.status === 'failed' ? '失败' :
                     s.status === 'running' ? '运行中' : '待继续'}
                  </span>
                  <span className="ml-2 text-[10px] text-muted font-mono">{s.session_id}</span>
                </div>
                <button
                  className="h-6 px-3 text-[11px] rounded-sm border border-primary text-primary bg-white cursor-pointer hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed"
                  onClick={() => onResume(s.session_id)}
                  disabled={running}
                >
                  继续
                </button>
              </div>
            ))}
          </div>
          <div className="h-7 px-3 flex items-center border-t border-border bg-slate-50">
            <button
              className="text-[11px] text-muted hover:text-body cursor-pointer bg-transparent border-0"
              onClick={loadSessions}
            >
              ↻ 刷新
            </button>
          </div>
        </div>
      )}

      {/* Create session button */}
      <div className="flex gap-3 mt-6">
        <button
          className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
            hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
          onClick={onCreate}
          disabled={running || !specFile || testPackets.length === 0 || !protocolName.trim()}
        >
          {running ? '创建中...' : '创建会话'}
        </button>
      </div>

      {/* Next-step nav */}
      {next && (
        <div className="flex gap-3 mt-8 pt-6 border-t border-border">
          <button
            className="h-8 px-4 text-[13px] rounded-sm border border-border bg-panel text-body cursor-pointer hover:bg-gray-50
              disabled:opacity-40 disabled:cursor-not-allowed disabled:text-muted"
            onClick={() => navigate(next.path)}
            disabled={!stepCompleted['upload']}
          >
            下一步：{next.title}
          </button>
        </div>
      )}
    </div>
  );
}
