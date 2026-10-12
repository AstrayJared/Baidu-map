// Netlify builds must target a deployed backend, not a visitor's own computer.
const fail = message => {
  console.error(`Netlify deployment configuration: ${message}`);
  process.exit(1);
};

let backend;
try {
  backend = new URL(process.env.VITE_API_BASE_URL?.trim() || '');
} catch {
  fail('Set VITE_API_BASE_URL to the public HTTPS backend origin.');
}
if (backend.protocol !== 'https:' || backend.username || backend.password
    || backend.search || backend.hash || backend.pathname !== '/') {
  fail('VITE_API_BASE_URL must be an HTTPS origin without credentials, path, query or fragment.');
}
const host = backend.hostname.toLowerCase();
if (host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local')
    || host === '[::1]' || host === '[::]' || /^(127\.|0\.|10\.|192\.168\.|169\.254\.)/.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host)) {
  fail('VITE_API_BASE_URL must be publicly reachable; local addresses cannot serve visitors.');
}
if (!process.env.VITE_BAIDU_MAP_AK?.trim()) {
  fail('Set VITE_BAIDU_MAP_AK to the browser JavaScript API GL key. Never use the server key.');
}
console.log('Netlify deployment variables are present; verify backend reachability before publishing.');
