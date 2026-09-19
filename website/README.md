# Thump website

A dependency-free static marketing site for Thump Open Source and the proposed Thump Cloud managed offering.

## Run locally

```bash
python -m http.server 8088 --directory website
```

Open <http://127.0.0.1:8088>.

## Structure

- `index.html` — semantic page content and product copy
- `styles.css` — responsive visual system
- `app.js` — mobile-navigation and copy-button enhancements
- `assets/` — logo and favicon

The site has no build step, package dependencies, analytics, cookies, or remote fonts. Essential navigation and content work without JavaScript.

## Product-claim policy

Thump Open Source may be described as available. Thump Cloud must remain labeled as planned, preview, or early access until a working hosted service exists. Do not add customer logos, uptime claims, certifications, launch dates, or finalized prices without evidence.
