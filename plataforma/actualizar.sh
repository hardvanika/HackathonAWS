#!/usr/bin/env bash
# Actualiza la configuración guardada (objetivo 70, inicio viernes 4 dic 18:00, cierre 20:00, enlaces) y redespliega.
# Uso: bash actualizar.sh   (desde hackathon/plataforma, con la sesión de AWS abierta: aws login --profile default)
set -euo pipefail
export REGION=eu-north-1
# En el portátil usa el perfil default; en GitHub Actions las credenciales vienen del rol OIDC.
[[ -z "${GITHUB_ACTIONS:-}" ]] && export AWS_PROFILE=default
STACK=hackathon-plataforma
TABLE=$(aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$REGION" \
  --query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].PhysicalResourceId | [0]" --output text)
echo "Tabla: $TABLE"
aws dynamodb update-item --region "$REGION" --table-name "$TABLE" \
  --key '{"PK":{"S":"CONFIG"},"SK":{"S":"EVENT"}}' \
  --condition-expression "attribute_exists(PK)" \
  --update-expression "SET goal = :g, eventStart = :s, submissionDeadline = :d, links = :l" \
  --expression-attribute-values '{
    ":g": {"N": "70"},
    ":s": {"S": "2026-12-04T18:00:00+01:00"},
    ":d": {"S": "2026-12-04T20:00:00+01:00"},
    ":l": {"M": {
      "website": {"S": "https://db1470yzxtmai.cloudfront.net/"},
      "meetup": {"S": "https://www.meetup.com/aws-sbg-at-charles-iii-univ-of-madrid-colmenarejo-campus/"},
      "whatsapp": {"S": "https://chat.whatsapp.com/IuoL9mjiGwz3IfgPREzg9T"},
      "builderCenter": {"S": "https://builder.aws.com"},
      "awsSignup": {"S": "https://aws.amazon.com/free"},
      "mfaDocs": {"S": "https://docs.aws.amazon.com/IAM/latest/UserGuide/enable-virt-mfa-for-root.html"},
      "budgetDocs": {"S": "https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html"},
      "awsConsole": {"S": "https://console.aws.amazon.com/"},
      "ai": {"S": "https://kiro.dev"},
      "aiClaude": {"S": "https://docs.claude.com/en/docs/claude-code/overview"}
    }}
  }' && echo "Configuración guardada actualizada."
ADMIN_EMAILS=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Parameters[?ParameterKey=='AdminEmails'].ParameterValue" --output text)
[[ -n "${GITHUB_ACTIONS:-}" && -n "$ADMIN_EMAILS" ]] && echo "::add-mask::$ADMIN_EMAILS"
export ADMIN_EMAILS
bash deploy.sh
echo "Listo: https://db1470yzxtmai.cloudfront.net/"
