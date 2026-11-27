#!/usr/bin/env bash
# Pone Amazon SES como remitente de los correos de la plataforma
# (confirmar el email al registrarse y «¿Olvidaste tu contraseña?").
#
# Uso (desde hackathon/plataforma, con la sesión abierta: aws login --profile default):
#   bash ses.sh remitente@dominio.es     1) verifica el remitente  2) vuelve a ejecutarlo tras pulsar el enlace del correo
#   bash ses.sh --estado                 dice si el remitente está verificado y si la cuenta sigue en el sandbox
#   bash ses.sh --salir-sandbox          pide a AWS acceso de producción (enviar a cualquier email). Tarda ~24 h.
set -euo pipefail
cd "$(dirname "$0")"
export REGION="${REGION:-eu-north-1}"
[[ -z "${GITHUB_ACTIONS:-}" ]] && export AWS_PROFILE="${AWS_PROFILE:-default}"
STACK="${STACK:-hackathon-plataforma}"
SITE="https://db1470yzxtmai.cloudfront.net/"

sandbox() {
  local p; p=$(aws sesv2 get-account --region "$REGION" --query ProductionAccessEnabled --output text)
  if [[ "$p" == "True" ]]; then echo "SES: cuenta en producción (puede enviar a cualquier email)."
  else
    echo "SES: la cuenta sigue en el SANDBOX: solo llegan correos a direcciones verificadas en SES y máx. 200/día."
    echo "     Para que llegue a todos los participantes: bash ses.sh --salir-sandbox"
    echo "     Para probar antes, verifica tu propio email de prueba: aws sesv2 create-email-identity --email-identity tu@email --region $REGION"
  fi
}

case "${1:-}" in
  ""|-h|--help) sed -n 2,8p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  --estado)
    FROM=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
      --query "Stacks[0].Parameters[?ParameterKey=='SesFromEmail'].ParameterValue" --output text)
    echo "Remitente configurado en la plataforma: ${FROM:-ninguno (remitente por defecto de Cognito, máx. 50/día)}"
    sandbox; exit 0 ;;
  --salir-sandbox)
    aws sesv2 put-account-details --region "$REGION" --production-access-enabled \
      --mail-type TRANSACTIONAL --contact-language EN --website-url "$SITE" \
      --use-case-description "Transactional email only for a free student hackathon (AWS Student Builder Group at UC3M x Spanish Entrepreneurship Association, Madrid, 4 Dec 2026, about 70 attendees). Amazon Cognito sends one email-verification code when a participant signs up on our platform and password-reset codes when they ask for one. Recipients are only people who register themselves on the site and consent. No marketing or bulk email. Expected volume under 500 emails in total. Bounces and complaints are handled by SES account-level suppression; addresses are removed after the event."
    echo "Solicitud enviada. AWS responde por email (normalmente en 24 h). Mira el estado con: bash ses.sh --estado"
    exit 0 ;;
esac

FROM="$1"
[[ "$FROM" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[a-zA-Z]{2,}$ ]] || { echo "Email no válido: $FROM"; exit 1; }
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

STATUS=$(aws sesv2 get-email-identity --email-identity "$FROM" --region "$REGION" \
  --query VerifiedForSendingStatus --output text 2>/dev/null || echo NONE)
if [[ "$STATUS" == "NONE" ]]; then
  aws sesv2 create-email-identity --email-identity "$FROM" --region "$REGION" >/dev/null
  echo "Hecho. AWS acaba de mandar un correo a $FROM con un enlace de verificación."
  echo "Ábrelo, pulsa el enlace (caduca en 24 h) y vuelve a ejecutar: bash ses.sh $FROM"
  exit 0
fi
if [[ "$STATUS" != "True" ]]; then
  echo "$FROM aún no está verificado. Pulsa el enlace del correo de AWS y vuelve a ejecutar: bash ses.sh $FROM"
  exit 1
fi

echo "$FROM verificado en SES ($REGION). Redesplegando para que Cognito mande los correos con SES..."
# Rol con el que Cognito envía por SES (si ya existe, no pasa nada)
aws iam create-service-linked-role --aws-service-name email.cognito-idp.amazonaws.com >/dev/null 2>&1 || true
export SES_IDENTITY_ARN="arn:aws:ses:$REGION:$ACCOUNT:identity/$FROM"
export SES_FROM="$FROM"
bash deploy.sh
sandbox
