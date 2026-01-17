# Roadmap (publik app)

## Mål
En publik app med inloggning, egen datalagring per användare och betal‑tiers.

## Rekommenderad stack (för få användare + hög gratisnivå)
- **Supabase** för Auth + Postgres (magic link, social login)
- **Stripe** för betalningar/tiers
- **Backend**: FastAPI (Python) eller Next.js API
- **Frontend**: Streamlit (snabbt) eller Next.js (mer flexibel auth/billing)

## Faser

### Fas 1 – Auth + grunddata
- Supabase‑projekt
- Auth: email + magic link
- Minimal användartabell
- Spara semesterdata per user

### Fas 2 – Kalender‑integrationer (Google/Outlook)
- OAuth per provider (Google Calendar, Microsoft Graph)
- Spara tokens säkert (server‑side)
- Synka/visa kalenderdata

### Fas 3 – Betalning/tiers
- Stripe produkter/plans
- Checkout + webhook
- Feature‑gates baserat på plan

### Fas 4 – Publicering
- Privacy policy + Terms
- Domän + analytics
- Support‑flöde

## Noteringar om kalender‑kopplingar
- Google/Outlook kräver OAuth och separata scopes
- Kräver ofta verifiering när appen blir publik
- Bäst att hantera via backend (inte enbart client‑side)
