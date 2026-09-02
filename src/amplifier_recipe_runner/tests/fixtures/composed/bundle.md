---
bundle:
  name: composed
  version: 1.0.0

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main

agents:
  include:
    - composed:builder
---

# Composed
