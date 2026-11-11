export function deviceWebSocketUrl(path) { const url = new URL(path, location.href); url.protocol = location.protocol === "https:" ? "wss:" : "ws:"; return url.href; }
