// Netlify ingress enforcement. No environment configuration means CLOSED.
// The authorizer uses persistent state; this edge never caches an allow decision.
interface Context {
  ip: string;
  next: () => Promise<Response>;
}
const locked = () => new Response(JSON.stringify({ error: 'traffic_locked' }), {
  status: 403,
  headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
});
export default async function trafficLock(request: Request, context: Context): Promise<Response> {
  try {
    const environment = (globalThis as unknown as {
      Netlify: { env: { get: (name: string) => string | undefined } };
    }).Netlify.env;
    const endpoint = environment.get('TRAFFIC_LOCK_AUTHORIZER_URL');
    const key = environment.get('TRAFFIC_LOCK_AUTHORIZER_KEY');
    const project = environment.get('TRAFFIC_LOCK_PROJECT');
    if (!endpoint || !key || key.length < 32 || !project || !context.ip) return locked();
    const target = new URL(endpoint);
    if (target.protocol !== 'https:' || target.username || target.password ||
        target.pathname !== '/_traffic_lock/authorize' || target.search || target.hash) return locked();
    const response = await fetch(target, {
      method: 'POST',
      headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json' },
      body: JSON.stringify({ project, ip: context.ip, method: request.method, path: new URL(request.url).pathname }),
      signal: AbortSignal.timeout(2000),
      redirect: 'error',
    });
    if (response.status !== 200 || (await response.json()).allow !== true) return locked();
    const downstream = await context.next();
    const headers = new Headers(downstream.headers);
    headers.set('cache-control', 'private, no-store');
    return new Response(downstream.body, { status: downstream.status, statusText: downstream.statusText, headers });
  } catch {
    return locked();
  }
}
