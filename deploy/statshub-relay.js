// Cloudflare Worker (plan gratis: 100.000 peticiones/día) que reenvía a Statshub
// solo las rutas /api/*. Úsalo SOLO si Statshub bloquea la IP de tu VM.
// Despliegue: dash.cloudflare.com → Workers → Create → pega esto → Deploy.
// Luego en .env: STATSHUB_RELAY=https://<tu-worker>.workers.dev  y  RELAY_KEY igual que abajo.
const KEY = "cambia-esta-clave";
export default {
  async fetch(req) {
    const url = new URL(req.url);
    if (!url.pathname.startsWith("/api/")) return new Response("no", { status: 404 });
    if (url.searchParams.get("k") !== KEY && req.headers.get("x-relay-key") !== KEY) return new Response("forbidden", { status: 403 });
    url.searchParams.delete("k");
    const target = "https://www.statshub.com" + url.pathname + url.search;
    const r = await fetch(target, { headers: {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Referer": "https://www.statshub.com/", "Accept-Language": "es-ES,es;q=0.9" },
      cf: { cacheTtl: 30 } });
    return new Response(r.body, { status: r.status, headers: { "content-type": r.headers.get("content-type") || "application/json" } });
  },
};
