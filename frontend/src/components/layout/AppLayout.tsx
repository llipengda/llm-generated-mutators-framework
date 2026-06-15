import { Outlet } from 'react-router-dom';
import { AppHeader } from './AppHeader';
import { StepIndicator } from './StepIndicator';
import { LogPanel } from './LogPanel';

export function AppLayout() {
  return (
    <div className="flex flex-col h-screen bg-page">
      <AppHeader />
      <StepIndicator />
      <div className="flex-1 flex flex-col overflow-hidden">
        <main className="flex-1 overflow-auto">
          <div className="max-w-[960px] mx-auto px-8 py-6">
            <Outlet />
          </div>
        </main>
        <LogPanel />
      </div>
    </div>
  );
}
