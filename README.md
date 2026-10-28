# VincereHandball Grabador

App de escritorio para grabar clips de entrenamiento de extremos desde una cámara USB/capturadora y
archivarlos solos por equipo, jugador y ejercicio. Admite varias cámaras a la vez y una pantalla para
el segundo monitor o el proyector.

Python + OpenCV se encargan de las cámaras y del disco. La interfaz es web moderna (HTML/CSS/JS)
dentro de una ventana nativa con [pywebview](https://pywebview.flowrl.com/).

## Instalación

Despues de cada comando ejecutado reinicia la terminal

```bash
winget install Python.Python.3.12
pip install -r requirements.txt
```

- Windows 10/11 y macOS no necesitan nada más (usan WebView2 / WebKit).
- Linux: `pip install "pywebview[qt]"` (o `[gtk]`) además de lo anterior.

## Uso

Dirigete a la carpeta donde lo tienes guardado utilizando el comando cd.

```bash
cd "(Carpeta donde lo tienes guardado)"
python main.py --browser --lan
```

## Cómo se usa

1. **Equipo:** pulsa el botón del equipo (bajo las pestañas) para abrir, crear, duplicar, renombrar o eliminar equipos.
   Cada equipo guarda sus jugadores, agrupados por categorías, y sus ejercicios. Todo se guarda solo.
2. **Jugador y ejercicio:** toca un chip. El lápiz activa la edición: añadir y quitar, y para mover un jugador de
   categoría tócalo y pulsa **Mover aquí** en la categoría de destino.
3. **Grabar** (tarjeta amarilla) y **Parar** (tarjeta roja), o con la barra **Espacio**. Las teclas **1 a 9** eligen jugador.
4. **Guardar copia / Importar** (panel de equipos): exporta un equipo a un `.json` para llevarlo a otro ordenador.

### Usar DroidCam u otra cámara virtual como fuente

1. Inicia el cliente de la cámara virtual en Windows y conecta el teléfono hasta que se vea su imagen en el cliente.
2. En VinceGraba abre **Ajustes → Cámaras disponibles** y pulsa **Volver a buscar** si has conectado la cámara después de abrir los ajustes.
3. Busca la tarjeta **DroidCam Video** (o el nombre registrado por esa aplicación). Si Windows ofrece tanto **DirectShow** como **Media Foundation**, prueba primero DirectShow —la ruta estable de v2.6— y usa Media Foundation como alternativa explícita si ese endpoint no abre.
4. Elige la resolución y pulsa **Conectar**. YouTube sigue separado en «Otras fuentes de vídeo», por lo que una cámara no aparecerá como una entrada de YouTube.

VinceGraba enumera cámaras mediante **DirectShow y Media Foundation** en Windows. Cada opción guarda juntos su índice y su backend: no se intenta abrir el índice de Media Foundation usando DirectShow, ni al revés. Los nombres conocidos de cámaras virtuales o móviles se etiquetan como tales, pero un dispositivo no se descarta por tener un nombre desconocido.

En v2.8, las cámaras virtuales reconocidas que Windows expone por DirectShow se abren primero por **nombre exacto mediante FFmpeg**, en vez de confiar en que el índice de OpenCV coincida con el de la lista de dispositivos. Si esa apertura falla, VinceGraba no intenta silenciosamente otro índice que podría corresponder a una cámara física distinta; muestra un error para ayudar a diagnosticarlo. La ruta estable de OpenCV para cámaras físicas y la lógica de FPS/grabación normal de v2.6 permanecen sin cambios.

**Importante:** que Iriun o DroidCam aparezcan en la lista aunque su cliente esté cerrado puede ser normal: Windows puede mantener registrado el controlador de cámara virtual. Eso indica que el endpoint está instalado, no que esté entregando imagen. Para usar el móvil, abre también el cliente de Windows y conecta el teléfono. Si el cliente está cerrado, VinceGraba debe mostrar la fuente como detectada pero no puede obtener vídeo.

La app no puede detectar una aplicación móvil solo porque esté instalada: Windows debe exponerla como un dispositivo de vídeo o la fuente debe conectarse mediante una ruta de vídeo compatible. Si DroidCam no aparece, abre su cliente de Windows, conecta el teléfono y comprueba primero que la imagen aparece en ese cliente; después vuelve a buscar. Si tampoco aparece en la aplicación Cámara de Windows, revisa la instalación/estado de su cliente y controlador. Consulta la guía oficial: https://droidcam.app/windows/.

### Detección de cámaras (v2.8)

La búsqueda incluye cámaras USB, capturadoras y endpoints virtuales publicados por Windows. Si un dispositivo se expone por ambos backends, la tarjeta permite elegir qué endpoint abrir. Los endpoints se mantienen identificados por **backend + índice**; los números iguales de DirectShow y Media Foundation no se consideran la misma entrada. Las cámaras con el mismo nombre en ambos sistemas se presentan juntas como posibles métodos de apertura; VinceGraba conserva el endpoint elegido al conectarlo. Usa **Volver a buscar** después de iniciar o reconectar una aplicación de cámara virtual.

La mejora de detección no cambia los FPS solicitados, la negociación MJPG/YUY2, la cadencia de captura ni la lógica de grabación normal de v2.6.

### Cámara estable y 30 FPS (Windows 10/11)

En la versión 2.6, VinceGraba solicita primero **MJPG** y mide una muestra real de fotogramas; si ese modo no alcanza el FPS solicitado, prueba **YUY2** y después el modo predeterminado del controlador. No confía solo en el valor FPS comunicado por Windows: el indicador utiliza la frecuencia medida de los frames recibidos. La vista previa y el búfer retrospectivo comparten una única codificación JPEG para reducir carga de CPU.

Para comprobar la cámara sin pasar por la interfaz, ejecuta en PowerShell desde la carpeta del proyecto:

```powershell
python tools\diagnose_webcam.py --index 0 --seconds 10 --width 1280 --height 720 --fps 30
```

Cambia `--index` por el número real de la cámara en Ajustes. Si el test independiente alcanza 30 FPS pero la vista previa no, el problema está después de la captura; si también mide menos de 30, el límite está en el modo admitido por el dispositivo, el controlador o el enlace USB. Más detalles en `CAMERA_ESTABLE_WINDOWS_v2.6.md`.

## Varias sesiones y pantalla del proyector

Una **sesión** es una cámara con su propio equipo, jugador, ejercicio y grabación. Las pestañas de arriba a la
derecha las alternan, y **+** añade otra (hasta 4). Cada sesión puede trabajar con un equipo distinto.

La organización por **pabellones es opcional**. En Ajustes puedes crear, renombrar o eliminar pabellones y asignarles la pista seleccionada. Las pestañas se agrupan visualmente por pabellón; las pistas sin asignar siguen apareciendo en «Sin pabellón». Eliminar un pabellón no borra clips ni pistas: solo las deja sin pabellón. La configuración se guarda automáticamente y las instalaciones existentes migran sin cambios: inicialmente todas las pistas quedan sin pabellón.

La **pantalla** (icono del monitor, o `python main.py --display`) muestra en directo el vídeo, el jugador y el
cronómetro de todas las sesiones, o de una sola. Es de solo lectura. Se abre a pantalla completa en el segundo
monitor si lo hay; si no, es una ventana normal que puedes arrastrar al proyector.

Si abres la aplicación otra vez con la primera ya en marcha, la segunda **no toca ninguna cámara**: se une a la
primera y abre la pantalla del proyector (`--operator` abre otra ventana de control, `--session s2` limita la
pantalla a una sesión).

### Cómo se evitan los conflictos

| Riesgo | Cómo está resuelto |
|---|---|
| Dos procesos abriendo la misma cámara o escribiendo a la vez | Un único proceso anfitrión (cerrojo del sistema operativo, que se libera solo si el anfitrión se cae). Las demás ventanas se conectan a él. |
| Dos sesiones con la misma cámara | Una cámara física solo la usa una sesión. |
| Un jugador grabando en dos sitios | No se permite: "Ana ya está grabando en Pista A". |
| Dos clips con el mismo nombre en el mismo segundo | El nombre se reserva con creación exclusiva de fichero: el segundo recibe `_2`. |
| Dos ventanas pulsando Grabar a la vez | Un solo cerrojo ordena las órdenes: una gana y la otra recibe un aviso claro. |
| Editar listas mientras otra sesión graba | Se puede, salvo quitar lo que se está grabando. |
| Dos ventanas editando equipos | SQLite en modo WAL con transacciones y nombres únicos garantizados por la base de datos. |
| Cámara desconectada a mitad de clip | Un vigilante guarda el clip, libera la cámara y avisa en todas las ventanas. |
| Datos desfasados entre ventanas | El servidor empuja cada cambio a todas las ventanas al instante (SSE); lo que escribes en una no se pierde si otra cambia algo. |
| Varias ventanas mirando una cámara | La imagen se codifica una sola vez y se reparte. |

### Aislamiento entre ventanas y usuarios

- El servidor solo escucha en `127.0.0.1` y comprueba la cabecera `Host`.
- Cada página exige una clave de arranque que solo está en un fichero legible por el usuario del sistema.
  Otro usuario del mismo ordenador, o una web abierta en el navegador, no consigue ningún acceso.
- Cada ventana recibe su propio token con un rol: **control** (maneja la app) o **pantalla** (solo mira, y solo ve
  lo imprescindible). El rol lo fija el servidor.
- Política de seguridad de contenido (CSP) en las páginas.

## Dónde se guardan los datos

```
Directorio_Base/
└── Equipo/
    └── Jugador/
        └── Ejercicio/            (o "Sin_ejercicio")
            └── 2026-10-10_18-30-12_Jugador_Ejercicio.mp4
```

- Carpeta base por defecto: `~/Videos/Entrenamientos_Extremos` (se cambia en Ajustes).
- Nunca se sobrescribe un clip. Las copias de equipos van a `Directorio_Base/Equipos/`.
- Equipos y jugadores: SQLite en `~/.handball_recorder/data.db`. Ajustes y sesiones: `config.json` en la misma carpeta.
  Se puede mover con la variable de entorno `HANDBALL_RECORDER_HOME`.
- Las cámaras conectadas se descubren de nuevo en cada inicio; no se restaura ni se abre automáticamente una cámara guardada.
  En Windows se muestran los nombres de dispositivo de DirectShow cuando están disponibles.
- Para quitar la carpeta de equipo de la ruta, pon `"group_by_profile": false` en `config.json`.
- Para abrir la pantalla siempre al arrancar, pon `"open_display_on_start": true`.
- Si venías de la versión anterior, tus jugadores y ejercicios se migran a un equipo llamado "Mi equipo".

## Estructura del código

| Archivo | Responsabilidad |
|---|---|
| `main.py` | Arranque: anfitrión o ventana anexada, ventanas pywebview y segundo monitor |
| `recorder/service.py` | Sesiones, grabación y reglas de concurrencia, sin nada de interfaz |
| `recorder/db.py` | Equipos, categorías, jugadores y ejercicios en SQLite; copia en JSON |
| `recorder/camera.py` | Hilo de captura OpenCV, grabación y caché de vista previa |
| `recorder/storage.py` | Carpetas y nombres de clip con reserva atómica |
| `recorder/server.py` | Páginas, API, tiempo real (SSE) y vista previa MJPEG, con roles |
| `recorder/clients.py` | Registro de ventanas, roles y presencia |
| `recorder/instance.py` | Cerrojo de instancia única y datos del anfitrión |
| `recorder/config.py` | Ajustes persistentes y rutas |
| `web/` | Control (`index.html`, `app.js`, `style.css`) y pantalla (`display.*`) |
| `tests/` | 41 pruebas de base de datos, concurrencia y servidor |

```bash
python -m unittest discover -s tests -t . -v
```

## Notas

- Resolución: 480p, 720p o 1080p por sesión, en Ajustes.
- Los clips usan una cadencia estable (FPS configurados) y ajustan el número de fotogramas al tiempo transcurrido: se descartan capturas sobrantes y se duplican cuando la cámara entrega menos fotogramas. Así se evita que los vídeos se aceleren por lecturas en ráfagas del controlador.
- El búfer retrospectivo conserva JPEG a mayor calidad y resolución (hasta el límite de memoria), para preservar más detalle técnico.
- Si cierras la ventana grabando, los clips también se guardan.
- Renombrar un equipo no renombra las carpetas de clips que ya existen.
- Formato: MP4 (códec `mp4v`), compatible con cualquier reproductor.

## Correcciones de rendimiento v2.13

- La cámara web de tablet ya no está limitada por diseño a 10 FPS / 640×360: solicita 720p/30 FPS, transmite fotogramas guiados por `requestVideoFrameCallback` y omite los envíos cuando el anterior sigue en curso. La frecuencia real depende del hardware y de la red.
- El host reutiliza el JPEG que manda la tablet, sin recodificarlo otra vez para vista previa y búfer retrospectivo.
- En modo LAN, la interfaz del ordenador se sirve por HTTP de loopback y las tablets por un puerto HTTPS separado. Esto evita cifrar en el host el flujo de vista previa local, manteniendo HTTPS para dispositivos remotos.
- El certificado local persiste entre ejecuciones si sus IP/SAN siguen vigentes, evitando cambiar la identidad TLS en cada arranque.
- Los directos de YouTube prefieren formatos H.264 de hasta 720p/30 FPS y se limita la salida al máximo de 30 FPS. El pipe de vídeo bruto pasa a estar búferizado y la métrica de salida se actualiza con los fotogramas publicados por el decodificador.
- La captura física y la lógica normal de grabación de la v2.6 no se modifican.

La selección de formato y las pruebas de YouTube sintéticas no sustituyen una prueba contra el directo concreto. En una red real, la frecuencia también depende de la fuente de YouTube, el ancho de banda, el decodificador y el PC.

## Cámara de tablet, clips retrospectivos y carpeta de destino

### Cámara remota por HTTPS

Para permitir que Safari/Chrome acceda a la cámara del dispositivo, arranca el anfitrión con:

```bash
python main.py --browser --lan
```

Abre en la tablet una de las URL `Tablet LAN` impresas en la consola. En modo LAN, el panel del ordenador permanece en `http://127.0.0.1` (loopback, con bajo coste para la vista previa) y las tablets se conectan mediante un listener HTTPS separado. El certificado local conserva la misma identidad entre ejecuciones mientras no cambien las IP de la red, y cubre las IP detectadas y `extremos.local`. Como es un certificado autofirmado, el sistema operativo/navegador de la tablet debe confiar en él; si no, la cámara puede seguir bloqueada aunque la página cargue. Mantén el servicio dentro de una red privada y no lo publiques en Internet.

Pulsa **Usar cámara de esta tablet** y concede permiso. La vista previa se reproduce localmente y el navegador solicita 720p/30 FPS, enviando fotogramas JPEG con calidad alta al anfitrión. La frecuencia real depende de la cámara, navegador, red y carga del dispositivo; cuando el envío se retrasa, se omiten fotogramas pendientes en vez de crear una cola de vídeo atrasado. Selecciona jugador y ejercicio y usa la línea de tiempo para elegir 10, 15, 20, 25, 30 segundos o 1 minuto. La selección se recuerda en ese navegador. Pulsa **¡Guardar última jugada!** para exportar el intervalo seleccionado disponible. El botón **Cancelar** descarta una grabación normal en curso sin crear un clip ni copiarlo a otra ubicación.

### Búfer retrospectivo

Cada cámara mantiene un anillo de JPEG comprimidos de hasta 60 segundos y 192 MiB. Se descartan fotogramas por antigüedad o al alcanzar el límite de memoria; según resolución y movimiento, el límite de memoria puede hacer que haya menos de un minuto disponible. Al guardar, se copia la lista de JPEG bajo un cerrojo corto y se codifica MP4 fuera del hilo de captura, sin pausar la vista previa. Si la cámara lleva conectada menos tiempo que el intervalo seleccionado, solo se guarda el material disponible.

### Carpeta de destino de los vídeos

Los clips se guardan en una carpeta local elegida por el usuario. La ruta predeterminada es `~/Videos/Entrenamientos_Extremos`. Para cambiarla, abre **Ajustes → Carpeta de los clips → Cambiar carpeta**, selecciona una carpeta existente o crea una nueva y confirma. La aplicación valida que la ruta se pueda escribir y la utiliza para las nuevas grabaciones y jugadas retrospectivas.

Cambiar el destino no mueve ni elimina vídeos existentes. Tampoco cambia la captura, los FPS, la resolución ni el ciclo de grabación. La carpeta seleccionada puede ser una carpeta local o una unidad que esté disponible en el ordenador; VinceGraba no sincroniza ni sube archivos a servicios externos.

### Pruebas

Ejecuta desde la raíz del proyecto: `python -m unittest discover -s . -t . -v`. Las pruebas automatizadas no reemplazan una prueba de cámara real ni una prueba de confianza del certificado en iPadOS/Android.


### Distribución adaptable de cámara y controles

- Arrastra la barra entre la cámara y los controles para cambiar cuánto espacio ocupa cada zona. En pantallas estrechas, arrastra la barra horizontal para ajustar la altura de la vista previa.
- El botón **Cámara: normal** alterna entre vista normal, vista compacta y cámara oculta; al ocultarla, los controles ocupan toda la pantalla.
- La distribución elegida se guarda en el navegador de ese dispositivo. Para restablecerla, borra el almacenamiento local del sitio o alterna el botón hasta volver al modo deseado.
- También puedes enfocar la barra y usar las flechas del teclado para ajustar la separación.

## Conectar una tablet o móvil mediante QR

En la aplicación de escritorio, pulsa **Conectar dispositivo**. La ventana muestra un QR y los enlaces LAN para abrir directamente la botonera en un móvil o tablet. Ambos dispositivos deben estar en la misma red Wi-Fi.

Para habilitar acceso desde la red local, inicia la aplicación con la opción `--lan` (por ejemplo, `python main.py --lan`). Si usas el navegador en lugar de la ventana de escritorio, ejecuta `python main.py --browser --lan`. La primera vez puede ser necesario confiar en el certificado HTTPS local en cada dispositivo; si el navegador muestra una advertencia de certificado, el acceso a cámara del dispositivo puede no estar disponible hasta confiar en él. No compartas el QR con personas no autorizadas: contiene un enlace de acceso a la botonera.


## Logo de la aplicación (opcional)

Para añadir el icono propio, coloca tu archivo `.ico` en `web/favicon.ico` (usa exactamente ese nombre). El HTML ya referencia esa ruta; no hay que modificar código. Si el archivo aún no existe, la aplicación seguirá funcionando y el navegador mostrará su icono predeterminado.

## Añadir un directo de YouTube como cámara

1. Abre **Ajustes**.
2. Pulsa **Añadir cámara desde YouTube** y pega el enlace de reproducción del directo (también puede ser un enlace oculto al que tengas acceso).
3. Espera el resultado. Si falla, la aplicación mostrará el motivo que devuelva YouTube o el decodificador.

El número que se elige para YouTube identifica la vista/recorte del flujo, no un dispositivo físico. Por eso puede coincidir con el índice de una cámara USB (por ejemplo, Iriun en la 0 o la webcam integrada en la 1); las fuentes tienen espacios independientes.

La conexión utiliza `yt-dlp` para resolver el enlace y FFmpeg para decodificar el flujo. Los frames pasan por una cola acotada y un reloj de presentación independiente a 30 FPS; el hilo que lee FFmpeg nunca impone pausas al decodificador. Si la red deja de entregar datos, la vista espera y continúa a ritmo normal cuando vuelve el flujo: no intenta recuperar el tiempo perdido acelerando. La cola aplica contrapresión para limitar la acumulación de fotogramas. Como en cualquier reproductor de directo, una interrupción de red puede causar buffering y aumentar temporalmente el retraso; no es posible garantizar cero pérdidas si el emisor o la conexión no entregan todos los frames. La grabación retrospectiva está deshabilitada para YouTube.


## Directos YouTube de AutomaticTV con tres vistas

Al usar **Añadir cámara desde YouTube**, selecciona el número de vista:
- **0 — Pista izquierda:** recorte automático del cuadrante superior izquierdo.
- **1 — Pista derecha:** recorte automático del cuadrante superior derecho.
- **2 — Panorámica:** recorte automático de toda la mitad inferior (incluye los cuadrantes inferiores 3 y 4).
- **3–5:** vista completa del flujo, sin recorte.

Las vistas 0–2 aplican recortes de imagen; no modifican ni adelantan el flujo. La grabación retrospectiva y su ajuste de segundos no están disponibles para YouTube, porque el directo lleva una latencia que depende del emisor y de la red.

Nota: cada vista virtual abre su propia conexión al flujo de YouTube. Si se necesitan las tres vistas simultáneas, añade el mismo enlace tres veces con los números 0, 1 y 2. Esto puede consumir más ancho de banda y recursos que una única conexión compartida.


## Revisión técnica v2.1

Consulta `AUDITORIA_Y_PLAN.md` para el detalle de cambios y `RESULTADOS_PRUEBAS.md`
para las verificaciones ejecutadas. La suite se ejecuta desde esta carpeta con
`python -m pytest -q`. Las pruebas automáticas no sustituyen la validación con cámaras
físicas en Windows 10/11.

### Directos de YouTube con reloj de presentación (v2.5)

El reproductor usa el buffering normal de FFmpeg para la fuente HLS/DASH y una cola acotada de frames. Un hilo de lectura alimenta la cola y otro presenta las imágenes con una cadencia estable; si se vacía por una interrupción, el reloj se vuelve a anclar al reanudarse en vez de reproducir frames en ráfaga. La cola aplica contrapresión para no crecer indefinidamente. La vista puede quedarse congelada mientras espera datos, como un reproductor que está almacenando en búfer, y el retraso puede aumentar después de una interrupción. No se promete latencia cero ni ausencia absoluta de pérdidas de red.

Para reproducir a velocidad de reloj estable, se utiliza el FPS informado por el formato de YouTube (30 por defecto si no se informa, hasta 60 FPS). Los timestamps se normalizan usando el FPS de origen para que un salto de PTS entre segmentos no provoque una larga secuencia de frames duplicados. El motor de playout limita la presentación, no el hilo de lectura; la vista previa de YouTube puede transmitir hasta 60 FPS. Las pruebas incluyen simulación de ráfagas y cortes, además de una comprobación de integración con un vídeo sintético y FFmpeg real. La validación de un directo de YouTube real aún debe hacerse en el equipo Windows donde se usará.


## v2.9 — Menor latencia en cámaras virtuales

La ruta FFmpeg/DirectShow de cámaras virtuales usa un búfer más pequeño y entrega el fotograma más reciente disponible para evitar acumular retraso en la vista previa. No se cambian los FPS ni la lógica de grabación normal de la cámara física. Consulta `CAMBIO_BAJA_LATENCIA_DROIDCAM_v2.9.md` para las pruebas y limitaciones.


## DroidCam directo (v2.10)
En Ajustes, usa **DroidCam directo** si la app del móvil ofrece IP Cam. Introduce la IP y el puerto que muestra el teléfono. Esta ruta entrega MJPEG a Python sin el cliente virtual de Windows. La URL de prueba documentada es `http://IP:PUERTO/video/force/1280x720` (la resolución cambia con el ajuste elegido). Si la app no expone IP Cam, continúa usando la cámara virtual de Windows.


### Conectar móviles directamente y grabar con máxima calidad (v2.11)

**Mejor ruta integrada en VinceGraba para Android compatible:** DroidCam clásico con función IP Cam. En el teléfono abre la app y usa la IP y el puerto que muestre; el PC debe poder abrir `http://IP:PUERTO/video` y el flujo `http://IP:PUERTO/video/force/1280x720`. La documentación oficial describe esta URL y el uso del argumento `force`: https://www.dev47apps.com/droidcam/connect/.

1. Antes de la jornada, prueba cada teléfono durante al menos 10 minutos, con el móvil conectado a corriente y evitando que la app quede en segundo plano.
2. Para una conexión más estable, utiliza una red local dedicada de 5 GHz (idealmente PC por Ethernet) o USB tethering/hotspot si tu teléfono lo permite. Evita una Wi-Fi congestionada.
3. Empieza con 1280×720 a 30 FPS para comprobar estabilidad; prueba 1920×1080 solo si el flujo y el PC lo mantienen de forma continua.
4. La grabación de la fuente **DroidCam directo** usa H.264 QP 0 para reducir la pérdida de cuantización en la codificación final. El archivo puede ser varias veces mayor: deja espacio libre y haz una prueba de tamaño/duración antes de grabar toda la jornada.
5. Las cámaras USB/físicas siguen usando exactamente la ruta de grabación normal de v2.6. No se modifican sus FPS ni su lógica.

**Límite importante de calidad:** no existe una garantía de “cero pérdida” desde el sensor del móvil hasta el archivo: la app del teléfono ya transmite una señal comprimida, y la conversión de color puede introducir diferencias. QP 0 evita la pérdida de cuantización H.264, pero no recupera detalles descartados antes de que el vídeo llegue a Python. Esta configuración prioriza calidad sobre tamaño del archivo.

**iPhone, Iriun u otras apps sin IP Cam:** no se pueden conectar con esta ruta HTTP específica. Para la jornada, usa su cliente/driver oficial y verifica el flujo en VinceGraba antes de grabar. La integración directa universal de todos los fabricantes requeriría sus protocolos/SDKs; no sería responsable prometerla sin probar cada teléfono.

#### Qué se ha probado en v2.11

- Compilación de Python y pruebas de captura directa MJPEG simulada.
- Prueba del codificador H.264 QP 0: escribe un MP4 y OpenCV vuelve a leerlo correctamente.
- Pruebas de estabilidad de cámara, YouTube, servidor y sincronización: 37 pruebas superadas en grupos ejecutados.
- No se dispone de Windows ni de un teléfono físico en este entorno; es imprescindible realizar una prueba real de conexión y grabación antes de la jornada.


## Interfaz de fuentes en v2.14

La interfaz principal solo ofrece webcams/capturadoras físicas y DroidCam directo por IP. Las cámaras virtuales y la fuente YouTube se ocultan porque no han ofrecido rendimiento suficientemente estable en las pruebas reales. El flujo LAN de conexión de tablets como dispositivos de control se mantiene.
