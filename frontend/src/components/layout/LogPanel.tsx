import { useEffect, useRef, useState } from 'react';
import { usePipelineState, usePipelineActions } from '../../context/PipelineContext';
import type { LogEntry } from '../../types';

const levelStyles: Record<LogEntry['level'], string> = {
  info: 'text-body',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-error',
};

const levelBadge: Record<LogEntry['level'], string> = {
  info: 'INFO',
  success: ' OK ',
  warning: 'WARN',
  error: 'ERR ',
};

const levelBadgeStyle: Record<LogEntry['level'], string> = {
  info: 'bg-gray-100 text-muted',
  success: 'bg-green-100 text-success',
  warning: 'bg-amber-100 text-amber-700',
  error: 'bg-red-100 text-error',
};

export function LogPanel() {
  const { logs } = usePipelineState();
  const { clearLogs } = usePipelineActions();
  const containerRef = useRef<HTMLDivElement>(null);
  const [collapsed, setCollapsed] = useState(true);

  // Auto-scroll to bottom when new logs arrive (only when expanded)
  useEffect(() => {
    if (!collapsed && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs, collapsed]);

  const unreadCount = logs.length;

  return (
    <section className="border-t border-border bg-panel-secondary shrink-0">
      {/* Header */}
      <div className="h-9 px-3 border-b border-border bg-slate-50 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <button
            className="w-5 h-5 flex items-center justify-center rounded-sm border border-border bg-white text-muted cursor-pointer
              hover:bg-gray-100 text-xs leading-none p-0 shrink-0"
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? '展开日志' : '收起日志'}
          >
            {collapsed ? '▸' : '▾'}
          </button>
          <span className="text-[13px] font-medium text-title">输出日志</span>
        </div>
        <div className="flex items-center gap-3">
          {collapsed && unreadCount > 0 && (
            <span className="text-[11px] text-muted">{unreadCount} 条</span>
          )}
          {!collapsed && (
            <>
              <span className="text-[11px] text-muted">{unreadCount} 条</span>
              {unreadCount > 0 && (
                <button
                  className="h-6 px-2 text-[11px] rounded-sm border border-border bg-white text-muted cursor-pointer
                    hover:bg-gray-100"
                  onClick={clearLogs}
                >
                  清空
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* Log lines — hidden when collapsed */}
      {!collapsed && (
        <div
          ref={containerRef}
          className="h-48 overflow-auto font-mono text-[11px] leading-5"
        >
          {logs.length === 0 ? (
            <div className="flex items-center justify-center h-full text-muted text-[11px]">
              暂无日志输出
            </div>
          ) : (
            logs.map((entry) => (
              <div
                key={entry.id}
                className={`flex items-start gap-2 px-3 py-0.5 border-b border-border-light last:border-b-0
                  hover:bg-gray-50/50 ${levelStyles[entry.level]}`}
              >
                <span className="text-muted shrink-0 select-none">{entry.time}</span>
                <span
                  className={`shrink-0 px-1 rounded-sm text-[10px] font-semibold leading-5 ${levelBadgeStyle[entry.level]}`}
                >
                  {levelBadge[entry.level]}
                </span>
                <span className="truncate">{entry.message}</span>
              </div>
            ))
          )}
        </div>
      )}
    </section>
  );
}
