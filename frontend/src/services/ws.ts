import type { WsMessage } from '../types';

/**
 * Connect to the session WebSocket for real-time log streaming.
 * Returns a cleanup function that closes the connection.
 */
export function connectLogSocket(
  sessionId: string,
  onLog: (line: string) => void,
): () => void {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  const url = `${protocol}//${host}/api/v1/sessions/${sessionId}/ws`;

  let ws: WebSocket | null = new WebSocket(url);
  let stopped = false;

  function connect() {
    if (stopped) return;
    ws = new WebSocket(url);
    ws.onmessage = (event) => {
      try {
        const msg: WsMessage = JSON.parse(event.data);
        if (msg.type === 'log' && msg.line) {
          onLog(msg.line);
        }
      } catch {
        // ignore malformed messages
      }
    };
    ws.onclose = () => {
      if (!stopped) {
        // Reconnect after 2 seconds.
        setTimeout(connect, 2000);
      }
    };
    ws.onerror = () => {
      ws?.close();
    };
  }

  // If the initial WS is already closed/errored, onclose/onerror handle reconnect.
  // But we need to set handlers on the initial ws too.
  ws.onmessage = (event) => {
    try {
      const msg: WsMessage = JSON.parse(event.data);
      if (msg.type === 'log' && msg.line) {
        onLog(msg.line);
      }
    } catch { /* ignore */ }
  };
  ws.onclose = () => {
    if (!stopped) setTimeout(connect, 2000);
  };
  ws.onerror = () => {
    ws?.close();
  };

  return () => {
    stopped = true;
    ws?.close();
    ws = null;
  };
}
