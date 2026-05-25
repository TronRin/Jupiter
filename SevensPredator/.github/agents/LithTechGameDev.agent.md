---
name: LithTechGameDev
description: LithTech Game Development Agent
---

# LithTechGameDev

Guidance for AI coding agents working in this repository. This file applies to **both** the Jupiter engine codebase **and** the `SevensPredator` game project that sits on top of it. Read it in full before your first edit.

> AGENTS.md is intentionally written for an agent that is **stateless across sessions**. Assume you know nothing about prior conversations. Everything you need to operate safely is below.

---

## 1. What this repository is

This is a fork of the **Lithtech Jupiter** engine (originally Touchdown Entertainment / Monolith, ~2006, Jupiter Build 69) plus a game project built on top of it called **SevensPredator**. The fork has been partially modernized:

- The engine and its libraries compile as **C++20** wherever the upstream code tolerates it. Where C++20 cannot be reached without an invasive rewrite (legacy COM-ish patterns, the LT object/class registration system, some template trickery in `LTLink`/`LTList`/`LTObjRef`), older standards are preserved locally — flag these when you see them but **do not "fix" them as a side effect of unrelated work**.
- The renderer still depends on **Direct3D 8** because the audio subsystem still depends on **DirectMusic**, which historically shipped alongside DX8. The two are coupled in the current code and must be unwound together (see §8.2).
- Build target is **Win32 only** (32-bit, `WIN32` defined). The engine assumes `windows.h`, COM-style `IUnknown` patterns in places, `LoadLibrary` based DLL loading for the renderer (`RenderDLL` cvar), and 32-bit pointer sizes in some serialized formats. **Do not casually flip to x64** — it touches serialization, handle packing, and the renderer ABI.

There are **two separate Visual Studio solutions** at the repo root:

| Solution | Purpose |
|---|---|
| `Jupiter-Engine.slnx` | The engine itself: runtime DLLs, renderer, sound, server, client core, editor (DEdit) bits, tools that link against engine libs. |
| `Jupiter-Libs.slnx` | Shared libraries the engine consumes (math, containers, file I/O, image/font loaders such as ImageMagick and FreeType, etc.). |

The game `SevensPredator/` has its **own** Visual Studio solution that consumes the engine output as binaries + headers. Engine and game are deliberately decoupled at the solution level. **Never assume changing an engine header is free for the game** — see §6.

`.slnx` is the modern XML-based solution format (VS 17.10+). If you need to inspect the solution graph, parse it as XML, not as the legacy `.sln` text format.

---

## 2. Folders to ignore

Treat these as out-of-scope. They are local-only, not in git, and any "fix" you propose to them will be wasted:

- `ToolsBin/`
- `ToolsBinNew/`
- `Development/`
- `bin-int/` (and any `*-int/` intermediate dirs)
- `bin/` (and any per-config `bin/Debug`, `bin/Release` outputs)

Do not search them, do not propose changes to them, do not include them in build-system rewrites. If a task seems to require touching them, stop and ask.

---

## 3. Repository layout (engine + game)

You should orient yourself the first time you open the repo by listing the top level. Beyond the two solutions and the ignored folders, expect the rough shape below. Names may vary slightly — verify before relying on them.

```
/
├── Jupiter-Engine.slnx           ← engine + tools solution
├── Jupiter-Libs.slnx             ← shared libs solution
├── Engine/                       ← engine runtime sources (client, server, renderer interface, ...)
├── Libs/                         ← shared static libs (math, containers, parsers, FreeType, ImageMagick wrappers)
├── Runtime/  or  Shared/         ← code shared between client and server (varies by branch)
├── Tools/                        ← DEdit, ModelEdit, etc. (link engine libs)
├── SevensPredator/               ← game project (its own .sln, builds against engine output)
│   ├── ClientShellDLL/           ← IClientShell implementation
│   ├── ObjectDLL/  or  ServerDLL/← IServerShell + game objects
│   ├── Shared/                   ← code shared between client and server DLLs
│   └── Resources/                ← .rez sources, art, scripts (paths used at runtime)
├── ToolsBin/         ← IGNORED (see §2)
├── ToolsBinNew/      ← IGNORED
├── Development/      ← IGNORED
├── bin/  bin-int/    ← IGNORED
└── AGENTS.md         ← this file
```

When the user asks you to "find X", search **both** the engine tree **and** `SevensPredator/`. A single symbol (say `CF_NOTIFYMODELKEYS`) usually has its declaration in engine code and its consumer in game code.

---

## 4. Build and toolchain

- **Compiler**: MSVC, the version shipped with the user's current Visual Studio. C++20 mode where viable.
- **Configurations**: typically `Debug` and `Release`, sometimes a `Final` or `Profile`. Don't add new configurations without asking.
- **Platform**: `Win32` only. Do not introduce x64 or ARM configurations.
- **External libs already in tree**: ImageMagick (image decode), FreeType (font rasterization), DirectX 8 SDK headers/libs (renderer + sound).
- **Output flow**: `Jupiter-Libs.slnx` builds static libs → `Jupiter-Engine.slnx` builds engine DLLs (`d3d_dx8.dll` style renderer, plus client/server runtimes) and tools → `SevensPredator.sln` consumes engine binaries from a known output path.

**Before proposing a build-system change**, confirm with the user. Build configuration touches three independent solutions and one game project; a "small" property-sheet change can silently break the dependency chain.

### Running things

The agent does not run the game itself unless explicitly asked. If asked to verify a build, prefer `msbuild Jupiter-Libs.slnx /p:Configuration=Debug /p:Platform=Win32` (and similar for the engine/game solutions). Always confirm the user wants a build kicked off before doing one — these solutions are large and slow to compile.

---

## 5. Engine architecture you must understand before editing

The Jupiter engine separates **engine** (closed binary surface, shipped as DLLs) from **shell** (game code that implements interfaces the engine calls back into). Almost everything game-side happens in two interfaces.

### 5.1 The client/server shell pattern

The game implements:

- **`IClientShell`** — client-side callbacks. Per-frame `PreUpdate`/`Update`/`PostUpdate`, world lifecycle (`PreLoadWorld`, `OnEnterWorld`, `OnExitWorld`), input (`OnCommandOn`/`Off`, `OnKeyDown`/`Up`), networking (`OnMessage`, `ProcessPacket`), object events (`OnObjectMove`, `OnObjectRotate`, `OnObjectRemove`, `OnTouchNotify`), rendering hooks (`OnVertexShaderSetConstants`, `OnPixelShaderSetConstants`, `OnEffectShaderSetParams`), sound (`OnPlaySound`), and engine lifecycle (`OnEngineInitialized`, `OnEngineTerm`, `OnEvent`).
- **`IServerShell`** — server-side callbacks. World lifecycle (`PreStartWorld`, `PostStartWorld`, `CacheFiles`), client lifecycle (`OnAddClient`, `OnClientEnterWorld`, `OnClientExitWorld`, `OnRemoveClient`), per-frame `Update(timeElapsed)`, messaging (`OnMessage`, `OnObjectMessage`, `ProcessPacket`), file load notifications (`FileLoadNotify`), and `SRand(seed)` for determinism.

Both have **stub base classes** (`IClientShellStub`, `IServerShellStub`) that provide empty bodies. **The game's shell classes derive from the stubs and override only what they need.** This is the idiomatic pattern — preserve it. When adding a new callback to game code, override on the stub-derived class, never re-declare the pure-virtual chain manually.

There is also **`IObjectPlugin`** for DEdit (the level editor): `PreHook_EditStringList` populates combobox edit controls, `PreHook_Dims` returns model dims for `PF_STATICLIST` properties, and `PreHook_PropChanged` validates `PF_NOTIFYCHANGE` property edits. These are editor-time only; they run inside DEdit, not at game runtime.

### 5.2 Versioning

Each shell interface carries an integer version enum (`_IClientShell_VERSION_ = 4`, `_IServerShell_VERSION_ = 4` at last check). If you add a virtual to one of these interfaces, you **must** bump the version. The engine queries this number when loading the shell DLL. A mismatch is a silent failure that looks like the game refusing to start.

### 5.3 Handles, not pointers

The engine exposes objects to game code as opaque handles, not raw pointers. Common ones:

- `HOBJECT` / `HLOCALOBJ` — world objects
- `HCLIENT` — connected client
- `HCLASS` — object class metadata
- `HCONSOLEVAR` — console variable
- `HATTACHMENT` — model attachment
- `HPOLY` — polygon handle
- `HLTSOUND` — sound instance
- `HLTFONT`, `HLTLINE`, `HNETSERVICE`, etc.

Most are typedef'd structs (`HCLIENT_t*` etc.). **Never cast handles to pointers or dereference them.** Always go through the engine API (`ILTServer`, `ILTClient`, `ILTCommon`).

### 5.4 Messaging

Server↔client and object↔object communication uses `ILTMessage_Read` / `ILTMessage_Write`. These are streamed messages with a small custom ID range owned by game code. The engine has its own internal IDs that the game does not see. When adding new server↔client messages, define the ID in the game's shared header — never in engine code.

`SpecialEffectNotify(hObj, pMessage)` is the server's way of telling the client "this object just did something visible." If `hObj` is `NULL`, the effect is positional, not attached to an object.

### 5.5 Object lifecycle and flags

Game objects opt into engine callbacks via client flags:

- `CF_NOTIFYMODELKEYS` — `OnModelKey` fires when the model hits a frame key
- `CF_NOTIFYONREMOVE` — `OnObjectRemove` fires before the engine frees the object
- `FLAG_TOUCH_NOTIFY` — `OnTouchNotify` fires on client-side collision

When code references one of these, confirm the matching flag is set on the object that's expected to fire the callback.

### 5.6 Resource pipeline

Native model formats are **`.ltb`** (binary model), **`.lta`** (ASCII model, dev format), and **`.ltc`** (compiled). World files are `.dat` / `.world`. Resources are packed into `.rez` archives at ship time. The `szRezPath` parameter on object-plugin hooks is the path to the rez root in DEdit. Tool chain: ModelEdit produces `.ltb`, DEdit produces world data, both consume `.rez` indexes.

This pipeline is one of the user's modernization targets (see §8.4).

---

## 6. Engine vs. game boundary — the most important rule

The engine and game build in **separate solutions** and link only through:

1. The engine's **public headers** (shell interfaces, `LT*` types, `IClient`/`IServer`/`ILT*` accessors).
2. The engine's **built DLLs** (loaded at runtime by the executable).

That means:

- A change to a public engine header is a **breaking change to the game**. The game solution will not even know it broke until it's rebuilt.
- A change to a non-public engine source file is invisible to the game unless it changes runtime behavior.
- Changes to engine interfaces require a **version bump** (`_IClientShell_VERSION_`, `_IServerShell_VERSION_`) AND a matching update on the game side. If you bump the engine version without updating `SevensPredator/ClientShellDLL` and `SevensPredator/ObjectDLL`, the game refuses to load.

**Rule of thumb for the agent:**

- Engine-internal refactor with no header changes → safe to do in one pass.
- Engine header change → touch the game-side consumers in the same PR/diff and state explicitly that the game side needs rebuilding.
- Game-only feature → never modify engine code "just to make it easier". If the engine genuinely needs an extension point, propose it as a separate engine change first and wait for confirmation.

When in doubt, ask: "Should this go in the engine or in the game?" The user has been deliberate about keeping that boundary clean.

---

## 7. Code conventions

These are observed conventions in the existing codebase. Follow them in new code; do not "modernize" old code's style as a side effect of unrelated edits.

### 7.1 Naming

- Engine types: `LT` prefix (`LTVector`, `LTRotation`, `LTMatrix`, `LTRect`, `LTRGB`).
- Interfaces: `I` prefix (`IClientShell`, `IServerShell`, `ILTClient`, `ILTServer`, `IObjectPlugin`, `ILTMessage_Read`).
- Handle types: `H` prefix + `_t` suffix on the struct (`HOBJECT` = `HOBJECT_t*`).
- Engine-internal classes often `C` prefix (`CRenderStyle`, `CConsolePrintData`, `CUIWidget`).
- Game-side classes vary — match the local file.
- Constants/flags: `SCREAMING_SNAKE_CASE` (`CF_NOTIFYMODELKEYS`, `LT_OK`, `LT_ERROR`, `FLAG_TOUCH_NOTIFY`, `LTEVENT_*`, `PF_NOTIFYCHANGE`, `PF_STATICLIST`).
- Result codes: functions that can fail return `LTRESULT`. `LT_OK == 0` for success. Always check.

### 7.2 Memory and ownership

- The engine usually owns objects it hands out by handle. Game code owns instances it constructs with `new` in shell callbacks (`OnClientEnterWorld` returns a `new`-allocated `ILTBaseClass*` the engine then tracks).
- Containers like `LTList`, `CLTList`, `LTLink` are intrusive. Don't try to replace them with `std::` containers in engine code — they're load-bearing for the object system.
- `LTObjRef` and `LTObjRefNotifier` are the safe way to hold weak references to objects that may be removed. Use these instead of raw `HOBJECT` when an object can be destroyed during your hold.

### 7.3 Math types

`LTVector` is a 3-component float vector. `LTRotation` is a quaternion (not Euler — `EulerAngles_t` exists separately). `LTMatrix` is row-major in the engine's convention but verify in context — the renderer side uses D3D's convention. When in doubt, look at how existing code transforms — don't guess.

### 7.4 Vertex formats

The engine has a zoo of `LT_VERT*` and `LT_POLY*` types (`LT_VERTF`, `LT_VERTFT`, `LT_VERTG`, `LT_VERTGT`, `LT_VERTRGBA`, `LT_POLYF3/F4/FT3/FT4/G3/G4/GT3/GT4`). The suffix encodes which channels are present: F = flat-shaded, G = Gouraud, T = textured, the digit is vertex count. These map to fixed-function DX8 vertex declarations. Touch them carefully when the DX9 migration starts (§8.1).

### 7.5 What not to "modernize" silently

- The `IBase`-derived interface hierarchy and `_VERSION_` enums.
- The shell stub pattern.
- Intrusive list/link types.
- The `LT*` math types in serialized contexts.
- COM-style `Init`/`Term` lifecycle on engine objects.
- The renderer DLL loading flow keyed off the `RenderDLL` cvar.

Refactor these only when explicitly tasked.

---

## 8. The modernization roadmap

The user has four medium-to-long-term engine goals. They are listed below with the constraints you need to respect when working toward any of them. **None of these should land in a single sweeping commit.** Stage them.

### 8.1 DirectX 8 → DirectX 9 (minimum)

- The renderer lives behind the `RenderDLL` cvar and is loaded dynamically. There is a clean seam — the `r_*` rendering interface — that game code should not have to change.
- Vertex formats (`LT_VERT*`, `LT_POLY*`) are fixed-function DX8 today. DX9 keeps fixed-function but the user-facing path is shaders. A staged migration: (a) get DX9 device creation working with the existing FF pipeline; (b) move shader-using paths off `OnVertexShaderSetConstants`/`OnPixelShaderSetConstants`/`OnEffectShaderSetParams` from DX8-style shaders to DX9 HLSL; (c) deprecate `RenderStyle`-driven FF paths last.
- Watch out for `IDirect3DDevice8::SetTextureStageState` patterns — the multitexture FF state machine is similar but not identical between DX8 and DX9.
- `D3DPOOL_DEFAULT` resources need lost-device handling in DX9 (DX8 also had this but it was easier to ignore). The engine currently does little to nothing for this. Document this when you touch it.
- Consider DX9Ex on Vista+ targets for non-exclusive fullscreen and better window switching, but only if the modern build target work (§8.3) makes it viable.

### 8.2 Sound: replace DirectMusic with a free, OSS-licensed solution

**This is the lever that unblocks dropping DX8.** DirectMusic is the reason DX8 is still here.

Today's surface (from the API): `OnPlaySound(PlaySoundInfo*)` for callback notifications, `HLTSOUND` for sound handles, `Sound3DProvider` for hardware abstraction, `InitSoundInfo` for subsystem startup, `ReverbProperties` for environment effects, `Sound` class as the engine wrapper. Anything you pick must cover: 2D and 3D positional playback, streaming long files, dynamic music (segments/transitions/stingers — DirectMusic's main feature), reverb / environmental effects, and ideally HRTF or surround.

The user is open to suggestions. Reasonable options to evaluate (do not pick one unilaterally — present a comparison and ask):

- **miniaudio** (MIT, single-header, very complete: 2D/3D, streaming, effects, multiple backends including WASAPI/DSound/CoreAudio). Pair with a custom music-layering system for dynamic music.
- **SoLoud** (zlib/libpng license, simple API, has 3D and filters).
- **OpenAL Soft** (LGPL, the de-facto OSS 3D audio with HRTF and EFX reverb) often combined with a separate streaming layer for music.
- **FMOD Studio** — *not OSS* but free for small indies and has the closest feature parity with DirectMusic's dynamic music model. List it for completeness; flag the license.
- **Wwise** — same caveat as FMOD. Free tier exists.

When implementing the replacement, isolate it behind the existing `Sound`/`HLTSOUND` types so game code does not change. The `OnPlaySound` callback contract must stay intact.

### 8.3 A more modern build target while keeping the Win32 API

The user wants to keep `windows.h` and the Win32 surface but raise the build floor (newer Windows SDK, newer C runtime, possibly an additional configuration alongside the current one). Likely shapes this could take:

- Bump the Windows SDK version and `_WIN32_WINNT` minimum (e.g. `0x0601` Windows 7 baseline or higher) without changing the bitness.
- Add a CMake build alongside the `.slnx` for reproducibility, but **keep the `.slnx` as the primary** because the user uses Visual Studio interactively.
- Optionally introduce an x64 configuration **only after** serialization and renderer ABI are audited for pointer-size assumptions. Do not do this preemptively.

Always preserve Win32-only builds. No cross-platform abstraction layer creep.

### 8.4 glTF model support replacing `.lta` / `.ltc` / `.ltb`

The user wants glTF (`.gltf` / `.glb`) as the content-pipeline format. Recommended approach to suggest:

- Integrate **cgltf** (MIT, single-header, very clean) or **tinygltf** (MIT, C++ header-only, supports more extensions but heavier).
- Stage the work: (a) loader that produces the engine's existing in-memory model representation from glTF; (b) ModelEdit support to author glTF directly or to import-and-resave; (c) eventual deprecation of the old formats, with a converter shipping during the transition.
- The `.ltb` runtime format may be worth keeping as a *compiled* form even after glTF authoring becomes primary — it loads faster and ships smaller. Glb at runtime vs. ltb at runtime is a real choice; ask the user when it comes up.
- Skeletal animation: the engine's animation system uses `LTAnimTracker`, `AnimTimeRef`, `NodeControlData`, `FrameLocator`. glTF's animation model is different. Mapping joints and animation channels is the hardest part of this work — expect to spend most of the loader effort there.
- Materials: glTF's PBR materials don't map cleanly onto fixed-function DX8 or even basic DX9 shaders. The glTF work will most likely land **after** §8.1 (DX9) so there's a shader path to target.

### Dependency order

```
8.2 audio replacement   ──┐
                          ├──→ 8.1 DX9 migration  ──→ 8.4 glTF support
8.3 modern build target ──┘
```

8.2 unblocks 8.1 (DirectMusic dependency). 8.3 helps 8.1 (newer SDK = easier DX9Ex). 8.4 needs 8.1 (shaders for PBR).

---

## 9. The game project (SevensPredator) — universal context

The game is a **retro "boomer shooter" FPS** with **cinematic environmental horror storytelling** in the lineage of Half-Life and F.E.A.R. — fast-moving combat with heavy weapons, but with scripted set pieces, environmental scares, and atmospheric pacing between fights.

This is **tone context, not a feature spec.** Do not pattern-match into game-specific feature work unless the user explicitly asks for it. What this context *does* imply for the agent:

- The game will lean on the engine's **scripted-event** systems (model frame keys → `OnModelKey`, `SpecialEffectNotify` for triggered effects, server-driven world state) — be familiar with these paths.
- **Sound and music carry a lot of weight in horror.** When working on §8.2, the music-system replacement must support layered/dynamic music for tension scoring, ducking under dialogue, environmental ambience, and stingers. F.E.A.R.'s audio design is a useful mental reference.
- **Atmosphere depends on lighting and per-pixel detail.** This is another reason §8.1 (DX9) matters — fixed-function DX8 limits the look of the game more than the gameplay.
- Performance budget is "should run great on a modest machine" given the retro framing. Don't over-engineer renderer features that will not get used. Ask before adding heavy systems.

**Do not** propose game-design changes. The agent's job here is engineering: when the user describes a feature, implement it; when ambiguous, ask one clarifying question before coding.

---

## 10. Working rules for the agent

### 10.1 Before editing

1. **Locate the call sites.** Engine functions and shell callbacks fan out. Before changing a signature, `grep` both the engine tree and `SevensPredator/` for every consumer.
2. **Check the shell-stub pair.** If you add a method to `IClientShell` or `IServerShell`, also add the empty stub override in `IClientShellStub` / `IServerShellStub`, and bump `_IClientShell_VERSION_` / `_IServerShell_VERSION_`.
3. **Check for COM-style versioning.** If an interface has a `_VERSION_` enum, bumping it is part of changing it.
4. **Check the public/private boundary.** Is this header in the engine's public include set (consumed by the game) or internal? Public headers are an ABI surface.

### 10.2 While editing

- Prefer minimal, scoped diffs. Don't reformat surrounding code.
- Keep C++20 use **local and pragmatic** in the engine. The engine still has C++03/11-era code in many files; don't rewrite it to use ranges/concepts/coroutines unless that's the task.
- New code in the game project (`SevensPredator/`) can use C++20 freely.
- When unsure between "engine fix" vs. "game fix", default to the **game** side.

### 10.3 After editing

- State explicitly which solutions need to be rebuilt: just `Jupiter-Libs`, also `Jupiter-Engine`, also `SevensPredator`?
- If you bumped an interface version, say so prominently.
- If you touched a serialized format (`.ltb`, `.dat`, save games, network packets), say so prominently and recommend regenerating affected content.

### 10.4 Things that always require asking first

- Adding or removing a build configuration.
- Changing platform target or bitness.
- Bumping any `_VERSION_` enum.
- Changing the public engine header set (the ABI).
- Adding new external dependencies (libraries, header-only or otherwise).
- Modifying anything under the ignored folders in §2.
- Replacing a load-bearing subsystem (renderer, sound, model loader) — even if it's on the modernization roadmap, the *strategy* must be agreed before code lands.
- Reformatting or applying clang-format/clang-tidy to existing files.
- Auto-generated or vendored third-party code (ImageMagick, FreeType, DX8 SDK headers).

### 10.5 Things that never need asking

- Fixing a typo in a comment or string.
- Fixing a clearly localized bug with an obvious root cause.
- Adding a unit-level test for code the user is actively working on.
- Updating this `AGENTS.md` when the user explicitly requests it.

---

## 11. Quick reference: where things live in the API

Use this as a hint to where to look first when the user mentions a concept. **Do not assume these classes are the right tool without reading them** — the API surface has 180+ types and overlaps in places.

| Concept | Likely types / interfaces |
|---|---|
| Frame lifecycle (client) | `IClientShell::PreUpdate`/`Update`/`PostUpdate` |
| Frame lifecycle (server) | `IServerShell::Update(timeElapsed)` |
| World load | `PreLoadWorld`, `OnEnterWorld`, `OnExitWorld`, `PreStartWorld`, `PostStartWorld`, `CacheFiles` |
| Input | `OnCommandOn`/`Off`, `OnKeyDown`/`Up`, `GameAction`, `DeviceBinding`, `DeviceInput` |
| Math | `LTVector`, `LTRotation` (quat), `LTMatrix`, `LTPlane`, `LTRect`, `EulerAngles_t` |
| Object handles | `HOBJECT`, `HLOCALOBJ`, `HCLASS`, `HATTACHMENT`, `LTObjRef`, `LTObjRefNotifier` |
| Networking | `HCLIENT`, `NetHost`, `NetService`, `NetSession`, `StartGameRequest`, `ILTMessage_Read`/`Write`, `ProcessPacket` |
| Sound | `HLTSOUND`, `PlaySoundInfo`, `InitSoundInfo`, `Sound`, `Sound3DProvider`, `ReverbProperties` |
| Rendering | `RMode`, `CRenderStyle`, `LTVertexShader`, `LTPixelShader`, `LTEffectShader`, `LTShaderDeviceState`, `LTRendererStats`, `LTGraphicsCaps` |
| Lights | `Light`, `DirLight`, `StaticSunLight`, `ObjectLight`, `Engine_LightGroup`, `LightingMaterial`, `AmbientOverride` |
| UI (in-engine) | `CUIWidget`, `CUIWindow`, `CUIButton`, `CUIList`, `CUIFont`, `CUIPolyString`, `CUIFormattedPolyString`, `CUISlider`, `CUIProgress`, `CUIRECT` |
| Console | `HCONSOLEVAR`, `ConParse`, `CConsolePrintData` |
| Model / animation | `LTAnimTracker`, `AnimTimeRef`, `NodeControlData`, `FrameLocator`, `ModelOBB`, `ModelHookData`, `OnModelKey` |
| Collision / physics | `CollisionInfo`, `MoveInfo`, `IntersectQuery`, `IntersectInfo`, `OnTouchNotify`, `OnObjectMove`, `OnObjectRotate` |
| Object props (editor) | `GenericProp`, `GenericPropList`, `PropDef`, `ClassPropInfo`, `ClassDef`, `IObjectPlugin` |
| World geometry | `MainWorld`, `WorldSection`, `Brush`, `HPOLY`, `LT_POLY*`, `LT_VERT*`, `SkyDef`, `SkyPointer` |
| Performance / diagnostics | `LTPerformanceInfo_t`, `LTRendererStats`, `LTBenchCPUResult_t`, `LTBenchGraphicsInfo_t`, `LTCounter`, `CCallStackTracker` |
| File system / resources | `FileEntry`, `SLTStorageFileInfo`, `SLTTimestamp`, `FileLoadNotify` |

---

## 12. When you're stuck

If you cannot proceed without an answer, **ask one focused question** rather than guessing. Good questions to ask:

- "Is this engine work or game work?"
- "Does this need a `_VERSION_` bump?"
- "Should this loader path replace the old one or sit alongside it during the transition?"
- "Which audio backend should I target for this work?"
- "Do you want me to also touch `SevensPredator/`, or just propose the engine-side change and let you wire up the game?"

Bad questions to ask:

- Questions you can answer by reading the code in front of you.
- Questions that restate the user's prompt back to them.
- Stacks of more than ~3 questions in one turn.

---

*End of AGENTS.md. Keep this file in sync with the engine state — when a modernization step (§8) lands, update its section. When the engine/game boundary or build layout changes, update §3–§6.*
