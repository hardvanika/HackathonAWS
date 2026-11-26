# Hackathon AWS x SEA

Web y plataforma del hackathon AWS Student Builder Group UC3M x SEA (jueves 3 dic 2026, UC3M Puerta de Toledo).

- `onepager/`: web pública. Se edita solo `generar.py` y se regenera con `python3 generar.py` (genera `index.html`, `reto.html` y `site/`).
- `plataforma/`: portal de participantes y panel admin (SAM: S3 + CloudFront, Cognito, API Gateway HTTP, Lambda Python, DynamoDB).
  - Desplegar: `cd plataforma && bash actualizar.sh` (necesita sesión AWS activa).

No subas credenciales, `.env` ni datos de participantes a este repo: es público.
