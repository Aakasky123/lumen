# Security policy

Lumen drives a real computer with the user's signed-in sessions, so security reports matter a lot.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository, with steps to
reproduce and the impact you see.

In scope, among others:

- a page, document, or file that can make Lumen act against the user's goal (prompt injection);
- a way to make Lumen type a password, one-time code, or vault secret into the wrong place or send it to a model;
- a way past the approval gate for a consequential action (submit, send, pay) in approval mode;
- the local API or extension socket accepting commands from another origin or user.

We aim to acknowledge reports within a week and will credit reporters who want it.
