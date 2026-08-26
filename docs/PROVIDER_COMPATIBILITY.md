# Provider compatibility and adaptive UI

Provider/model limits are declared by each adapter and consumed by the task service, Blender UI and MCP bridge. The UI is not a second source of truth: hidden controls and server-side validation use the same resolved constraint object.

## Generation input matrix

| Provider/model | Text | Single image | Multi-view | Multi-view slots | Formats |
| --- | --- | --- | --- | --- | --- |
| Tripo CN/Global V3.1/V3.0/V2.5/P1 | Yes | 1 front | 2–4, front required | front, left, back, right | PNG, JPEG, WebP |
| Hunyuan Direct 3.0 | Yes | 1 front | 2–4, front required | front, left, right, back | PNG/JPEG for multi-view; WebP also for single image |
| Hunyuan Direct 3.1 | Yes | 1 front | 2–8, front required | 3.0 slots plus top, bottom, left-front, right-front | PNG/JPEG for multi-view; WebP also for single image |
| TokenHub CN/Global Hunyuan 3.0/3.1 | Yes | Same as corresponding Hunyuan model | Same as corresponding Hunyuan model | Model-dependent | Model-dependent |
| TokenHub CN/Global Tripo 3.1/P1 | Yes | Hidden | Hidden | — | — |
| Compare | Yes | 1 front | Intersection of both selected adapters: 2–4 | front, left, back, right | Intersection of both adapters |

Hunyuan multi-view input enforces dimensions greater than 128 and less than 5000 pixels and a 6 MB raw-file total. Tripo requires at least 128 pixels per side. Tencent's current public TokenHub Tripo guide describes text and image capability but publishes only the production text request body; the plugin therefore exposes text only for those two TokenHub models instead of guessing a billable image payload. Job creation rejects stale or unsupported inputs before queueing or provider billing.

Official references:

- [Tripo multi-view generation](https://developers.tripo3d.ai/en/docs/generation-multiview-to-model/standard)
- [Tencent Hunyuan professional generation](https://cloud.tencent.com/document/product/1804/123447)
- [Tencent TokenHub Hunyuan API](https://cloud.tencent.com/document/product/1823/130082)
- [Tencent TokenHub Tripo API](https://cloud.tencent.com/document/product/1823/136143)

## Blender behavior

- The generation method is selected first. Blender then hides incompatible providers; model changes recalculate view buttons and limits.
- Dynamic provider, account, job, candidate and operation enums use stable numeric ids, so filtering never changes the selected value by list position.
- Switching to a narrower model removes incompatible UI references from the pending form; managed source files are not silently deleted.
- Image format, dimensions, count and aggregate size are checked again when the job is created.
- Tripo V2.5 hides V3-only geometry, parts and quality controls.
- Tripo single-image mode alone shows image autofix, texture alignment and orientation; these fields are rejected for text/multi-view requests.
- Tripo rig v1.0 accepts only biped; rig v2.5 accepts non-humanoid rig types. Unsupported combinations never reach billing.
- Hunyuan 3.1 hides LowPoly and Sketch generation types; TokenHub Tripo hides all Hunyuan-only fields.
- Candidate post-process menus contain only operations supported by a configured provider and compatible with the candidate source/format.
- Animation appears only for an actual provider rig-result candidate.

## MCP behavior

Call `get_generation_constraints` before composing an image job and `get_process_capabilities` before post-processing a candidate. `create_asset_job` and process submission independently repeat validation, so a client cannot bypass Blender's hidden/disabled controls by sending raw JSON.

No bridge method returns a provider credential, provider download URL or arbitrary local path.

## Credential resolution

The account manager supports multiple profiles for each explicit supplier: Tripo CN, Tripo Global, Hunyuan Direct, TokenHub China and TokenHub Global. New accounts are saved to the native OS credential store by default; if native storage is unavailable, Blender clearly falls back to session memory. The manager lists accounts for the selected supplier and provides explicit activate and delete actions. The local profile registry contains only opaque random ids, provider names and notes. A job binds its selected opaque id so later UI changes cannot switch credentials during asynchronous work.

The Blender process may also resolve only fixed allowlisted variables: `TRIPO_API_KEY`, `TRIPO_CN_API_KEY`, `TRIPO_GLOBAL_API_KEY`, `HUNYUAN_3D_API_KEY`, `HUNYUAN_DIRECT_API_KEY`, `TOKENHUB_API_KEY`, `TOKENHUB_CN_API_KEY`, `TOKENHUB_INTL_API_KEY` and `TOKENHUB_GLOBAL_API_KEY`. An explicitly selected profile wins, followed by session profiles, saved profiles and the environment profile. MCP does not expose profile ids, notes, sources or environment variable names; public status remains provider/configured/available booleans. For better isolation, inject variables only into the Blender launch process rather than the whole desktop session.
