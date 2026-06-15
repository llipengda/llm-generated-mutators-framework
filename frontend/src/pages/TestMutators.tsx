import { useNavigate } from 'react-router-dom';
import { usePipelineState, usePipelineActions, STEPS } from '../context/PipelineContext';
import type { MutatorTestResult } from '../types';

const statusLabel: Record<MutatorTestResult['status'], string> = {
  pending: '等待',
  running: '测试中',
  passed: '通过',
  failed: '失败',
  repairing: '修复中',
};

export default function TestMutators() {
  const { running, testResults, stepCompleted } = usePipelineState();
  const { handleRunStep } = usePipelineActions();
  const navigate = useNavigate();

  const stepIdx = STEPS.findIndex((s) => s.id === 'test');
  const prev = STEPS[stepIdx - 1];
  const next = STEPS[stepIdx + 1];
  const allPassed = testResults.length > 0 && testResults.every((r) => r.status === 'passed');
  const hasResults = testResults.length > 0;

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

      <h2 className="text-[16px] font-semibold text-title m-0 mb-2 pr-28">变异器检验&修复</h2>
      <p className="text-[13px] text-muted m-0 mb-6 leading-relaxed">
        对每个变异器执行测试，检查变异结果是否可解析。不合格的变异器将自动修复。
        <em className="text-warning not-italic ml-1">（耗时较长）</em>
      </p>

      <button
        className="h-8 px-5 text-[13px] rounded-sm bg-primary border border-primary text-white cursor-pointer
          hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed"
        onClick={() => handleRunStep('step_5')}
        disabled={running}
      >
        {running ? '测试进行中...' : hasResults ? '重新测试' : '开始检验'}
      </button>

      {hasResults && (
        <div className="mt-6 flex flex-col gap-4">
          {testResults.map((r) => (
            <div
              key={r.packetType}
              className={`p-4 rounded-sm border ${
                r.status === 'passed' ? 'border-success'
                  : r.status === 'failed' ? 'border-error'
                    : 'border-border'
              } bg-white`}
            >
              <div className="flex justify-between items-center mb-3">
                <code className="text-xs text-primary font-semibold">{r.packetType}</code>
                <span className={`px-2 py-0.5 rounded-sm text-[11px] ${
                  r.status === 'passed' ? 'bg-green-100 text-success'
                    : r.status === 'failed' ? 'bg-red-100 text-error'
                      : 'bg-gray-100 text-muted'
                }`}>
                  {statusLabel[r.status]}
                </span>
              </div>

              <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px]">
                <span className="text-success">通过: {r.passed}</span>
                <span className="text-error">失败: {r.failed}</span>
                {r.repairAttempts > 0 && (
                  <span className="text-warning">修复次数: {r.repairAttempts}</span>
                )}
              </div>

              {r.issues.length > 0 && (
                <ul className="m-0 mt-3 pl-5 text-xs">
                  {r.issues.map((issue, i) => (
                    <li key={i} className="text-muted leading-relaxed">{issue}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      {allPassed && (
        <div className="mt-4 px-4 py-2.5 rounded-sm text-[13px] bg-green-50 border border-success text-success">
          所有变异器检验通过。
        </div>
      )}

      {next && (
        <div className="flex gap-3 mt-8 pt-6 border-t border-border">
          <button
            className="h-8 px-4 text-[13px] rounded-sm border border-border bg-panel text-body cursor-pointer hover:bg-gray-50
              disabled:opacity-40 disabled:cursor-not-allowed disabled:text-muted"
            onClick={() => navigate(next.path)}
            disabled={!stepCompleted['test']}
          >
            下一步：{next.title}
          </button>
        </div>
      )}
    </div>
  );
}
