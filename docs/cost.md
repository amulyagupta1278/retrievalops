# Low-cost deployment

The week-one target is one 8 GB x86 K3s VPS, not infrastructure high availability. As of
2026-09-01, Hetzner lists the shared CX33 (4 vCPU, 8 GB, 80 GB) at €8.49/month excluding IPv4 and
VAT after its June 2026 price adjustment. Add the provider's current IPv4 fee, a domain, backups,
and any outbound traffic overage. Verify the live price before purchase:
https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/

GitHub Actions, GHCR, Let's Encrypt, K3s, Traefik, Prometheus, MLflow, PostgreSQL, and Argo
Rollouts can be run on free/open-source tiers for this low-traffic demo. The dominant costs are the
VPS, domain, and backups. MiniLM inference is CPU-only and the application makes no paid LLM calls.

This budget deliberately trades cluster HA and managed services for visible production mechanics.
Set upload/traffic alerts and a provider budget alert; delete the VPS—not merely stop it—when the
demo is retired because cloud resources remain billable while they exist.
