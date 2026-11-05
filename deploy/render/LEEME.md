# edgelab en Render (cuenta nueva) con el código privado

El código está en tu repositorio privado `alexviso333/edgelab`. Cada vez que se sube un cambio, GitHub construye la
imagen (pestaña **Actions**, 5–8 min) y la guarda privada en `ghcr.io/alexviso333/edgelab:latest`.

1. **Token de GitHub de solo lectura** (para que Render pueda descargar la imagen privada):
   github.com/settings/tokens → *Generate new token (classic)* → marca solo **read:packages** → sin caducidad → copia el token.
2. **Render** (la cuenta nueva): New → Web Service → *Existing image* →
   imagen `ghcr.io/alexviso333/edgelab:latest` → *Add credential*: usuario `alexviso333`, contraseña = el token del paso 1 → plan **Free**.
3. **Variables de entorno** en Render:
   - `HF_TOKEN` = token NUEVO de Hugging Face (de escritura; el anterior está expuesto, bórralo)
   - `DATA_REPO` = `alexviso333/edgelab-data`
   - `EDGELAB_PASSWORD` = la contraseña para entrar a la web
4. (Opcional) Render → Settings → *Deploy Hook* → copia la URL y guárdala en GitHub (repo → Settings → Secrets → Actions)
   como `RENDER_DEPLOY_HOOK`: así cada cambio se publica solo.
5. Seguro extra contra el sueño: en cron-job.org (gratis) una tarea que abra `https://TU-SERVICIO.onrender.com/api/health`
   cada 10 minutos (la web ya se visita sola, esto es por si acaso).
6. Cuando funcione, cierra la versión del PC.
