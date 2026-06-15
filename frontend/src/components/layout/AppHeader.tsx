import { usePipelineState } from '../../context/PipelineContext';

export function AppHeader() {
  const { protocolName } = usePipelineState();

  return (
    <header className="h-20 bg-primary text-white flex items-center justify-between px-4 shrink-0">
      <div className="flex items-center gap-3">
        <svg className="w-7 h-7 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M12 2L2 7l10 5 10-5-10-5z" />
          <path d="M2 17l10 5 10-5" />
          <path d="M2 12l10 5 10-5" />
        </svg>
        <div>
          <h1 className="text-[16px] font-semibold leading-tight m-0">协议模糊测试平台</h1>
          <p className="text-[11px] text-white/70 leading-tight m-0">Protocol Fuzzer Pipeline Demo</p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        {protocolName.trim() && (
          <span className="text-[11px] px-2.5 py-0.5 bg-white/15 border border-white/25 rounded-sm uppercase tracking-wide">
            {protocolName.trim()}
          </span>
        )}
        <span className="text-[11px] px-2.5 py-0.5 bg-white/10 border border-white/20 rounded-sm">
          v0.1 Demo
        </span>
      </div>
    </header>
  );
}
