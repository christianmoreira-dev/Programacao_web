# PlayShop

This folder keeps the existing Angular application and adds a standalone Python
API. The API client is in `src/service/play-shop-api.service.ts`; the existing
Angular UI has not yet been connected to it.

## Run the API

From this folder, start the backend with Python 3:

```powershell
python backend/server.py
```

The API listens at `http://localhost:3000/api`. On first startup, it creates
`backend/playshop.db` and seeds the product catalog.

Run the backend tests with:

```powershell
python -m unittest discover -s backend -p "test_*.py"
```

## Run the existing Angular app

Install the frontend dependencies if needed, then start the app in a separate
terminal:

```powershell
npm install
npm start
```

## Optional email configuration

Email delivery is optional. Configure the SMTP environment variables in the
same terminal before starting the API. For Brevo, use the SMTP login and SMTP
key from the Brevo dashboard, and a verified sender address:

```powershell
$env:PLAYSHOP_SMTP_HOST = "smtp-relay.brevo.com"
$env:PLAYSHOP_SMTP_PORT = "587"
$env:PLAYSHOP_SMTP_USER = "BREVO_SMTP_LOGIN"
$env:PLAYSHOP_SMTP_PASSWORD = "BREVO_SMTP_KEY"
$env:PLAYSHOP_SMTP_FROM = "PlayShop <verified-sender@example.com>"
python backend/server.py
```

Never commit SMTP keys or other credentials.
