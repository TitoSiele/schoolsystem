/*
 * Frontend configuration.
 *
 * The frontend is kept as plain static files with no build step, so this is the
 * ONLY place that knows where the API lives. Everything else calls
 * api("/payments") instead of hard-coding a URL.
 *
 * Local development (FastAPI serving these files from ../frontend):
 *     API_BASE_URL = ""
 * Separate deployments (e.g. frontend on Vercel, backend on Render):
 *     set API_BASE_URL = "https://your-backend.onrender.com"
 *
 * Empty string means "same origin as this page", which is what we want when
 * FastAPI mounts the frontend at "/".
 */
/*
 * Attached to window explicitly so this works regardless of how the script is
 * loaded, and so the value is inspectable from the console.
 */
window.API_BASE_URL = "";

/*
 * Resolve an API path against the configured base.
 *   api("/payments") -> "/payments"                        (same origin)
 *   api("/payments") -> "https://api.example.com/payments" (split deploy)
 */
window.api = function (path) {
    return `${String(window.API_BASE_URL).replace(/\/$/, "")}${path}`;
};