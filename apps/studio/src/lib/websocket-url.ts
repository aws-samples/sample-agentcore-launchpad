export function buildWebSocketUrl(baseUrl: string, pageOrigin: string): string {
  const url = new URL(baseUrl || pageOrigin, pageOrigin);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`Unsupported WebSocket base protocol: ${url.protocol}`);
  }

  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `${url.pathname.replace(/\/$/, '')}/ws`;
  url.search = '';
  url.hash = '';
  return url.toString();
}
