---
bundle:
  name: supplier
  version: 0.0.1
  description: A DIFFERENT bundle claiming the same namespace and agent name.

agents:
  include:
    - supplier:reviewer
---

# Impostor

Stands in for a CALLER/host bundle that happens to supply a colliding name.
It is never declared by any recipe here -- that is the point.
