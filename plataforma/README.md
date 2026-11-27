# Plataforma de participantes · Hackathon AWS Student Builder Group at UC3M x SEA

Portal donde cada participante ve la agenda y los enlaces (Meetup, Builder Center, consola), marca los pasos previos, forma equipo y entrega repo + demo. La organización tiene un panel con todo.

## Arquitectura (serverless, cuenta de SEA, eu-south-2)

```
Navegador ──► CloudFront ──┬─► S3 (landing + /app/)
                           └─► /api/* ─► API Gateway HTTP ─► Lambda (Python) ─► DynamoDB
Navegador ──► Cognito (email + contraseña; códigos de confirmación y recuperación enviados con SES)
Cuenta AWS del participante: stack "hackathon-checkin" ─► POST /api/checkin (MFA root, presupuesto)
```

| Pieza | Para qué | Coste estimado |
|---|---|---|
| S3 + CloudFront | Web estática (landing y portal) | ~0 € (free tier) |
| Cognito Essentials | Login con email + contraseña, grupo `admins` | 0 € hasta 10.000 usuarios activos/mes |
| SES | Código para confirmar el email y para recuperar la contraseña | ~0,10 € / 1.000 emails |
| API Gateway HTTP + Lambda | API | ~0 € |
| DynamoDB on-demand | Datos | ~0 € |
| Route 53 (opcional) | Dominio propio | 0,50 €/mes + dominio |

## Estructura

- `template.yaml`: toda la infraestructura (SAM).
- `backend/app.py`: la API (una Lambda con router).
- `frontend/app/index.html`: el portal (HTML + JS sin dependencias). La landing de `../web/index.html` se sube a la raíz.
- `checker/checkin.yaml`: plantilla que cada participante lanza en **su** cuenta para demostrar que hizo los pasos de consola.
- `deploy.sh`: despliegue en un comando.
- `ses.sh`: pone SES como remitente de los correos (verifica el remitente, redespliega y pide salir del sandbox).

## Antes de desplegar (una vez)

1. Activar la región eu-south-2 en la cuenta (es opt-in) si no lo está.
2. **SES**: verificar el dominio (o, mientras tanto, un email) y **pedir salida del sandbox** (Account dashboard > Request production access). Sin eso solo llegan emails a direcciones verificadas. Tarda ~24 h: pedirlo ya.
3. Instalar AWS CLI y SAM CLI.

## Desplegar

```bash
SES_IDENTITY_ARN=arn:aws:ses:eu-south-2:<cuenta>:identity/<dominio-o-email> \
SES_FROM=<remitente> ./deploy.sh
```

Con dominio propio añade `DOMAIN=... CERT_ARN=<ACM en us-east-1> ZONE_ID=<Route 53>`.

Después: regístrate en `/app/` y hazte admin con el comando que imprime el script. En **Panel de organización > Configuración** se rellenan enlaces, agenda, hora de cierre y aviso, sin redesplegar.

## Confirmar email y recuperar contraseña (SES)

- **Registro**: la cuenta se crea con el email sin confirmar. Al entrar, la web pide el código que llega por correo. Hasta confirmarlo, la API responde 403 a todo lo que no es público. Si alguien registra un email que no es suyo y no lo confirma, el dueño real puede volver a crear la cuenta con ese email.
- **¿Olvidaste tu contraseña?**: en Iniciar sesión. Cognito manda un código al email (solo a emails confirmados) y la web deja poner una contraseña nueva.
- Las cuentas creadas antes de este cambio ya tienen el email confirmado: no se les pide nada.
- Los dos correos usan la plantilla `VerificationMessageTemplate` de `template.yaml` (blanco, Noto Sans, detalle rojo).

Mientras no haya SES, Cognito manda los correos con su remitente por defecto (máx. 50 al día). Para usar SES:

```bash
bash ses.sh remitente@dominio.es   # AWS manda un enlace a ese email: púlsalo
bash ses.sh remitente@dominio.es   # otra vez: comprueba, crea el rol de Cognito y redespliega con SES
bash ses.sh --salir-sandbox        # pide acceso de producción (sin esto solo llega a emails verificados)
bash ses.sh --estado
```

`deploy.sh` y `actualizar.sh` conservan el remitente SES y los admins del despliegue anterior.

## Meetup → plataforma

La gente se apunta con su **nombre completo** en Meetup y en la plataforma (decisión de Ivan, 23 nov). Exporta la lista de asistentes de Meetup (CSV) y pégala en **Importar Meetup**: se cruza por nombre (sin tildes, mayúsculas ni espacios de más) y, si el CSV trae emails, también por email. Quien se registre después de la importación se marca solo.

## Comprobación de la cuenta de AWS

El botón **Comprobar mi cuenta** abre CloudFormation en la cuenta del participante con la plantilla `checker/checkin.yaml` y su token ya relleno. Crea una Lambda de solo lectura que mira:

- `mfaRoot`: MFA activado en root (`iam:GetAccountSummary`).
- `budget`: hay al menos un presupuesto en AWS Budgets.

Y envía solo esos booleanos y el ID de cuenta. No damos acceso a su cuenta a nadie. Además demuestra que la cuenta funciona y saben desplegar. Para añadir más pasos, añade la comprobación en la plantilla y su clave en `requiredChecks` (configuración).

Límite honesto: un participante con mala fe podría llamar a `/api/checkin` a mano con su token. Para un hackathon es suficiente.

## Pendiente de confirmar

- Enlaces: Meetup, Builder Center, registro AWS, guía de IA.
- Agenda por horas, hora de cierre de entregas.
- Qué pasos de consola son obligatorios (ahora: MFA root + presupuesto).
- Probar que el quick-create de CloudFormation en eu-west-1 carga la plantilla del bucket de eu-south-2.
- Probar el login con código por email en la región (Cognito Essentials + SES).
