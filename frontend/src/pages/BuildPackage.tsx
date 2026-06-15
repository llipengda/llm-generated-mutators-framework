import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';

export default function BuildPackage() {
  const { running, protocolName, packageReady, packagePath, mutators } = usePipelineState();
  const { handleRunStep } = usePipelineActions();
  const navigate = useNavigate();

  const stepIdx = STEPS.findIndex((s) => s.id === 'package');
  const prev = STEPS[stepIdx - 1];

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

      <h2 className="text-[16px] font-semibold text-title m-0 mb-2 pr-28">打包</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        编译所有变异器和修复器 C# 代码为最终动态库。
      </p>

      <div className="flex gap-8 px-4 py-2.5 bg-panel-secondary border border-border rounded-sm mb-4 text-[13px]">
        <span>协议名称: <strong className="text-body">{protocolName.trim() || '—'}</strong></span>
        <span>变异器数量: <strong className="text-body">{mutators.length}</strong></span>
      </div>

      <button
        className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
          hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
        onClick={() => handleRunStep('step_final')}
        disabled={running || packageReady}
      >
        {running ? '编译中...' : packageReady ? '编译完成' : '编译打包'}
      </button>

      {packageReady && (
        <div className="mt-6">
          <div className="px-4 py-2.5 rounded-sm text-[13px] bg-green-50 border border-success text-success mb-4">
            编译打包成功
          </div>
          <div className="px-4 py-3 bg-panel-secondary border border-border rounded-sm mb-4">
            <span className="block text-[11px] text-muted mb-1">输出路径</span>
            <code className="text-primary text-xs">{packagePath}</code>
          </div>
        </div>
      )}
    </div>
  );
}
