---
bundle:
  name: lean-caller
  version: 1.0.0
  description: The CALLER bundle. Supplies no reviewer of any kind.

agents:
  include:
    - lean-caller:packager
---

# lean-caller

Models the calling session in the "run from a caller lacking the agent"
fixture. Its roster is deliberately disjoint from every recipe's closure.
