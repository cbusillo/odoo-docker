# Security Policy

## Supported Versions

Only the latest images built from `main` are supported. Fixes ship in new
images built from `main`.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/odoo-docker/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the image tag or digest, the impact, and the smallest steps that
reproduce it.

Do not send database dumps, customer data, credentials, or other personal
data. Use redacted or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- vulnerable or tampered packages added by this image's build;
- unsafe image defaults, such as file permissions, users, or services;
- the bundled Launchplane runtime health addon exposing data it should not;
  and
- build, registry, or GitHub Actions supply-chain problems.

Problems in Odoo itself should go to Odoo S.A., and problems in the base
OS packages should go to their upstream projects.
