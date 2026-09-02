---
bundle:
  name: supplier
  version: 3.1.0
  description: Fixture bundle supplying the agents a recipe declares.

agents:
  include:
    - supplier:reviewer
    - supplier:extra
---

# Supplier

Fixture bundle. No modules, no hooks -- execution only reads the roster.
