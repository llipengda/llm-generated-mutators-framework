import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { usePipelineState, STEPS } from '../context/PipelineContext';

/**
 * Redirects to the first incomplete step if the user tries to access
 * a step whose prerequisites haven't been completed yet.
 */
export function RouteGuard() {
  const { stepCompleted, currentStep } = usePipelineState();
  const location = useLocation();

  // Find which step the current path corresponds to
  const currentStepDef = STEPS.find((s) => s.path === location.pathname);
  if (!currentStepDef) {
    // Unknown path — redirect to the current active step
    const activeStep = STEPS.find((s) => s.id === currentStep);
    return <Navigate to={activeStep?.path ?? '/start'} replace />;
  }

  // Check if all prior steps are completed
  const targetIdx = STEPS.findIndex((s) => s.id === currentStepDef.id);
  for (let i = 0; i < targetIdx; i++) {
    if (!stepCompleted[STEPS[i].id]) {
      // Not ready — redirect to the current active step
      const activeStep = STEPS.find((s) => s.id === currentStep);
      return <Navigate to={activeStep?.path ?? '/start'} replace />;
    }
  }

  return <Outlet />;
}
