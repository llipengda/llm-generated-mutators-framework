import { useNavigate, useLocation } from 'react-router-dom';
import { usePipelineState, STEPS, type PipelineStep } from '../../context/PipelineContext';

const statusIcon: Record<string, string> = {
  pending: '○',
  active: '◉',
  running: '◎',
  success: '✓',
  error: '✕',
  warning: '!',
};

const statusColors: Record<string, string> = {
  pending: 'text-muted',
  active: 'text-primary',
  running: 'text-warning',
  success: 'text-success',
  error: 'text-error',
  warning: 'text-warning',
};

export function StepIndicator() {
  const { getStepStatus, canAccessStep } = usePipelineState();
  const navigate = useNavigate();
  const location = useLocation();

  const handleClick = (step: PipelineStep) => {
    if (canAccessStep(step.id)) {
      navigate(step.path);
    }
  };

  return (
    <nav className="flex bg-panel border-b border-border overflow-x-auto shrink-0">
      {STEPS.map((step, idx) => {
        const status = getStepStatus(step.id);
        const isCurrent = step.path === location.pathname;
        const accessible = canAccessStep(step.id);

        return (
          <button
            key={step.id}
            className={`relative flex flex-col items-center gap-0.5 py-2.5 px-4 min-w-[100px] bg-transparent border-0 cursor-pointer transition-colors
              ${!accessible ? 'opacity-40 cursor-not-allowed text-muted' : ''}
              ${accessible ? statusColors[status] : ''}
              ${isCurrent && accessible ? 'border-b-2 border-primary' : 'border-b-2 border-transparent'}
              ${accessible && !isCurrent ? 'hover:text-body' : ''}
              ${status === 'running' && accessible ? 'animate-pulse' : ''}
            `}
            onClick={() => handleClick(step)}
            disabled={!accessible}
            title={accessible ? step.description : '请先完成前面的步骤'}
          >
            <span className="text-[10px] font-bold tracking-wider opacity-70">
              {idx + 1}
            </span>
            <span className="text-sm leading-none">{statusIcon[status]}</span>
            <span className="text-[11px] whitespace-nowrap text-center">{step.title}</span>
            {idx < STEPS.length - 1 && (
              <span className="absolute -right-2.5 top-1/2 w-5 h-0.5 bg-border -translate-y-1/2 pointer-events-none" />
            )}
          </button>
        );
      })}
    </nav>
  );
}
