import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';

export default function GenerateDataModel() {
  const { running, datamodel, stepCompleted } = usePipelineState();
  const { handleRunStep } = usePipelineActions();
  const navigate = useNavigate();

  const stepIdx = STEPS.findIndex((s) => s.id === 'datamodel');
  const prev = STEPS[stepIdx - 1];
  const next = STEPS[stepIdx + 1];

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

      <h2 className="text-[16px] font-semibold text-title m-0 mb-2 pr-28">DataModel 生成</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        调用大模型根据规范文档生成协议的 Peach Pit DataModel（XML 格式）。
      </p>

      <button
        className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
          hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
        onClick={() => handleRunStep('step_2')}
        disabled={running}
      >
        {running ? '生成中...' : '生成 DataModel'}
      </button>

      {datamodel && (
        <div className="mt-6">
          <span className="px-3 py-1 text-xs rounded-sm bg-primary text-white">DataModel</span>
          <pre className="mt-3 m-0 p-4 bg-gray-50 border border-border rounded-sm text-xs leading-relaxed overflow-x-auto max-h-96">
            <code>{datamodel}</code>
          </pre>
        </div>
      )}

      {next && (
        <div className="flex gap-3 mt-8 pt-6 border-t border-border">
          <button
            className="h-8 px-4 text-[13px] rounded-sm border border-border bg-panel text-body cursor-pointer hover:bg-gray-50
              disabled:opacity-40 disabled:cursor-not-allowed disabled:text-muted"
            onClick={() => navigate(next.path)}
            disabled={!stepCompleted['datamodel']}
          >
            下一步：{next.title}
          </button>
        </div>
      )}
    </div>
  );
}
