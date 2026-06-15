import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';
import type { MutatorInfo } from '../types';

const statusLabel: Record<MutatorInfo['status'], string> = {
  pending: '等待中',
  generating: '生成中',
  validating: '语法校验',
  ready: '就绪',
  error: '错误',
};

export default function GenerateMutators() {
  const { running, packetTypes, mutators, sessionId } = usePipelineState();
  const { handleToggleType, handleRestoreSelection, handleRunStep } = usePipelineActions();
  const navigate = useNavigate();

  const stepIdx = STEPS.findIndex((s) => s.id === 'mutators');
  const prev = STEPS[stepIdx - 1];
  const next = STEPS[stepIdx + 1];
  const selectedCount = packetTypes.filter((p) => p.selected).length;
  const readyCount = mutators.filter((m) => m.status === 'ready').length;
  const allMutatorsReady = readyCount === selectedCount && selectedCount > 0 && !running;

  if (!sessionId) {
    return (
      <div className="px-4 py-2.5 rounded-sm text-[13px] bg-amber-50 border border-warning text-amber-800">
        请先上传文件并创建会话。
      </div>
    );
  }

  if (packetTypes.length === 0) {
    return (
      <div className="px-4 py-2.5 rounded-sm text-[13px] bg-amber-50 border border-warning text-amber-800">
        尚未提取数据包类型，请先完成「类型提取」步骤。
      </div>
    );
  }

  return (
    <div className="relative">
      <div className="absolute top-0 right-0">
        <button
          className="h-7 px-3 text-xs rounded-sm border border-border bg-white text-body cursor-pointer hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
          onClick={() => prev && navigate(prev.path)}
          disabled={running}
        >
          ← 返回上一步
        </button>
      </div>

      <h2 className="text-[16px] font-semibold text-title m-0 mb-2 pr-28">变异器生成</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        调用大模型根据 DataModel 为每种数据包类型生成 C# 变异器代码。可多选类型。
      </p>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3 mb-5">
        {packetTypes.map((pt) => (
          <label
            key={pt.id}
            className={`flex items-start gap-3 px-4 py-3 rounded-sm border cursor-pointer transition-colors
              ${pt.selected ? 'border-primary bg-blue-50/50' : 'border-border bg-white hover:border-gray-300'}
              ${running ? 'opacity-60 cursor-not-allowed' : ''}`}
          >
            <input
              type="checkbox"
              checked={pt.selected}
              onChange={() => handleToggleType(pt.id)}
              disabled={running}
            />
            <div>
              <code className="block text-xs text-primary">{pt.name}</code>
            </div>
          </label>
        ))}
      </div>

      <div className="flex gap-3">
        <button
          className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
            hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
          onClick={() => handleRunStep('step_4')}
          disabled={running || selectedCount === 0}
        >
          {running ? '生成中...' : `生成变异器 (${selectedCount} 个类型)`}
        </button>
        <button
          className="h-8 px-4 text-[13px] rounded-sm border border-border bg-white text-body cursor-pointer hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          onClick={handleRestoreSelection}
          disabled={running}
        >
          恢复全选
        </button>
      </div>

      {mutators.length > 0 && (
        <div className="mt-6 flex flex-col gap-3">
          {mutators.map((m) => (
            <div
              key={m.packetType}
              className={`p-4 rounded-sm border ${m.status === 'ready' ? 'border-success' : 'border-border'} bg-white`}
            >
              <div className="flex items-center gap-3 mb-2">
                <code className="text-xs text-primary font-semibold">{m.packetType}</code>
                <span className={`px-2 py-0.5 rounded-sm text-[11px] ${m.status === 'ready' ? 'bg-green-100 text-success' : 'bg-gray-100 text-muted'}`}>
                  {statusLabel[m.status]}
                </span>
              </div>
              {m.code && (
                <pre className="m-0 p-3 bg-gray-50 border border-border rounded-sm text-[11px] leading-relaxed overflow-x-auto max-h-48">
                  <code>{m.code}</code>
                </pre>
              )}
            </div>
          ))}
        </div>
      )}

      {allMutatorsReady && (
        <div className="mt-4 px-4 py-2.5 rounded-sm text-[13px] bg-green-50 border border-success text-success">
          所有变异器已生成并通过语法校验。
        </div>
      )}

      {next && (
        <div className="flex gap-3 mt-8 pt-6 border-t border-border">
          <button
            className="h-8 px-4 text-[13px] rounded-sm border border-border bg-panel text-body cursor-pointer hover:bg-gray-50
              disabled:opacity-40 disabled:cursor-not-allowed disabled:text-muted"
            onClick={() => navigate(next.path)}
            disabled={!allMutatorsReady}
          >
            下一步：{next.title}
          </button>
        </div>
      )}
    </div>
  );
}
