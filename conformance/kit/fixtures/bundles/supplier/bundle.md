---
bundle:
  name: supplier
  version: 3.1.0
  description: Fixture bundle. The DECLARED dependency of every good fixture.

agents:
  include:
    - supplier:reviewer
    - supplier:summarizer
---

# supplier

Conformance fixture. No modules, no hooks: resolution only reads the roster.
`summarizer` exists so the behavior-partial fixture has something to NOT get.
