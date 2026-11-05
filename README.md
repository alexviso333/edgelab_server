# edgelab

Laboratorio de estrategias de apuestas de fútbol que **descubre, inventa, valida y vigila** estrategias pre-partido y en directo, te avisa de cuándo entrar (con la cuota mínima y el stake) y de cuándo y cuánto cubrirte, y aprende solo de cada partido.

Competiciones: Premier, LaLiga, Serie A, Bundesliga, Ligue 1, Champions, Europa League, Conference y Copa del Rey.
Coste: **0 €** (VM gratuita de Google Cloud, fuentes gratuitas, Telegram gratis).

---

## Qué hace, en una frase por pieza

| Pieza | Qué hace |
|---|---|
| **Colector Statshub** (principal) | Calendario, resultados, árbitro, alineaciones previstas/confirmadas, stats por partido (xG, tiros, córners, tarjetas) y **~130 mercados de 15 casas** (bet365, Pinnacle, Betfair…). Va con freno (≥2,5 s entre peticiones) porque Cloudflare corta con 429 si vas rápido. |
| football-data.co.uk | Histórico de cuotas de **apertura y cierre** (bet365, Pinnacle, máx., media) de 1X2, O/U 2.5 y AH de las 5 grandes desde hace 20 años. Base del backtest pre-partido con precios reales y del CLV. |
| Sofascore (www.sofascore.com/api/v1) | Mismos IDs que Statshub. Cronología de cada partido (goles con minuto, rojas, cambios, añadido y **xG de cada tiro**), alineaciones confirmadas, estadísticas en vivo y **cuotas pre-partido y EN DIRECTO** (1X2, AH, goles, ambos marcan, próximo gol, tarjetas, córners). Desde España no muestra bet365: se usa como precio de referencia en vivo y de respaldo. |
| Understat | Minuto y xG de cada tiro desde 2014 (5 grandes). Si no responde desde tu red, Sofascore lo sustituye. |
| Open-Meteo | Clima en el estadio (gratis). |
| **Modelos** | Elo, Dixon-Coles (goles y xG), mercado sin margen, gradient boosting y **red neuronal (MLP)**, apilados con validación walk-forward. |
| **Alineaciones** | Once previsto/confirmado de Statshub + historial de cada jugador (500 partidos por equipo): fuerza del once frente al habitual, bajas de titulares, rotaciones. |
| **Duelos** | Extremo contra lateral, delantero contra centrales, mediapunta contra pivotes: regates, xG, entradas, duelos, faltas y riesgo de tarjeta del defensor. |
| **Cara a cara** | Todos los enfrentamientos previos: puntos, goles, empates, tarjetas. |
| **Cruce de factores** | Todas las parejas de factores × cada mercado en un mapa de 3×3; las casillas fuertes se vuelven estrategias y se validan. |
| **Informe del partido** | Junta todos los factores con dirección y fuerza y redacta la lectura (reglas o IA opcional). |
| **Modelo en directo** | Cadena de Markov minuto a minuto: cuota justa de cualquier mercado en cualquier marcador/minuto, con rojas, xG en vivo y tiempo añadido. Sus parámetros se re-aprenden cada semana con los partidos jugados. |
| **Coberturas** | Fórmulas clásicas + un solver general que cubre cualquier combinación de mercados liquidando cada marcador igual que bet365 (cuartos, AH en directo desde el marcador actual…). |
| **Motor de estrategias** | Plantilla de 7 piezas → catálogo (el tuyo + las mías) → rejilla → algoritmo genético → árboles de decisión que encuentran reglas nuevas. |
| **Validación** | Entrena en unas temporadas, prueba en otras que no ha visto, contrae el yield hacia −margen según la muestra, corrige por miles de pruebas (Benjamini-Hochberg), exige CLV positivo y que no dependa de 3 cuotas locas. |
| **Seguimiento en papel** | Lo validado pasa a papel con partidos reales; se liquida solo; si se degrada se retira, si confirma te propone pasarla a dinero real. |
| **Web + Telegram** | Panel, picks, directo con señales, calculadora, estrategias, laboratorio, apuestas, aprendizaje. Alertas al móvil. |

## Sobre el objetivo del +30 % de yield (leer antes de apostar)

El sistema está construido para **intentar** encontrar ventajas grandes, pero tiene que decirte la verdad sobre lo que encuentre:

- En mercados principales y líquidos (1X2/AH de la Premier) un yield sostenido del +30 % no existe: el mejor modelo publicado en directo contra Betfair saca ~+4,5 %. Los yields altos aparecen en muestras pequeñas y desaparecen al ampliarlas; para eso está la contracción y la validación fuera de muestra.
- Donde sí hay más margen de error del precio: mercados secundarios (tarjetas, córners, tiros), apertura de bet365 frente a Pinnacle, y situaciones concretas del directo. El problema ahí es que **bet365 limita rápido** a quien gana en esos mercados.
- Indicadores que mirar (por orden): **CLV** (si no batimos el cierre de Pinnacle, el yield positivo es suerte), yield **contraído**, nº de apuestas, temporadas positivas.
- Nada pasa a dinero real sin ≥150 apuestas en papel con partidos reales. Nunca martingalas ni subir stake tras perder (están en el catálogo solo como control negativo).

## Cómo abrir la página

**Opción A: en tu PC, ahora mismo (2 minutos, con datos de demostración)**
1. Instala Python 3.11 o superior desde python.org (en Windows marca *Add python.exe to PATH*).
2. Descomprime el zip y haz doble clic en `abrir_en_windows.bat` (en Mac/Linux: `./abrir.sh`).
3. La primera vez prepara todo y crea un mundo simulado; luego se abre sola `http://localhost:8000`.
   Mientras esa ventana negra esté abierta funcionan **la web y el planificador** (calendario, cuotas,
   alineaciones, cronologías y directo se actualizan solos; en *Hoy → Estado de los datos* ves cómo va y
   tienes el botón *Sincronizar ahora*). Para datos reales: `abrir_en_windows.bat real`
   (descarga el histórico; tarda horas la primera vez).

**Opción B: 24/7 sin tener el PC encendido** → Google Cloud gratis (abajo). Luego la abres desde el móvil o
el PC en `http://IP_DE_TU_VM:8000` con la contraseña que te da el instalador.

**IA para el informe del partido (opcional):** por defecto el texto lo redacta un sistema de reglas gratuito.
Si quieres que lo escriba una IA, pon en `.env` `LLM_PROVIDER=gemini` y `GEMINI_API_KEY=...` (Google AI Studio
tiene nivel gratuito con límites) o `LLM_PROVIDER=anthropic` y `ANTHROPIC_API_KEY=...` (de pago, céntimos al mes).
La IA solo recibe los factores calculados y tiene prohibido inventar datos.

## Instalación en Google Cloud (gratis)

1. En la consola de GCP (con la cuenta secundaria): **Compute Engine → Crear instancia**
   - Región `us-east1`, tipo `e2-micro`, disco **estándar** 30 GB, Debian 12. (Es la combinación del nivel gratuito.)
   - Marca "Permitir tráfico HTTP".
2. **Red VPC → Cortafuegos → Crear regla**: entrada, TCP `8000`, origen `0.0.0.0/0` (la web va con contraseña).
3. Entra por SSH desde la consola y ejecuta:
   ```bash
   git clone https://github.com/alexviso333/edgelab.git && cd edgelab && sudo bash deploy/install.sh
   ```
   Al final imprime la **contraseña de la web** y comprueba qué fuentes responden desde esa IP.
4. Carga el histórico (unas horas la primera vez; puedes cerrar la consola):
   ```bash
   sudo -u edgelab bash -c "cd /opt/edgelab && nohup .venv/bin/python -m edgelab.cli backfill --seasons 6 > data/backfill.log 2>&1 &"
   ```
5. Cuando termine, el primer laboratorio (luego se ejecuta solo cada lunes):
   ```bash
   sudo -u edgelab bash -c "cd /opt/edgelab && .venv/bin/python -m edgelab.cli lab"
   ```
6. Abre `http://IP_DE_LA_VM:8000`.

**Telegram:** crea un bot con @BotFather, pon `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID` en `/opt/edgelab/.env` y `sudo systemctl restart edgelab-worker`.

**Si Statshub bloquea la IP de la VM** (`cli check` dirá FALLO): sube `STATSHUB_MIN_INTERVAL` a 4; si sigue, despliega `deploy/statshub-relay.js` en Cloudflare Workers (gratis) y pon `STATSHUB_RELAY` y `RELAY_KEY` en `.env`.

## Probarlo sin red

```bash
pip install -r requirements.txt
python -m pytest -q                 # 30+ tests: liquidación, coberturas, parsers con respuestas reales de Statshub, modelo en vivo, pipeline
python -m edgelab.cli demo          # mundo sintético con 2 ineficiencias plantadas + laboratorio completo
python -m edgelab.cli api           # web en http://localhost:8000
```
El modo demo comprueba que el laboratorio **encuentra** las ventajas plantadas fuera de muestra y que el control negativo no pasa.

## Cómo usarlo cada semana

- **Lunes** (automático): recalibra el modelo en vivo, reentrena, relanza la búsqueda, revisa las estrategias en papel y te manda el informe por Telegram.
- **Durante la semana**: mira *Partidos y picks* (pre-partido) y *Directo*. Cada señal dice la **cuota mínima**: si bet365 paga menos, no hay valor y no se entra.
- Si haces una apuesta de verdad, pulsa **"La hice"** (con la cuota real) o importa un CSV en *Apuestas*. Así el sistema mide tu rendimiento real aparte del de papel. No hace falta que me pases los resultados: se liquidan solos con Statshub.

## Estructura

```
edgelab/
  collectors/   statshub, footballdata, understat, sofascore, weather, teams (unificación de nombres), http (cliente educado)
  core/         db (SQLite), markets (liquidación de todos los mercados), hedge (coberturas)
  models/       elo, dixon_coles, poisson, features, ensemble (ML + MLP + apilado), inplay (directo), secondary (córners/tarjetas)
  strategies/   dsl (plantilla de 7 piezas), catalog (≈80 semillas), search (rejilla, genético, árboles)
  backtest/     prematch (precios reales), inplay (cronologías + precios grabados o simulados)
  validation/   stats (bootstrap, contracción, BH-FDR, veredictos)
  live/         engine (calendario, grabador de cuotas, picks, directo, coberturas)
  learning/     loop (liquidación, CLV, calibración, promoción/retirada, informe, importación)
  api/          FastAPI      web/  frontend      deploy/  VM + systemd + relay      scripts/  mundo sintético
```

## APIs de pago (no se usan; solo si algún día quieres)

| API | Para qué | Precio aprox. |
|---|---|---|
| API-Football | Copas (Copa del Rey) con más detalle y alineaciones | gratis 100 pet./día; Pro ~19 $/mes |
| Betfair Exchange API | Cuotas de exchange en vivo (delayed gratis con cuenta; live: tarifa única ~299 £) | 0 € delayed |
| The Odds API | Cuotas de muchas casas por API | gratis 500 pet./mes; 30 $/mes 20k |
| OddsPapi | Pinnacle en vivo/copas | gratis 250 pet./mes |

Con Statshub (bet365 + Pinnacle + Betfair) el sistema no necesita ninguna de pago para empezar.
