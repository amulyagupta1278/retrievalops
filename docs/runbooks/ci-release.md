# CI and immutable image release

The `ci` workflow is the only image publisher. Pull requests and `main` pushes run formatting,
linting, strict typing, the complete test suite against PostgreSQL, both controlled benchmarks,
dependency audit, Prometheus rule validation, a container smoke test, and a HIGH/CRITICAL image
scan. A `main` push publishes only after both gate jobs pass.

Published images use only `ghcr.io/<owner>/<repository>:sha-<40-character-commit>` for deployment.
The workflow emits a registry-backed GitHub artifact attestation and signs the immutable digest
with Sigstore keyless signing. Deployments must resolve and store that digest; mutable tags are
not release inputs.

## Required repository rule

Configure a GitHub ruleset for `main` that:

- requires pull requests and at least one approval;
- requires `quality` and `container` status checks to pass and be current;
- blocks force pushes and branch deletion;
- requires conversation resolution; and
- prevents bypass, including administrators, except an audited break-glass role.

Until this ruleset and one green hosted run are independently visible, T16 is not considered
fully proven even when every local check passes.

## Verification

Verify an image before deployment:

```sh
cosign verify --certificate-identity-regexp '^https://github.com/.+/.github/workflows/ci.yml@refs/heads/main$' --certificate-oidc-issuer https://token.actions.githubusercontent.com IMAGE@DIGEST
gh attestation verify IMAGE@DIGEST --repo OWNER/REPOSITORY
```

If any gate or publication step fails, no deployable digest is handed to the rollout workflow;
the running application and MLflow policy aliases remain unchanged.
