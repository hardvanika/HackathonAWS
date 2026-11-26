"""API de la plataforma del hackathon (una sola Lambda con router).

Modelo de datos (tabla unica):
  USER#<email>   / PROFILE  perfil, equipo, checks de AWS, flags
  ALLOW#<email>  / MEETUP   email importado del export de Meetup
  TOKEN#<token>  / USER     token de comprobacion de cuenta AWS -> email
  TEAM#<id>      / META     equipo, miembros (set), entrega
  CODE#<code>    / TEAM     codigo de union -> id de equipo
  CONFIG         / EVENT    agenda, enlaces, fechas (editable por admins)
"""
import json
import os
import re
import secrets
import string
import csv
import io
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

TABLE = os.environ["TABLE"]
POOL = os.environ["USER_POOL_ID"]
CLIENT_ID = os.environ["USER_POOL_CLIENT_ID"]
CHECKER_URL = os.environ.get("CHECKER_TEMPLATE_URL", "")
REGION = os.environ.get("AWS_REGION", "eu-south-2")
AUTH_MODE = os.environ.get("AUTH_MODE", "otp")  # "password" si no hay SES
# Admins fijos (parametro AdminEmails del stack). El resto se gestionan desde el panel (items ADMIN#email).
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE)
ddb_client = boto3.client("dynamodb")
cognito = boto3.client("cognito-idp")
ser = TypeSerializer()

MAX_TEAM = 2  # parejas como maximo
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"^https://[^\s<>\"']{4,400}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")

# Lo que no esta confirmado se deja vacio: los admins lo rellenan desde el panel.
DEFAULT_CONFIG = {
    "eventName": "Hackathon AWS Student Builder Group at UC3M x SEA",
    "eventStart": "2026-12-04T18:00:00+01:00",
    "location": "UC3M, campus de Puerta de Toledo, aula 0.A.07",
    "submissionDeadline": "2026-12-04T20:00:00+01:00",  # viernes 4 dic, 20:00: 2 h después del inicio (Ivan, 26 nov)
    # Timeline del dia. Hora vacia = "por confirmar". Se edita desde el panel (Ajustes).
    "agenda": [
        {"time": "18:00", "title": "Llegada y registro", "detail": "Recogemos tu nombre y te damos acceso."},
        {"time": "", "title": "Bienvenida y presentación del reto", "detail": "Formamos parejas y repasamos los módulos."},
        {"time": "", "title": "A construir", "detail": "Con Builder Center, tus 100 $ de créditos y tu asistente de IA."},
        {"time": "", "title": "Cierre de entregas", "detail": "Repo y demo, desde la plataforma."},
        {"time": "", "title": "Demos y cierre", "detail": "Cada equipo enseña lo que ha montado."},
    ],
    # Invitados y ponentes: [{"name": "...", "role": "...", "org": "...", "url": "https://..."}]
    "speakers": [],
    "timerStart": "",                  # inicio de la cuenta atras del hackathon (boton del panel)
    "timerMinutes": 120,
    "goal": 70,                        # objetivo de asistentes (panel > Estadisticas)
    "participantRegion": "eu-west-1",  # region donde lanzan la comprobacion en SU cuenta
    "requiredChecks": ["mfaRoot", "budget"],
    "links": {
        "website": "https://db1470yzxtmai.cloudfront.net/",
        "meetup": "https://www.meetup.com/aws-sbg-at-charles-iii-univ-of-madrid-colmenarejo-campus/events/317598343/",
        "whatsapp": "https://chat.whatsapp.com/IuoL9mjiGwz3IfgPREzg9T",
        "builderCenter": "https://builder.aws.com",
        "awsSignup": "https://aws.amazon.com/free",
        "mfaDocs": "https://docs.aws.amazon.com/IAM/latest/UserGuide/enable-virt-mfa-for-root.html",
        "budgetDocs": "https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html",
        "awsConsole": "https://console.aws.amazon.com/",
        "ai": "https://kiro.dev",
        "aiClaude": "https://docs.claude.com/en/docs/claude-code/overview",
    },
    "announcement": "",
}
CONFIG_KEYS = set(DEFAULT_CONFIG)
PROFILE_FIELDS = {"name": 80, "university": 40, "meetupName": 80, "githubUser": 40}


# ---------------- utilidades ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resp(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, default=_json_default, ensure_ascii=False),
    }


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    try:
        return int(o) if o == int(o) else float(o)
    except Exception:
        return str(o)


class HttpError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def body_of(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    if len(raw) > 200_000:
        raise HttpError(413, "Peticion demasiado grande")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HttpError(400, "JSON no valido")
    if not isinstance(data, dict):
        raise HttpError(400, "JSON no valido")
    return data


def claims_of(event):
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except KeyError:
        raise HttpError(401, "No autenticado")


def email_of(claims):
    e = (claims.get("email") or "").lower()
    if not e:
        raise HttpError(401, "Usa el ID token")
    return e


def is_admin(claims):
    # HTTP API serializa los arrays como "[admins otro]"
    if "admins" in str(claims.get("cognito:groups", "")):
        return True
    e = (claims.get("email") or "").lower()
    if not e:
        return False
    if e in ADMIN_EMAILS:
        return True
    return "Item" in table.get_item(Key={"PK": f"ADMIN#{e}", "SK": "ADMIN"})


def clean_url(v, field):
    v = (v or "").strip()
    if v and not URL_RE.match(v):
        raise HttpError(400, f"{field}: tiene que ser una URL https://")
    return v


def get_config():
    item = table.get_item(Key={"PK": "CONFIG", "SK": "EVENT"}).get("Item") or {}
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in item.items():
        if k in CONFIG_KEYS:
            if k == "links" and isinstance(v, dict):
                cfg["links"].update(v)
            else:
                cfg[k] = v
    return cfg


def parse_dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def norm_name(n):
    """'  Iván  Pérez-Gómez ' -> 'ivan perez gomez' para cruzar con Meetup por nombre completo."""
    n = unicodedata.normalize("NFKD", str(n or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", n).split())


def names_from_export(text):
    """Saca los nombres del export de Meetup (columna Name/Nombre) o, si no hay cabecera, una persona por linea."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return set()
    header = [c.strip().lower() for c in rows[0]]
    col = next((i for i, h in enumerate(header) if h in ("name", "nombre", "full name", "nombre completo")), None)
    if col is None:
        col = next((i for i, h in enumerate(header) if "name" in h or "nombre" in h), None)
    if col is not None:
        vals = [r[col] for r in rows[1:] if len(r) > col]
    else:
        vals = [r[0] for r in rows if not EMAIL_FIND.search(r[0])]
    return {norm_name(v) for v in vals if len(norm_name(v)) >= 3}


def in_meetup(email, name):
    if "Item" in table.get_item(Key={"PK": f"ALLOW#{email}", "SK": "MEETUP"}):
        return True
    nn = norm_name(name)
    return bool(nn) and "Item" in table.get_item(Key={"PK": f"ALLOWNAME#{nn}", "SK": "MEETUP"})


def get_profile(email):
    return table.get_item(Key={"PK": f"USER#{email}", "SK": "PROFILE"}).get("Item")


def tx(items):
    """TransactWriteItems con valores python -> tipos DynamoDB."""
    out = []
    for kind, spec in items:
        s = dict(spec)
        s["TableName"] = TABLE
        if "Key" in s:
            s["Key"] = {k: ser.serialize(v) for k, v in s["Key"].items()}
        if "Item" in s:
            s["Item"] = {k: ser.serialize(v) for k, v in s["Item"].items()}
        if "ExpressionAttributeValues" in s:
            s["ExpressionAttributeValues"] = {k: ser.serialize(v) for k, v in s["ExpressionAttributeValues"].items()}
        out.append({kind: s})
    ddb_client.transact_write_items(TransactItems=out)


def tx_reasons(e):
    return [r.get("Code") for r in e.response.get("CancellationReasons", [])]


# ---------------- rutas publicas ----------------
def public_config(event):
    cfg = get_config()
    return resp(200, {
        "config": cfg,
        "auth": {"region": REGION, "clientId": CLIENT_ID, "mode": AUTH_MODE},
    })


def register(event):
    data = body_of(event)
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()[:80]
    uni = (data.get("university") or "").strip()[:40]
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise HttpError(400, "Email no valido")
    if not name:
        raise HttpError(400, "Falta el nombre")
    password = str(data.get("password") or "")
    if AUTH_MODE == "password" and not (8 <= len(password) <= 128):
        raise HttpError(400, "La contraseña tiene que tener al menos 8 caracteres")
    if data.get("consent") is not True:
        raise HttpError(400, "Hace falta aceptar el tratamiento de datos")

    try:
        cognito.admin_create_user(
            UserPoolId=POOL, Username=email, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": email},
                            {"Name": "email_verified", "Value": "true"},
                            {"Name": "name", "Value": name}],
        )
        # Contrasena aleatoria que nadie conoce: deja al usuario CONFIRMED para entrar con codigo por email.
        pw = password if AUTH_MODE == "password" else \
            "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40)) + "aA1!"
        cognito.admin_set_user_password(UserPoolId=POOL, Username=email, Password=pw, Permanent=True)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "UsernameExistsException" and AUTH_MODE == "password":
            raise HttpError(409, "Ya hay una cuenta con ese email. Entra con tu contraseña.")
        if code == "InvalidPasswordException":
            try:
                cognito.admin_delete_user(UserPoolId=POOL, Username=email)  # no dejar cuentas sin contraseña valida
            except ClientError:
                pass
            raise HttpError(400, "La contraseña no es válida: usa al menos 8 caracteres")
        if code != "UsernameExistsException":
            raise

    allowed = in_meetup(email, name)
    token = secrets.token_urlsafe(24)
    try:
        tx([
            ("Put", {"Item": {
                "PK": f"USER#{email}", "SK": "PROFILE", "GSI1PK": "USERS", "GSI1SK": email,
                "email": email, "name": name, "university": uni, "meetupVerified": allowed,
                "checkToken": token, "consentAt": now_iso(), "createdAt": now_iso(),
            }, "ConditionExpression": "attribute_not_exists(PK)"}),
            ("Put", {"Item": {"PK": f"TOKEN#{token}", "SK": "USER", "email": email}}),
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        # ya existia el perfil: no pasa nada, que inicie sesion
    return resp(200, {"ok": True})


def checkin(event):
    """Lo llama la Lambda de comprobacion desplegada en la cuenta AWS del participante."""
    data = body_of(event)
    token = str(data.get("token", ""))
    if not TOKEN_RE.match(token):
        raise HttpError(400, "Token no valido")
    ref = table.get_item(Key={"PK": f"TOKEN#{token}", "SK": "USER"}).get("Item")
    if not ref:
        raise HttpError(404, "Token desconocido")
    account = str(data.get("accountId", ""))
    if not re.fullmatch(r"\d{12}", account):
        raise HttpError(400, "accountId no valido")
    raw = data.get("results") or {}
    results = {k: (v if isinstance(v, bool) else None) for k, v in raw.items()
               if isinstance(k, str) and re.fullmatch(r"[A-Za-z]{1,30}", k)}
    cfg = get_config()
    start = parse_dt(cfg.get("eventStart"))
    ts = datetime.now(timezone.utc)
    check = {
        "accountId": account,
        "region": str(data.get("region", ""))[:20],
        "results": results,
        "at": ts.isoformat(timespec="seconds"),
        "beforeStart": bool(start and ts < start),
    }
    table.update_item(
        Key={"PK": f"USER#{ref['email']}", "SK": "PROFILE"},
        UpdateExpression="SET awsCheck = :c, awsFirstCheckAt = if_not_exists(awsFirstCheckAt, :t)",
        ExpressionAttributeValues={":c": check, ":t": check["at"]},
    )
    return resp(200, {"ok": True, "results": results})


# ---------------- participante ----------------
def checker_launch_url(profile, cfg, event):
    if not CHECKER_URL:
        return ""
    region = cfg.get("participantRegion") or "eu-west-1"
    api = f"https://{event['requestContext']['domainName']}"
    return (f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
            f"#/stacks/quickcreate?templateURL={quote(CHECKER_URL, safe='')}"
            f"&stackName=hackathon-checkin&param_Token={profile['checkToken']}"
            f"&param_ApiUrl={quote(api, safe='')}")


def team_view(team_id, with_emails=False):
    team = table.get_item(Key={"PK": f"TEAM#{team_id}", "SK": "META"}).get("Item")
    if not team:
        return None
    members = sorted(team.get("members") or [])
    names = []
    if members:
        keys = [{"PK": f"USER#{m}", "SK": "PROFILE"} for m in members]
        got = ddb.batch_get_item(RequestItems={TABLE: {"Keys": keys, "ProjectionExpression": "email, #n",
                                                       "ExpressionAttributeNames": {"#n": "name"}}})
        by = {p["email"]: p.get("name", "") for p in got["Responses"].get(TABLE, [])}
        names = [{"name": by.get(m, ""), **({"email": m} if with_emails else {})} for m in members]
    return {"id": team_id, "name": team.get("name"), "code": team.get("code"),
            "members": names, "submission": team.get("submission")}


def get_me(event):
    claims = claims_of(event)
    email = email_of(claims)
    p = get_profile(email)
    if not p:
        raise HttpError(404, "No encontramos tu inscripcion. Registrate primero.")
    cfg = get_config()
    team = team_view(p["teamId"], with_emails=True) if p.get("teamId") else None
    p = {k: v for k, v in p.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK")}
    return resp(200, {
        "profile": p, "team": team, "config": cfg, "isAdmin": is_admin(claims),
        "checkerUrl": checker_launch_url(p, cfg, event),
    })


def put_me(event):
    email = email_of(claims_of(event))
    data = body_of(event)
    sets, vals = [], {}
    for f, maxlen in PROFILE_FIELDS.items():
        if f in data:
            sets.append(f"{f} = :{f}")
            vals[f":{f}"] = str(data[f]).strip()[:maxlen]
    if "builderCenterUrl" in data:
        sets.append("builderCenterUrl = :b")
        vals[":b"] = clean_url(data["builderCenterUrl"], "Perfil de Builder Center")
    if "aiReady" in data:
        sets.append("aiReady = :ai")
        vals[":ai"] = bool(data["aiReady"])
    if "awsReady" in data:
        sets.append("awsReady = :aw")
        vals[":aw"] = bool(data["awsReady"])
    if not sets:
        raise HttpError(400, "Nada que actualizar")
    sets.append("updatedAt = :u")
    vals[":u"] = now_iso()
    try:
        table.update_item(Key={"PK": f"USER#{email}", "SK": "PROFILE"},
                          UpdateExpression="SET " + ", ".join(sets),
                          ConditionExpression="attribute_exists(PK)", ExpressionAttributeValues=vals)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HttpError(404, "Registrate primero")
        raise
    return resp(200, {"ok": True})


def create_team(event):
    email = email_of(claims_of(event))
    name = (body_of(event).get("name") or "").strip()[:60]
    if len(name) < 2:
        raise HttpError(400, "Ponle nombre al equipo")
    team_id = secrets.token_hex(6)
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    try:
        tx([
            ("Put", {"Item": {"PK": f"TEAM#{team_id}", "SK": "META", "GSI1PK": "TEAMS", "GSI1SK": name.lower() + "#" + team_id,
                              "name": name, "code": code, "members": {email}, "createdBy": email, "createdAt": now_iso()}}),
            ("Put", {"Item": {"PK": f"CODE#{code}", "SK": "TEAM", "teamId": team_id},
                     "ConditionExpression": "attribute_not_exists(PK)"}),
            ("Update", {"Key": {"PK": f"USER#{email}", "SK": "PROFILE"},
                        "UpdateExpression": "SET teamId = :t",
                        "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(teamId)",
                        "ExpressionAttributeValues": {":t": team_id}}),
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            raise HttpError(409, "Ya estas en un equipo (o no estas registrado)")
        raise
    return resp(200, {"team": team_view(team_id)})


def join_team(event):
    email = email_of(claims_of(event))
    code = (body_of(event).get("code") or "").strip().upper()
    ref = table.get_item(Key={"PK": f"CODE#{code}", "SK": "TEAM"}).get("Item") if code else None
    if not ref:
        raise HttpError(404, "Codigo de equipo no valido")
    team_id = ref["teamId"]
    try:
        tx([
            ("Update", {"Key": {"PK": f"TEAM#{team_id}", "SK": "META"},
                        "UpdateExpression": "ADD members :m",
                        "ConditionExpression": "attribute_exists(PK) AND (attribute_not_exists(members) OR size(members) < :max)",
                        "ExpressionAttributeValues": {":m": {email}, ":max": MAX_TEAM}}),
            ("Update", {"Key": {"PK": f"USER#{email}", "SK": "PROFILE"},
                        "UpdateExpression": "SET teamId = :t",
                        "ConditionExpression": "attribute_exists(PK) AND attribute_not_exists(teamId)",
                        "ExpressionAttributeValues": {":t": team_id}}),
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            r = tx_reasons(e)
            if r and r[0] == "ConditionalCheckFailed":
                raise HttpError(409, f"El equipo ya tiene {MAX_TEAM} personas")
            raise HttpError(409, "Ya estas en un equipo")
        raise
    return resp(200, {"team": team_view(team_id)})


def leave_team(event):
    email = email_of(claims_of(event))
    p = get_profile(email)
    if not p or not p.get("teamId"):
        raise HttpError(400, "No estas en ningun equipo")
    tid = p["teamId"]
    tx([
        ("Update", {"Key": {"PK": f"TEAM#{tid}", "SK": "META"},
                    "UpdateExpression": "DELETE members :m",
                    "ExpressionAttributeValues": {":m": {email}}}),
        ("Update", {"Key": {"PK": f"USER#{email}", "SK": "PROFILE"},
                    "UpdateExpression": "REMOVE teamId", "ConditionExpression": "teamId = :t",
                    "ExpressionAttributeValues": {":t": tid}}),
    ])
    return resp(200, {"ok": True})


def submit(event):
    email = email_of(claims_of(event))
    p = get_profile(email)
    if not p or not p.get("teamId"):
        raise HttpError(400, "Primero crea o unete a un equipo")
    deadline = parse_dt(get_config().get("submissionDeadline"))
    if deadline and datetime.now(timezone.utc) > deadline:
        raise HttpError(403, "El plazo de entrega ha terminado")
    data = body_of(event)
    sub = {
        "repoUrl": clean_url(data.get("repoUrl"), "Repositorio"),
        "demoUrl": clean_url(data.get("demoUrl"), "Demo"),
        "videoUrl": clean_url(data.get("videoUrl"), "Video"),
        "slidesUrl": clean_url(data.get("slidesUrl"), "Presentacion"),
        "module": str(data.get("module", "")).strip()[:60],
        "description": str(data.get("description", "")).strip()[:1500],
        "awsServices": str(data.get("awsServices", "")).strip()[:300],
        "submittedBy": email,
        "submittedAt": now_iso(),
    }
    if not sub["repoUrl"]:
        raise HttpError(400, "El repositorio es obligatorio")
    table.update_item(Key={"PK": f"TEAM#{p['teamId']}", "SK": "META"},
                      UpdateExpression="SET submission = :s", ConditionExpression="attribute_exists(PK)",
                      ExpressionAttributeValues={":s": sub})
    return resp(200, {"ok": True, "submission": sub})


# ---------------- admins ----------------
def require_admin(event):
    if not is_admin(claims_of(event)):
        raise HttpError(403, "Solo organizacion")


def query_all(pk):
    items, kw = [], {"IndexName": "GSI1", "KeyConditionExpression": Key("GSI1PK").eq(pk)}
    while True:
        r = table.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def admin_participants(event):
    require_admin(event)
    users = query_all("USERS")
    for u in users:
        for k in ("PK", "SK", "GSI1PK", "GSI1SK", "checkToken"):
            u.pop(k, None)
    return resp(200, {"participants": users})


def admin_list(event):
    require_admin(event)
    rows = {a["PK"].split("#", 1)[1]: {"email": a["PK"].split("#", 1)[1], "addedBy": a.get("addedBy", ""),
            "addedAt": a.get("addedAt", ""), "fixed": False} for a in query_all("ADMINS")}
    for e in ADMIN_EMAILS:
        rows[e] = {**rows.get(e, {"email": e, "addedBy": "", "addedAt": ""}), "fixed": True}
    for r in rows.values():
        u = table.get_item(Key={"PK": f"USER#{r['email']}", "SK": "PROFILE"}).get("Item") or {}
        r["name"] = u.get("name", "")
        r["registered"] = bool(u)
    return resp(200, {"admins": sorted(rows.values(), key=lambda r: r["email"])})


def admin_set(event):
    require_admin(event)
    me = email_of(claims_of(event))
    data = body_of(event)
    e = str(data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(e):
        raise HttpError(400, "Email no valido")
    if data.get("admin", True):
        table.put_item(Item={"PK": f"ADMIN#{e}", "SK": "ADMIN", "GSI1PK": "ADMINS", "GSI1SK": e,
                             "addedBy": me, "addedAt": now_iso()})
    else:
        if e in ADMIN_EMAILS:
            raise HttpError(400, "Este admin es fijo (se configura al desplegar)")
        if e == me:
            raise HttpError(400, "No puedes quitarte a ti mismo")
        table.delete_item(Key={"PK": f"ADMIN#{e}", "SK": "ADMIN"})
    return admin_list(event)


def admin_teams(event):
    require_admin(event)
    teams = [team_view(t["PK"].split("#", 1)[1], with_emails=True) for t in query_all("TEAMS")]
    return resp(200, {"teams": [t for t in teams if t]})


def admin_allowlist(event):
    """Pega el export de Meetup (CSV). Se cruza por nombre completo y, si hay, por email."""
    require_admin(event)
    text = str(body_of(event).get("csv", ""))
    emails = sorted({e.lower() for e in EMAIL_FIND.findall(text)})
    names = sorted(names_from_export(text))
    with table.batch_writer() as bw:
        for e in emails:
            bw.put_item(Item={"PK": f"ALLOW#{e}", "SK": "MEETUP", "importedAt": now_iso()})
        for n in names:
            bw.put_item(Item={"PK": f"ALLOWNAME#{n}", "SK": "MEETUP", "importedAt": now_iso()})
    email_set, name_set = set(emails), set(names)
    matched = 0
    for u in query_all("USERS"):
        if u.get("meetupVerified"):
            continue
        if u["email"] in email_set or norm_name(u.get("name")) in name_set:
            table.update_item(Key={"PK": u["PK"], "SK": "PROFILE"}, UpdateExpression="SET meetupVerified = :t",
                              ExpressionAttributeValues={":t": True})
            matched += 1
    return resp(200, {"imported": len(names) + len(emails), "names": len(names), "emails": len(emails),
                      "matchedRegistered": matched})


def admin_config(event):
    require_admin(event)
    data = body_of(event)
    bad = set(data) - CONFIG_KEYS
    if bad:
        raise HttpError(400, f"Claves no validas: {', '.join(sorted(bad))}")
    for k in ("eventStart", "submissionDeadline", "timerStart"):
        if data.get(k) and not parse_dt(data[k]):
            raise HttpError(400, f"{k}: usa formato ISO, p.ej. 2026-12-04T21:00:00+01:00")
    if "links" in data:
        if not isinstance(data["links"], dict):
            raise HttpError(400, "links tiene que ser un objeto")
        for k, v in data["links"].items():
            clean_url(v, f"links.{k}")
    for k in ("agenda", "speakers"):
        if k in data and not (isinstance(data[k], list) and all(isinstance(x, dict) for x in data[k])):
            raise HttpError(400, f"{k} tiene que ser una lista de objetos")
    for sp in data.get("speakers") or []:
        if not str(sp.get("name") or "").strip():
            raise HttpError(400, "speakers: cada persona necesita name")
        if sp.get("url"):
            clean_url(sp["url"], "speakers.url")
    cur = table.get_item(Key={"PK": "CONFIG", "SK": "EVENT"}).get("Item") or {"PK": "CONFIG", "SK": "EVENT"}
    cur.update(json.loads(json.dumps(data)))
    cur["updatedAt"] = now_iso()
    table.put_item(Item=cur)
    return resp(200, {"config": get_config()})


PUBLIC_UNIS = ("UC3M", "UPM", "ICADE", "UCJC", "UCM", "UEM", "CUNEF")
_STATS_CACHE = {"at": 0.0, "body": None}


def public_stats(event):
    """Solo totales para la web pública: ni nombres, ni emails, ni horas exactas."""
    import time
    from datetime import timedelta
    if _STATS_CACHE["body"] and time.time() - _STATS_CACHE["at"] < 60:
        return resp(200, _STATS_CACHE["body"])
    def madrid(dt):  # UTC+1, o UTC+2 en horario de verano UE (sin depender de tzdata en Lambda)
        dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        def last_sun(m):
            d = datetime(dt.year, m, 31, 1, tzinfo=timezone.utc)
            return d - timedelta(days=(d.weekday() + 1) % 7)
        return dt + timedelta(hours=2 if last_sun(3) <= dt < last_sun(10) else 1)
    users = query_all("USERS")
    today = madrid(datetime.now(timezone.utc)).date()
    by_uni = {u: 0 for u in PUBLIC_UNIS + ("Otra",)}
    by_day = {}
    hoy = 0
    for u in users:
        uni = u.get("university") if u.get("university") in PUBLIC_UNIS else "Otra"
        by_uni[uni] += 1
        dt = parse_dt(u.get("createdAt"))
        if dt:
            d = madrid(dt).date()
            by_day[d] = by_day.get(d, 0) + 1
            if d == today:
                hoy += 1
    days = []
    if by_day:
        d, acc = min(by_day), 0
        while d <= today:
            acc += by_day.get(d, 0)
            days.append({"date": d.isoformat(), "new": by_day.get(d, 0), "total": acc})
            d += timedelta(days=1)
    teams = len(query_all("TEAMS"))
    body = {"total": len(users), "today": hoy, "teams": teams, "inTeam": sum(1 for u in users if u.get("teamId")),
            "byUni": by_uni, "days": days[-30:]}
    _STATS_CACHE.update(at=time.time(), body=body)
    return resp(200, body)


ROUTES = {
    ("GET", "/api/public/config"): public_config,
    ("GET", "/api/public/stats"): public_stats,
    ("POST", "/api/register"): register,
    ("POST", "/api/checkin"): checkin,
    ("GET", "/api/me"): get_me,
    ("PUT", "/api/me"): put_me,
    ("POST", "/api/teams"): create_team,
    ("POST", "/api/teams/join"): join_team,
    ("POST", "/api/teams/leave"): leave_team,
    ("PUT", "/api/teams/submission"): submit,
    ("GET", "/api/admin/participants"): admin_participants,
    ("GET", "/api/admin/teams"): admin_teams,
    ("GET", "/api/admin/admins"): admin_list,
    ("POST", "/api/admin/admins"): admin_set,
    ("POST", "/api/admin/allowlist"): admin_allowlist,
    ("PUT", "/api/admin/config"): admin_config,
}


def handler(event, context):
    rc = event.get("requestContext", {}).get("http", {})
    fn = ROUTES.get((rc.get("method"), event.get("rawPath")))
    if not fn:
        return resp(404, {"error": "Ruta no encontrada"})
    try:
        return fn(event)
    except HttpError as e:
        return resp(e.status, {"error": e.msg})
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return resp(500, {"error": "Error interno"})
