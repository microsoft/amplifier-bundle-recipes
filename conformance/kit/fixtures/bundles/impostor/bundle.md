---
bundle:
  name: supplier
  version: 0.0.1
  description: A DIFFERENT bundle claiming the same namespace and agent name.

agents:
  include:
    - supplier:reviewer
---

# impostor

Stands in for a CALLER/host bundle that happens to supply a colliding name.
Never declared by a good fixture's recipe -- that is the entire point.
