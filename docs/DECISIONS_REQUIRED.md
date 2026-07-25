# Decisions still required from the owner

Эти вопросы не блокируют локальный Docker-first MVP, но должны быть закрыты до соответствующего scale, production или local-GPU этапа.

## Before external deployment

- [ ] Production target: single server, Docker Swarm, Kubernetes or managed platform.
- [ ] Public domain, TLS termination and reverse proxy.
- [ ] Authentication: local credentials, OIDC provider or enterprise SSO.
- [ ] Tenant onboarding and role matrix.
- [ ] Data retention, deletion and backup requirements.
- [ ] Whether user document contents may be sent to OpenRouter.
- [ ] Region/residency and compliance requirements.

## Before Wikipedia ingestion at scale

- [x] First local source: Russian Wikimedia XML pages-articles multistream dump `ruwiki-20260701`.
- [ ] Additional Wikipedia languages and future exact ZIM snapshots.
- [ ] Maximum corpus size and available disk.
- [ ] Whether citations point to local article viewer, canonical web URL or both.
- [ ] Required update cadence for snapshots.

## Before local llama.cpp phase

- [ ] Operating system and container runtime.
- [ ] GPU model(s), count and VRAM.
- [ ] System RAM and CPU.
- [ ] Maximum acceptable model disk footprint.
- [ ] Target simultaneous users and expected query rate.
- [ ] Allowed model licenses.
- [ ] Required Russian/English quality thresholds.

## Before production release

- [ ] RPO/RTO and restore drill requirements.
- [ ] Malware scanning policy and ClamAV requirement.
- [ ] Observability retention and access policy.
- [ ] Human review ownership for evaluation set and citation failures.
- [ ] Error budget and on-call ownership.
