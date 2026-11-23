# Security policy

## Supported versions

| Version | Security fixes |
|---|---|
| 1.0.x | yes |
| older | no |

Fixes come out as a new 1.0.x release. Update the add-on in Orange (**Options → Add-ons…**); the next machine you start then runs the fixed version too, because the machine always runs the same version as the add-on.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue, a pull request or a discussion:

1. Open the [Security tab](https://github.com/ggaammdev/orange3-cloud/security) of the repository on GitHub.
2. Click **Report a vulnerability**.
3. Describe the problem, the versions and the cloud it affects, how to reproduce it, and what an attacker could do with it.

Only the maintainers see the report. We answer in the report itself, agree with you on when the fix and an advisory are published, and credit you in the advisory if you want.

Don't put real tokens, keys, project or account IDs or your data in the report; made-up values are enough.

## Scope

In scope:

- **The add-on** that runs in Orange on your computer: how it signs in to your cloud with the cloud's own command-line tool, what it creates in your account (machines, network rules, storage for your files, roles), and the files it keeps in its private folder (the machine's token and certificate).
- **The machine image** `ghcr.io/ggaammdev/orange3-cloud-machine` and the API server on it.
- **The API's token and TLS:** each machine's random bearer token, its self-signed certificate that the add-on trusts exclusively, and the network rule that lets only your computer's IP address reach the machine. A way to get a machine's token, to pass the certificate check, or to reach the API without the token is in scope.

Not in scope:

- What a holder of the token can do. The token is the machine's password: the add-on and the machine exchange Python pickles, so anyone with the token can run code on that machine, by design.
- Vulnerabilities in Orange, the clouds' services and command-line tools, or other packages. Please report those to their projects, and tell us if the add-on uses them in an unsafe way.
- Settings of your own cloud account that the add-on didn't make.
