# RESEARCH — Prior Art Review Tracker

Prior reverse-engineering of the Ajazz / Sinowealth protocol (VID `0x1A2C`)
used as reference material for Phase 5 (WebHID) and the Interactive Snake Game.

Review status:

- [ ] `shrey715/openajazz` — pioneering Python-based CLI/Web control, macros, and music sync for the AK820 Max Plus
- [ ] `NotAProton/ak820-utils` — interactive per-key RGB minigames (Whack-a-mole) and audio visualizers in Go on the Max Plus
- [ ] `MemerGamer/AjazzAk820ProUnofficialWebdriver` — functional TypeScript WebHID architecture (`protocol.ts`, `device.ts`) for the AK820 Pro
- [ ] `fromagge/ak820-screen` — WebHID TFT screen manipulation for the AK820 series
- [ ] `arafatamim/OpenRGBAK820Plugin` — AK820 Max Plus mapped into OpenRGB; identified the Sinowealth Keyboard Controller architecture

## Review notes

**Before starting Phase 5 (WebHID):**
Review `MemerGamer`'s `navigator.hid` filter and `usagePage` implementations
for the AK820 Pro WebHID architecture — these inform our device selection and
report framing for the AK820 Max Plus (`VID 0x1A2C` / `PID 0xA036`).

**For the Snake Game:**
Review `NotAProton`'s game loop matrix handling — its per-key RGB minigame
(Whack-a-mole) demonstrates how to drive interactive key-level lighting from a
host loop, which is directly applicable to an interactive Snake Game on the
matrix.

**Cross-reference:**
- Protocol wire format: [PROTOCOL.md](PROTOCOL.md)
- Phase roadmap: [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)
- Task list: [TODO.md](TODO.md)
