# UI package resources

`web.ui.index_html()` remains the compatibility facade. Fixed UTF-8 package
resources render one inline document without a build tool or module loader.
Templates own markup; `styles/app.css` owns the existing cascade.

`app-shell.js` constructs and connects dependencies, routes DOM events and tabs,
and owns bootstrap/auth teardown. `api-client`, `session-controller`, `run-state`
and `realtime-controller` retain the accepted A7–A8 request/session/run owners.
Subject controllers own releases, backups, settings, history, terminal, finder,
presets/v2fly, candidates and status views. Each receives a finite state view and
explicit dependencies. `candidate-selection` owns pure selection calculations.
`ui-lifetime` owns subject request cancellation, channel revisions and scheduled
work. Both internal event handlers and exported actions use the same stale
response guard; draft edits invalidate related requests. Teardown releases
listeners, timers and streams. `legacy-runtime.js` is only a compatibility entry.

The resource order is explicit in `SCRIPT_RESOURCES`. Update the fixed renderer
and resource hashes when intentionally changing assets; retain behavior tests.
