#!/usr/bin/env bash
# Despliega la plataforma en la cuenta de SEA.
# Requisitos: AWS CLI v2 con credenciales de la cuenta (perfil activo).
#   Correos por SES: bash ses.sh remitente@dominio.es (verifica la identidad y redespliega).
set -euo pipefail
cd "$(dirname "$0")"

REGION="${REGION:-eu-north-1}"  # la cuenta "Hackathon AWS" solo permite eu-north-1
STACK="${STACK:-hackathon-plataforma}"
# SES: si no se pasan, se mantienen los del despliegue anterior (los guarda ses.sh)
prev() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" --output text 2>/dev/null | sed 's/^None$//' || true; }
SES_IDENTITY_ARN="${SES_IDENTITY_ARN:-$(prev SesIdentityArn)}"
SES_FROM="${SES_FROM:-$(prev SesFromEmail)}"
ADMIN_EMAILS="${ADMIN_EMAILS:-$(prev AdminEmails)}"  # admins fijos, separados por comas (por defecto, los de antes)
CHECKER="${CHECKER:-false}"
EXTRA=()
[[ -n "${DOMAIN:-}" ]] && EXTRA+=("DomainName=$DOMAIN" "CertificateArn=${CERT_ARN:?Falta CERT_ARN (us-east-1)}" "HostedZoneId=${ZONE_ID:-}")

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ART="hackathon-artifacts-$ACCOUNT-$REGION"
aws s3api head-bucket --bucket "$ART" 2>/dev/null || \
  aws s3api create-bucket --bucket "$ART" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null

# Sin SAM CLI: CloudFormation empaqueta la Lambda (no tiene dependencias externas)
aws cloudformation package --template-file template.yaml --s3-bucket "$ART" --output-template-file .packaged.yaml --region "$REGION" >/dev/null
aws cloudformation deploy --template-file .packaged.yaml --stack-name "$STACK" --region "$REGION" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --no-fail-on-empty-changeset \
  --parameter-overrides "SesIdentityArn=$SES_IDENTITY_ARN" "SesFromEmail=$SES_FROM" "EnableChecker=$CHECKER" "AdminEmails=$ADMIN_EMAILS" ${EXTRA[@]+"${EXTRA[@]}"}

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

# Web: one pager (../onepager/site) en la raiz + portal en /app/
BUILD=$(mktemp -d)
cp -r frontend/. "$BUILD/"
cp ../onepager/site/index.html ../onepager/site/reto.html "$BUILD/"
aws s3 sync "$BUILD/" "s3://$(out SiteBucket)/" --delete --region "$REGION"
[[ "$CHECKER" == "true" ]] && aws s3 cp checker/checkin.yaml "s3://$(out CheckerBucket)/checker/checkin.yaml" --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" >/dev/null

echo "Listo: $(out SiteUrl)/app/"
if [[ -n "$SES_FROM" ]]; then echo "Correos (confirmar email y recuperar contraseña): por SES desde $SES_FROM"
else echo "Correos: remitente por defecto de Cognito (max. 50/dia). Para usar SES: bash ses.sh tu@email"; fi
N_ADMINS=$(awk -F, 'NF{print NF}' <<<"${ADMIN_EMAILS:-}"); echo "Admins fijos: ${N_ADMINS:-0}. El resto se dan desde el panel (Organización > Admins)."
