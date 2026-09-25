# UI package resources

`web.ui.index_html()` remains the compatibility facade.  It delegates to this
package, which reads fixed UTF-8 resources via `importlib.resources` and emits
the same single inline HTML document that the legacy endpoints already serve.

For an existing UI change:

- Change the text or structure of **История** in `templates/tabs/history.html`.
  It does not require Python, HTTP, auth, or runtime changes.
- Change the appearance of an existing button in `styles/app.css`.  Preserve
  cascade order; it does not require runtime or data changes.

`scripts/legacy-runtime.js` is intentionally still one byte-preserved legacy
runtime.  A7 will own request/session boundaries, A8 will own accepted-run and
realtime state, and A9 will split feature controllers.  Do not introduce a
generic state bus or placeholder modules in this resource-only checkpoint.
