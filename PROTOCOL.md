# AJAZZ AK820 Max Plus — HID Protocol Notes

Reverse-engineered from the vendor configuration utility by dynamic instrumentation
(Frida `WriteFile` / `HidD_SetFeature` / `DeviceIoControl` hooks) plus scripted
Windows UI automation to drive the UI and correlate packets with on-screen actions.

| Item | Value |
|---|---|
| Target | `DeviceDriver.exe` (AJAZZ AK820 Max Plus "三模RGB版" config tool) |
| Install dir | `C:\Program Files\AJAZZ AK820 Max Plus\` |
| Binary | PE32, x86, 1 618 296 bytes, MFC (`mfc140u.dll`) |
| SHA-256 | `EC1CE433810EC64E2F5C135CD3683338D13766A87F35D2B3FFB44D19F7CA617F` |
| UI module | `XYUI.dll`, SHA-256 `D7078ED3F3E4CBBCB36097BBAB4B34C554713CD9146F5BFFE99A6EF5798B060F` |

**Confidence legend:** ✅ measured and reproduced · ⚠️ inferred from partial evidence · ❓ open

---

## 1. Transport

All lighting traffic is written with **`WriteFile` on the HID device handle**.

* Report length: **65 bytes** = 1 report-ID byte + 64 payload bytes.
* Report ID: **`0x00`**.
* The app sends a **full state snapshot** on every change — never a delta.
* `HidD_SetFeature` and `DeviceIoControl` were hooked for the entire session and
  **never fired** for lighting. That rules out feature reports and raw IOCTL paths,
  even though `HidD_SetFeature` is resolved dynamically at runtime.

### Device identity ✅

Independently confirmed by enumerating the live device and by reproducing the write
from a plain Python `CreateFileW` + `WriteFile` sequence (no vendor DLLs):

| Property | Value |
|---|---|
| USB product string | `AK820` |
| Vendor ID | **`0x1A2C`** |
| Product ID | **`0xA036`** |
| Target interface | `MI_00` |
| Usage page / usage | `0xFF02` / `0x0002` (vendor-defined) |
| Output report length | **65 bytes** |

`MI_00` is the only collection exposing a 65-byte output report, so it is
unambiguous. The device is composite; the other collections are ordinary input
interfaces and must not be written to:

| Path fragment | Usage page | Usage | Out len | Role |
|---|---|---|---|---|
| `&mi_00` | `0xFF02` | `0x0002` | 65 | **lighting / vendor channel** |
| `&mi_02&col05` | `0xFF01` | `0xFF01` | 0 | vendor-defined, input only |
| `&mi_02&col02` | `0x0001` | `0x0080` | 0 | system control |
| `&mi_02&col03` | `0x000C` | `0x0001` | 0 | consumer control |
| `&mi_01`, `&mi_02&col01/col04` | — | — | — | keyboard collections |

The handle is opened with `GENERIC_READ | GENERIC_WRITE` and
`FILE_SHARE_READ | FILE_SHARE_WRITE`. Shared access works while the vendor app is
running, so the two can coexist (the last writer wins on any given frame).

### Frame header

Every report starts with the same 5-byte prefix:

```
00  AA 55 CC 33   ...
^   ^^^^^^^^^^^
|   magic
report ID
```

The byte at offset **`[5]` is the command/opcode** and selects the payload layout.

### Command set (observed)

| Opcode | Direction | Purpose |
|---|---|---|
| `0x07` | host → device | Lighting configuration (mode / brightness / speed / colour) |
| `0x0E` | host → device | Keep-alive. Sent every **~5.02 s** while the app is open, all-zero payload, independent of user action |
| `0xF6`–`0xFB` | host → device | Per-key RGB table push — 6 sequential reports, see §4 |
| `0x0A` | host → device | Commit / apply the per-key RGB table. Empty payload |

`0x07` packets are also re-sent periodically as part of the state refresh, which is
why the same frame can appear more than once for a single UI action.

The port reproduces the `0x0E` heartbeat every 5 s (`animation_engine.py`) so the
traffic pattern matches the vendor app while a host-driven effect is running.

---

## 2. Lighting configuration frame (`0x07`)

65-byte layout:

| Byte | Field | Notes |
|---|---|---|
| `[0]` | `report_id` | `0x00`, constant |
| `[1..4]` | `magic` | `AA 55 CC 33`, constant |
| `[5]` | `command` | `0x07` |
| `[6]` | `mode` | `0x00`–`0x12`, see §3 |
| `[7]` | `brightness` | UI level **− 1** (`1..5` → `0x00..0x04`) |
| `[8]` | `speed` | UI level **− 1** (`1..5` → `0x00..0x04`); retained globally even in static modes |
| `[9]` | `variant` | Mode-specific flag, see below |
| `[10]` | `color` | Palette index `0x00`–`0x07`, see §3 |
| `[11..64]` | padding | `0x00` |

### There is no R/G/B triplet in this frame

This is the key negative result: the `0x07` lighting frame **does not carry per-channel
colour**. It carries a **palette index** in `[10]`, and the keyboard firmware resolves
the actual colour.

Evidence: sweeping the colour swatches Red → Green → Blue → Yellow → Magenta → Cyan
advanced `[10]` monotonically `0x00 → 0x05` while bytes `[7]`,`[8]`,`[9]` stayed
byte-identical. No `FF 00 00` / `00 FF 00` / `00 00 FF` pattern appeared anywhere in
any `0x07` frame across 65 captured packets.

True per-key RGB lives in the separate `0xF6`–`0xFB` table family (§4).

### `variant` (`[9]`)

| Value | Observed for |
|---|---|
| `0x00` | Static constant light, Overall breathing, all other tested modes |
| `0x01` | Kaleidoscope |

Uncharacterised. Kaleidoscope's panel exposes only Brightness / Speed / Colours — no
visible control writes this byte, so it appears to be an internal mode parameter. ❓

### Brightness / speed range — top level is `0x04`, not `0x07`

Measured by pixel-scanning the slider track: the active track spans x≈355–578 and the
handle centre for the displayed value **5** sits at x≈569, i.e. the right-hand end.
There is no track beyond x≈578 (background pixels only). A value of **5 is therefore
the maximum**, and:

```
brightness_byte = level - 1      // 1..5 -> 0x00..0x04
speed_byte      = level - 1      // 1..5 -> 0x00..0x04
```

Corroboration: brightness 5 → `[7]=0x04`, brightness 1 → `[7]=0x00`;
speed 4 → `[8]=0x03`, speed 1 → `[8]=0x00`. Speed 4 sits at x≈517 on a 355–578 track,
which fits a five-position scale and not an eight-position one.

> Synthetic input (SendInput drags, arrow keys) could not push either slider above 5.
> This was resolved by measuring the track geometry rather than by driving it further.

---

## 3. Enumerations

### Lighting mode (`[6]`) — 0-based index into the UI list

```c
typedef enum {
    AJAZZ_MODE_LEFT_RIGHT_RIPPLES   = 0,   /* Left and right ripples   */
    AJAZZ_MODE_STARRY_DOTS          = 1,   /* Starry dots              */
    AJAZZ_MODE_UP_DOWN_RIPPLES      = 2,   /* Up and Down Ripples      */
    AJAZZ_MODE_BIDIRECTIONAL_EJECT  = 3,   /* Bidirectional ejection   */
    AJAZZ_MODE_SPECTRAL_CYCLE       = 4,   /* Spectral cycle           */
    AJAZZ_MODE_KALEIDOSCOPE         = 5,   /* Kaleidoscope             */
    AJAZZ_MODE_STATIC               = 6,   /* Static constant light    */
    AJAZZ_MODE_OVERALL_BREATHING    = 7,   /* Overall breathing        */
    AJAZZ_MODE_CROSS_OPERATION      = 8,   /* Cross operation          */
    AJAZZ_MODE_SINGLE_KEY_BREATHING = 9,   /* Single key breathing     */
    AJAZZ_MODE_JIANGSHAN            = 10,  /* Jiangshan Picture Scroll */
    AJAZZ_MODE_TOP3_BOTTOM3         = 11,  /* Top three, bottom three  */
    AJAZZ_MODE_RAIN_PAVILION        = 12,  /* Rain Falling Pavilion    */
    AJAZZ_MODE_WINDMILL             = 13,  /* windmill                 */
    AJAZZ_MODE_SINE_WAVE            = 14,  /* Sine wave                */
    AJAZZ_MODE_GALLOPING_HORSE      = 15,  /* Galloping horse          */
    AJAZZ_MODE_MUSIC_RHYTHM         = 16,  /* Music rhythm             */
    AJAZZ_MODE_SCREEN_RHYTHM        = 17,  /* Screen light rhythm      */
    AJAZZ_MODE_CUSTOM               = 18   /* Custom Mode              */
} ajazz_mode_t;
```

Verified by direct correlation: Kaleidoscope → `0x05`, Static constant light → `0x06`,
Overall breathing → `0x07`. ✅

### Colour palette (`[10]`) — 8 preset slots

```c
typedef enum {
    AJAZZ_COLOR_RED     = 0,
    AJAZZ_COLOR_GREEN   = 1,
    AJAZZ_COLOR_BLUE    = 2,
    AJAZZ_COLOR_YELLOW  = 3,
    AJAZZ_COLOR_MAGENTA = 4,
    AJAZZ_COLOR_CYAN    = 5,
    AJAZZ_COLOR_WHITE   = 6,
    AJAZZ_COLOR_CUSTOM  = 7    /* rainbow swatch -> per-key table / Custom Mode */
} ajazz_color_t;
```

Each mode remembers its own colour; switching away and back restores it. ✅

---

## 4. Per-key RGB table (`0xF6`–`0xFB`)

Custom Mode does **not** carry colours in the `0x07` frame. It uploads a full
per-key RGB table as six sequential HID reports, then commits with `0x0A`.

The single exception is *entering* the mode: sending `0x07` with
`mode = 18` switches the board to the per-key display, after which the table is
what you see. The vendor UI never emits that frame itself (it is already in the
mode when the Custom tab is open), so this was the one part of the sequence that
had to be established from the host side — see ✅ below.

### Structure

| Property | Value |
|---|---|
| Reports | 6, opcodes `0xF6`, `0xF7`, `0xF8`, `0xF9`, `0xFA`, `0xFB` in that order |
| Data per report | buffer bytes `[6..64]` = **59 bytes** |
| Total table | 6 × 59 = **354 bytes** |
| Key slots | 354 ÷ 3 = **118 keys**, 3 bytes each, 8-bit R,G,B |
| Commit | `0x0A`, empty payload |

The table is a **flat byte stream split across the six reports**; triple boundaries
straddle report boundaries because 59 is not a multiple of 3.

```
table_byte(T)  ->  report opcode  0xF6 + (T / 59)
                   buffer byte    6 + (T % 59)

key(i)         ->  R = T[3i]
                   G = T[3i + 1]
                   B = T[3i + 2]        i = 0 .. 117
```

### Verification

Setting **Esc** to pure green (`00 FF 00`) changed exactly **one** byte in the whole
354-byte table: buffer byte **52**, i.e. the **G** channel, with bytes 51 and 53 left at
`0x00`.

Cross-check against the formula: key `15` → `T[45..47]` → `45/59 = 0` → report `0xF6`,
buffer bytes `6+45 = 51`, `52`, `53`. Exactly what was observed. ✅

The fact that every observed edit lands precisely on a 3-byte boundary and only ever
touches one channel is the strongest structural evidence for a 3-byte stride.

### Confirmed key indices

The table is indexed by **LED chain order, not visual keyboard order** — indices are
non-monotonic when read left-to-right across the keyboard.

| Key index | Table bytes | Buffer location | Colour observed |
|---|---|---|---|
| 14 | 42–44 | `0xF6`, bytes 48–50 | blue `00 00 FF` (cleared on reassignment) ✅ |
| 15 | 45–47 | `0xF6`, bytes 51–53 | green `00 FF 00` (set by us) ✅ |
| 12–13 | 36–41 | `0xF6`, bytes 42–47 | magenta `FF 00 FF` (F-row neighbours) ⚠️ |

Other non-zero slots present in the factory Custom-Mode profile (unmapped to keys):
`60`, `63`, `64`, `65`, `66`, `84`, `85`, `107`, `108`, `109`, `110`.

Example of a decoded slice of the pushed table:

```
idx  13 : FF 00 FF   magenta
idx  15 : 00 FF 00   green     (Esc)
idx  60 : FF FF FF   white
idx  63 : FF 00 00   red
idx  84 : 00 00 FF   blue
idx  85 : FF FF 00   yellow
idx 107 : 00 FF FF   cyan
idx 108 : FF FF FF   white
idx 109 : FF FF FF   white
idx 110 : FF 00 00   red
```

⚠️ A full 118-entry key→index map was **not** enumerated. Each assignment requires
several scripted UI steps (select key → recolour → move selection → commit) and the
widget's commit semantics are unreliable under synthetic input. The mapping above is
sufficient to locate and rewrite any given key once its index is known.

### Known UI quirk worth reproducing in a tool

Selecting a key **loads that key's current colour into the picker**. Setting a colour
before selecting a key therefore gets overwritten. Correct order:

1. Select the target key first.
2. Set R/G/B (preset swatch, colour wheel, or the R/G/B sliders).
3. Move the selection to another key (or press **Save**) to commit.
4. The table is re-pushed on the next key selection.

### Host implementation

The Python port implements this layer in `ajazz_hid.py`:

| Function | Role |
|---|---|
| `pack_key_table()` / `parse_key_table()` | 118 `(r,g,b)` tuples ↔ 354-byte stream |
| `build_keymap_chunks()` | the six reports, split on the flat stream (never on key boundaries) |
| `build_commit_packet()` | the `0x0A` commit frame |
| `upload_keymap()` | chunks + commit in one call |
| `matrix_mapper.py` | interactive index → key-label mapper, writes `keymap_matrix.json` |

`tests/test_ajazz_hid.py` asserts the byte formula above, the Esc = index 15
offset (buffer `51..53` on `0xF6`), and the straddle case where key 19's B channel
lands at byte 6 of the `0xF7` report.

Two practical notes:

* A **1 ms pause** is inserted between reports. The vendor app writes back-to-back
  and that works, but a small gap is cheap insurance against firmware drops.
* The uploader sends a `0x07` frame with `mode = 18` before the table, so a board
  that was showing a built-in effect switches to the per-key display. ✅ Verified
  on hardware: with a board previously in Static mode, the frame was accepted and
  the following table became visible immediately.

### End-to-end verification ✅

The complete sequence was reproduced from a host program with no vendor code:

1. `0x07` with `mode = 18` → board switches to Custom Mode.
2. Six `0xF6`–`0xFB` reports carrying a 354-byte hue sweep (all 118 slots).
3. `0x0A` commit.

On hardware this lit the whole board as a smooth rainbow gradient in LED-chain
order, **sweeping from Esc to Del across the layout**. That single test confirms
the report split, the 59-byte payload boundaries (including triples that straddle
reports), the commit frame, and the mode-entry frame together.

### Host-driven animation

Because every frame is a full table, animation cost scales with frame rate:

| Frames/s | Reports/s (6 chunks + commit each) | Measured |
|---|---|---|
| 60 | 420 | **59.8 fps sustained over 3 s**, no write errors |
| 30 | 210 | 29.9 fps sustained over 4 s |
| 20 | 140 | 20.0 fps sustained |
| 15 | 105 | 15.0 fps sustained |

`animation_engine.py` renders on the host and pushes each frame with
`send_keymap()`. A separate thread injects `0x0E` every 5 s; a lock serialises the
two writers so a heartbeat can never interleave with a chunk. Frame deadlines are
not accumulated, so if a write stalls the engine drops the backlog rather than
bursting to catch up.

---

## 5. C definitions

```c
#pragma pack(push, 1)

/* Lighting configuration frame, opcode 0x07 */
typedef struct {
    uint8_t report_id;     /* [0]      0x00                                  */
    uint8_t magic[4];      /* [1..4]   AA 55 CC 33                           */
    uint8_t command;       /* [5]      0x07                                  */
    uint8_t mode;          /* [6]      ajazz_mode_t                          */
    uint8_t brightness;    /* [7]      level - 1, 0x00..0x04                 */
    uint8_t speed;         /* [8]      level - 1, 0x00..0x04                 */
    uint8_t variant;       /* [9]      0x00, or 0x01 for Kaleidoscope        */
    uint8_t color;         /* [10]     ajazz_color_t                         */
    uint8_t padding[54];   /* [11..64] zero                                  */
} ajazz_light_report_t;    /* sizeof == 65 */

/* One frame of the per-key table upload, opcodes 0xF6..0xFB */
typedef struct {
    uint8_t report_id;     /* [0]      0x00                                  */
    uint8_t magic[4];      /* [1..4]   AA 55 CC 33                           */
    uint8_t command;       /* [5]      0xF6 + chunk, chunk = 0..5            */
    uint8_t data[59];      /* [6..64]  flat RGB table slice                  */
} ajazz_keymap_report_t;   /* sizeof == 65 */

typedef struct {
    uint8_t r, g, b;
} ajazz_rgb_t;

#define AJAZZ_KEY_COUNT   118
#define AJAZZ_KEY_CHUNKS  6
#define AJAZZ_CHUNK_LEN   59

static inline uint8_t ajazz_chunk_opcode(size_t table_byte) {
    return (uint8_t)(0xF6 + table_byte / AJAZZ_CHUNK_LEN);
}
static inline uint8_t ajazz_chunk_offset(size_t table_byte) {
    return (uint8_t)(6 + table_byte % AJAZZ_CHUNK_LEN);
}

#pragma pack(pop)
```

### Building a lighting frame

```c
static void build_lighting(ajazz_light_report_t *p, uint8_t mode,
                           uint8_t brightness_level, uint8_t speed_level,
                           uint8_t variant, uint8_t color)
{
    memset(p, 0, sizeof *p);
    p->report_id  = 0x00;
    p->magic[0] = 0xAA; p->magic[1] = 0x55;
    p->magic[2] = 0xCC; p->magic[3] = 0x33;
    p->command    = 0x07;
    p->mode       = mode;
    p->brightness = brightness_level - 1;   /* UI 1..5 -> 0x00..0x04 */
    p->speed      = speed_level - 1;
    p->variant    = variant;
    p->color      = color;
}
/* WriteFile(hid, p, 65, &written, NULL); */
```

### Uploading a per-key table

```c
static BOOL upload_keymap(HANDLE hid, const ajazz_rgb_t keys[AJAZZ_KEY_COUNT])
{
    const uint8_t *flat = (const uint8_t *)keys;   /* 354 bytes */
    DWORD written;

    for (int chunk = 0; chunk < AJAZZ_KEY_CHUNKS; chunk++) {
        ajazz_keymap_report_t rep;
        memset(&rep, 0, sizeof rep);
        rep.magic[0] = 0xAA; rep.magic[1] = 0x55;
        rep.magic[2] = 0xCC; rep.magic[3] = 0x33;
        rep.command  = (uint8_t)(0xF6 + chunk);
        memcpy(rep.data, flat + chunk * AJAZZ_CHUNK_LEN, AJAZZ_CHUNK_LEN);

        if (!WriteFile(hid, &rep, sizeof rep, &written, NULL)) return FALSE;
    }

    ajazz_keymap_report_t commit;                  /* opcode 0x0A, empty */
    memset(&commit, 0, sizeof commit);
    commit.magic[0] = 0xAA; commit.magic[1] = 0x55;
    commit.magic[2] = 0xCC; commit.magic[3] = 0x33;
    commit.command  = 0x0A;
    return WriteFile(hid, &commit, sizeof commit, &written, NULL);
}
```

> The table must be sent in chunk order `0xF6 → 0xFB` with no other lighting frame
> interleaved, immediately followed by the `0x0A` commit.

---

## 6. Raw evidence samples

Lighting configuration frames (`0x07`), bytes `[5..10]`:

```
07 06 04 03 00 05    Static, brightness 5, speed 4, cyan
07 06 00 03 00 05    Static, brightness 1, speed 4, cyan
07 06 04 03 00 00    Static, brightness 5, red
07 06 04 03 00 01    Static, brightness 5, green
07 06 04 03 00 02    Static, brightness 5, blue
07 06 04 03 00 03    Static, brightness 5, yellow
07 06 04 03 00 04    Static, brightness 5, magenta
07 06 04 03 00 05    Static, brightness 5, cyan
07 06 04 03 00 07    Static, brightness 5, custom
07 05 04 03 01 06    Kaleidoscope (mode 5), variant 1
07 07 04 03 00 06    Overall breathing (mode 7)
07 07 04 00 00 06    Overall breathing, speed 1
```

Keep-alive, every ~5.02 s:

```
00 AA 55 CC 33 0E 00 00 00 00 00 ... (all remaining bytes 0x00)
```

Per-key table burst, showing the Esc edit (green at `0xF6` bytes 51–53):

```
F6: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
                                    ^^^^^ bytes 51,52,53 = 00 FF 00
```

---

## 7. Methodology / what was ruled out

* **Instrumentation:** Frida attach to a running `DeviceDriver.exe`, inline hooks via
  `Process.getModuleByName(...).getExportByName(...)` + `Interceptor.attach`.
  `Module.findExportByName` and the `hexdump` runtime helper are unavailable in this
  target's Frida environment.
* **Hook coverage:** `WriteFile`, `DeviceIoControl`, `HidD_SetFeature`.
  Only `WriteFile` produced lighting data. `HidD_SetFeature` is not in the PE import
  table (resolved via `GetProcAddress`) but was hooked successfully at runtime by
  symbol name and never fired for lighting.
* **Filtering:** 65-byte writes identified by `nNumberOfBytesToWrite == 65` and
  `buf[0] == 0x00`. Repeated 512 / 4096 / 4-byte writes are unrelated file I/O.
* **UI driving:** the window is a single owner-drawn `XYClient` surface — the UIA tree
  contains one `Pane` and there are **no child controls**, so accessibility invocation
  is impossible. `PostMessage` clicks are ignored (the app hit-tests the real cursor
  position); real `SendInput` at `TOPMOST` z-order was required.
  Clicks must be computed from **client** coordinates via `ClientToScreen`, because the
  window can be repositioned by the app.

---

## 8. Open questions

1. **Full key→index map.** 118 slots exist; only a handful are mapped. The order is
   LED-chain order, so it cannot be inferred from the visual layout.
2. **`variant` (`[9]`)** semantics beyond Kaleidoscope = `0x01`.
3. **Palette→RGB definition.** The eight preset colours are resolved by firmware;
   their exact values (e.g. whether "red" is `FF 00 00` or `FF 20 20`) are unknown and
   may be adjustable on-device.
4. **`AJAZZ_COLOR_CUSTOM` (`0x07`) + Custom Mode interaction** — whether the `0x07`
   frame's palette index is ignored once a per-key table has been uploaded.
5. **Whether 118 is the true physical LED count** for this 75 % layout, or a fixed
   allocation larger than the populated LEDs.
