# Security policy

This is research and teaching software from a final-year engineering project.
Run it on a private network or on your own machine: the services assume a
trusted network, the message bus accepts any client that can reach it, and the
control panel is open to anyone who can load the page.

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/YASHII-X46/Containerised-Digital-Twin-Platform-for-Evaluating-Network-Impacts-of-DER-Integration/security/advisories/new)
rather than opening a public issue. Please include what you did, what happened,
and which service and deployment mode (Linux or Windows containers) it affected.

Expect an acknowledgement within about a week. Fixes land on `main`.

## Scope

In scope: the services in this repository and their bus contract.

Out of scope: PSS SINCAL itself, which is Siemens' to maintain; the upstream
dependencies credited in the README, which are best reported to their own
projects; and deployments that publish the bus or the control panel to an
untrusted network.
