---
bundle:
  name: acme
  version: 1.2.0
  description: Fixture bundle supplying review agents.

agents:
  include:
    - acme:reviewer
    - acme:summarizer
---

# Acme

Fixture bundle. No modules, no hooks -- planning only reads the roster.
