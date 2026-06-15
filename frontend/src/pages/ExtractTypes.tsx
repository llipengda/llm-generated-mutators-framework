import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';

export default function ExtractTypes() {
  const { running, packetTypes, stepCompleted } = usePipelineState();
  const { handleRunStep } = usePipelineActions();
  const navigate = useNavigate();

  const stepIdx = STEPS.findIndex((s) => s.id === 'extract');
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

      <h2 className="text-[16px] font-semibold text-title m-0 mb-2 pr-28">类型提取</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        调用大模型从规范文档中自动提取协议数据包类型。
      </p>

      <button
        className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
          hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
        onClick={() => handleRunStep('step_1')}
        disabled={running}
      >
        {running ? '提取中...' : '开始提取'}
      </button>

      {packetTypes.length > 0 && (
        <div className="mt-6 overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead className="bg-slate-50">
              <tr>
                <th className="h-8 px-3 text-left font-medium text-muted border-b border-border">序号</th>
                <th className="h-8 px-3 text-left font-medium text-muted border-b border-border">数据包类型</th>
              </tr>
            </thead>
            <tbody>
              {packetTypes.map((pt, i) => (
                <tr key={pt.id} className="hover:bg-slate-50">
                  <td className="h-8 px-3 border-b border-border">{i + 1}</td>
                  <td className="h-8 px-3 border-b border-border">
                    <code className="text-primary text-xs">{pt.name}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {next && (
        <div className="flex gap-3 mt-8 pt-6 border-t border-border">
          <button
            className="h-8 px-4 text-[13px] rounded-sm border border-border bg-panel text-body cursor-pointer hover:bg-gray-50
              disabled:opacity-40 disabled:cursor-not-allowed disabled:text-muted"
            onClick={() => navigate(next.path)}
            disabled={!stepCompleted['extract']}
          >
            下一步：{next.title}
          </button>
        </div>
      )}
    </div>
  );
}
