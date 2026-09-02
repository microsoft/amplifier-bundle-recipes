---
bundle:
  name: umbrella
  version: 1.0.0
  description: Fixture bundle that INCLUDES another bundle.

includes:
  - bundle: ../satellite

agents:
  include:
    - umbrella:lead
---

# Umbrella

Its own roster is one agent; composing its include adds `satellite:helper`,
whose definition lives in an entirely different tree.
