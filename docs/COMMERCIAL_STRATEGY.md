# Thump commercial strategy

Status: product hypothesis, not a commitment to shipped features or pricing.

## Recommendation

Keep **Thump Open Source** complete and useful for self-hosting. Build **Thump Cloud** as a managed control plane for teams that want private-network monitoring without operating public ingress, Redis, TLS, upgrades, or a highly available Thump deployment.

The commercial value is operational relief and team governance—not removing features from the open-source edition.

## The buyer and the job

Initial buyer:

- a small platform, DevOps, or SRE team
- already pays for an uptime provider and does not want another paging stack
- operates cron jobs and internal services in one or more private networks
- can deploy a small connector but does not want to expose or operate Thump publicly

Job to be done:

> Show my existing uptime system whether private jobs and services are healthy, without opening inbound network access or operating another production control plane.

This is narrower and more credible than competing as a general observability platform.

## Product line

### Thump Open Source — available now

The open-source edition remains the trustworthy foundation:

- heartbeat and cron-schedule checks
- private HTTP probes
- plain `200`/`503` status endpoints
- SQLite or Redis storage
- Prometheus metrics and Grafana assets
- Docker and Kubernetes deployment
- full source access and no artificial check limit

The community edition should not be weakened to manufacture demand for Cloud. Its adoption, transparency, and deployability are the commercial funnel.

### Thump Cloud — proposed paid product

Thump Cloud should consist of:

1. **A lightweight connector inside each private network**
   - initiates outbound-only, mutually authenticated connections
   - runs probes and forwards signed observations
   - buffers briefly through transient network interruptions
   - contains no alert-routing complexity

2. **A managed control plane**
   - hosts the public `200`/`503` endpoints
   - stores check state and event history
   - manages connector identity and rotation
   - provides a team dashboard and configuration workflow
   - handles upgrades, persistence, TLS, and high availability

3. **Team and governance features**
   - organizations, environments, and role-based access
   - audit history
   - connector fleet health and version visibility
   - configuration validation and change history
   - SSO/SAML and policy controls for larger customers later

The first paid release should still rely on the customer's existing uptime vendor for paging. Building a second alerting product would erase Thump's strongest positioning.

## Free versus paid boundary

| Capability | Open Source | Cloud |
|---|---:|---:|
| Heartbeats and private probes | Yes | Yes |
| Unlimited self-hosted checks | Yes | — |
| Plain status endpoints | Self-hosted | Managed |
| Public ingress, TLS, Redis, backups | Customer operates | Managed |
| Outbound-only private connector | Later, source-visible client | Managed enrollment and fleet |
| Team dashboard | Basic API/Grafana | Hosted UI |
| Organizations and roles | No | Yes |
| Audit and configuration history | Local event history | Yes |
| Connector identity rotation | Manual | Managed |
| SSO/SAML and policy controls | No | Business tier |
| Support | Community | Email / priority by tier |

A source-visible connector lowers adoption friction and avoids asking customers to place an opaque binary inside private networks. The paid moat is the managed service, fleet control, governance, and support.

## Pricing hypotheses

Do not publish these as final pricing until interviews and a working beta establish value. Test simple, predictable plans rather than per-event billing.

### Hypothesis A — recommended launch test

| Plan | Price hypothesis | Included | Intended user |
|---|---:|---|---|
| Open Source | $0 | Unlimited self-hosted checks | operators happy to run it |
| Cloud Solo | $9/month | 25 checks, 2 connectors, 30-day history | homelab and solo operators |
| Cloud Team | $29/month | 100 checks, 10 connectors, 90-day history, 5 members | small platform teams |
| Cloud Business | $99/month | 500 checks, 50 connectors, 1-year history, SSO, audit export, priority support | established teams |

Use checks and connectors as visible limits. Avoid charging by heartbeat or probe event: it makes reliable monitoring feel financially dangerous.

### Founding-customer alternative

Offer one **Founding Team** plan at $19/month for the first design partners, with generous limits and a permanent discount. One plan is easier to validate before billing, entitlement, and support complexity multiply.

## Paid feature ideas, ranked

### Build first

1. Outbound-only connector enrollment and rotation.
2. Managed status endpoints with custom check slugs.
3. Connector and check inventory across environments.
4. Configuration validation, preview, and safe rollout.
5. Event history and connector-offline diagnosis.
6. Basic organization membership and owner/member roles.
7. Billing with one founding plan.

### Build after demand is proven

1. Terraform provider or declarative API.
2. GitHub/GitLab configuration sync.
3. Maintenance windows and temporary check pauses.
4. Audit export and configurable retention.
5. SSO/SAML and SCIM.
6. Regional data residency.
7. Private hosted status domains and IP allowlists.
8. Service-provider/multi-tenant accounts for MSPs.

### Do not build yet

- a full incident-management or on-call product
- logs, traces, or metrics ingestion
- synthetic browser testing
- a marketplace of dozens of integrations
- AI root-cause summaries without enough underlying evidence
- a mobile application
- a complex usage-based billing system

Those features create crowded product categories and a much larger operational burden before the narrow bridge is validated.

## Commercial roadmap

### Phase 0 — demand validation

- publish the combined OSS/Cloud website
- label Cloud as planned and invite design-partner research
- collect use cases through a public design-partner issue template
- interview 10 operators about ingress, HA, security review, connector count, and budget
- success gate: at least 5 qualified teams describe the same painful operational job and 3 agree to test a paid beta

### Phase 1 — technical beta

- one hosted region
- organizations with owner/member roles
- connector enrollment using short-lived bootstrap credentials and renewable identity
- heartbeat and HTTP probe parity with OSS
- hosted status endpoints
- connector health, check state, and event history
- manual onboarding and no self-service billing

Success gate: three teams run production-adjacent checks for 30 days, connector recovery is understood, and no customer needs inbound access.

### Phase 2 — paid founding plan

- self-service organization and connector onboarding
- metering by active checks/connectors
- one monthly subscription
- backups, restore drills, operational dashboards, support runbook, and public service status
- explicit service terms without promising an SLA the operation cannot yet sustain

### Phase 3 — team and enterprise controls

- configuration history and approvals
- audit export
- SSO/SAML and SCIM
- longer retention and regional options
- priority support and a defensible SLA

## Architecture principles

- Outbound-only from customer networks; no Cloud-initiated connection into private infrastructure.
- Per-connector identity with rotation and immediate revocation.
- Signed observations with replay protection and monotonic sequencing.
- Tenant isolation enforced in every storage key, query, and API authorization path.
- Keep check evaluation semantics shared with the open-source project to prevent product drift.
- Treat the hosted status endpoint as public but minimally revealing, matching today's `up`/`down` contract.
- Preserve an export path so customers can leave Cloud and self-host.

## Risks

| Risk | Mitigation |
|---|---|
| Market is too narrow | Validate willingness to pay before building alerting or dashboards broadly |
| Connector becomes a security concern | Source-visible client, outbound-only transport, minimal privileges, signed releases |
| Hosting reliability undermines the monitor | Start with a small design-partner cohort; build restore and failure testing before paid launch |
| OSS users perceive an open-core bait and switch | Keep current monitoring capabilities and unlimited self-hosted checks in OSS |
| Existing uptime vendors add private agents | Differentiate through vendor neutrality, simplicity, exportability, and self-host option |
| Support burden exceeds revenue | One founding plan, documented limits, narrow platform support, no bespoke integrations initially |

## Website truth rules

Until Cloud exists, public copy must say **planned**, **product research**, or **design partners**. Do not claim:

- production availability
- customer counts or logos
- an SLA
- integrations that are not implemented
- finalized prices
- security certifications
- a working hosted dashboard

The current website therefore presents Open Source as available now and Cloud as a clearly labeled product direction.