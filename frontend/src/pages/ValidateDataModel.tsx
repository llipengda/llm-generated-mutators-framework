import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';

export default function ValidateDataModel() {
  const { running, validationPassed, validationError, testPackets, stepCompleted } = usePipelineState();
  const { handleRunStep } = usePipelineActions();
  const navigate = useNavigate();

  const stepIdx = STEPS.findIndex((s) => s.id === 'validate');
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

      <h2 className="text-[16px] font-semibold text-title m-0 mb-2 pr-28">DataModel 检验&修复</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        使用测试数据包解析 DataModel，验证模型正确性。若失败，大模型将自动尝试修复（最多 3 次）。
      </p>

      <div className="flex gap-8 px-4 py-2.5 bg-panel-secondary border border-border rounded-sm mb-4 text-[13px]">
        <span>测试包数量: <strong className="text-body">{testPackets.length}</strong></span>
      </div>

      {!validationPassed && (
        <button
          className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
            hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
          onClick={() => handleRunStep('step_3')}
          disabled={running}
        >
          {running ? '检验中...' : '开始检验'}
        </button>
      )}

      {validationError && (
        <div className="mt-4 px-4 py-2.5 rounded-sm text-[13px] bg-red-50 border border-error text-error">
          {validationError}
        </div>
      )}

      {validationPassed && (
        <div className="mt-4 px-4 py-2.5 rounded-sm text-[13px] bg-green-50 border border-success text-success">
          DataModel 校验通过，所有测试包解析成功。
        </div>
      )}

      {next && (
        <div className="flex gap-3 mt-8 pt-6 border-t border-border">
          <button
            className="h-8 px-4 text-[13px] rounded-sm border border-border bg-panel text-body cursor-pointer hover:bg-gray-50
              disabled:opacity-40 disabled:cursor-not-allowed disabled:text-muted"
            onClick={() => navigate(next.path)}
            disabled={!stepCompleted['validate']}
          >
            下一步：{next.title}
          </button>
        </div>
      )}
    </div>
  );
}
